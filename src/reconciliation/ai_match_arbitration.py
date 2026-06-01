"""Bounded AI arbitration for cases unresolved by deterministic matching.

The model may choose only supplied row IDs. Every accepted placement is checked
again by deterministic Python before it reaches the workbook.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from src.config import Settings, estimate_tokens, settings
from src.providers.ai_client import AIClient, build_ai_client
from src.reconciliation.issue_taxonomy import PrimaryIssueCode, normalize_ai_reason_code
from src.reconciliation.matching_engine import (
    ACCEPTED_DETERMINISTIC_STATUSES,
    MatchResult,
    is_mirror_polarity_plausible,
    is_missing_reference,
    is_type_compatible,
)
from src.reconciliation.recon_models import (
    AIArbitrationResult,
    AIMatchDecision,
    AIReconciliationCandidate,
    AIReconciliationCandidatePacket,
    AIReconciliationComparison,
    AIReconciliationRequest,
    AIReconciliationResponse,
    AIValidationResult,
    FinalMatchDecisionSource,
    MatchRecord,
    ReconRow,
    UnresolvedDeterministicMatchCluster,
)

logger = logging.getLogger("reconciliation.ai_match_arbitration")

PROMPT_VERSION = "ai-recon-v5"

ALLOWED_DECISION_TYPES = {
    "one_to_one",
    "one_to_many",
    "many_to_one",
    "many_to_many",
    "ledger_only",
    "review_required",
}
ALLOWED_MATCH_TYPES = {
    "exact_reference",
    "amount_date",
    "grouped_amount",
    "voucher_typo",
    "date_shift",
    "rounding",
    "contextual",
    "side_mirror",
    "unmatched",
    "review",
}
DECISION_KEYS = {
    "decision_id",
    "decision_type",
    "org_row_ids",
    "party_row_ids",
    "match_type",
    "confidence",
    "amount_delta",
    "date_delta_days",
    "reason_code",
    "explanation",
    "review_reason",
}

_SYSTEM_PROMPT = """You reconcile two accounting ledgers.
Use only supplied row IDs and supplied evidence. Never invent rows, amounts, dates,
vouchers, references, or facts. Prefer a final decision when evidence is sufficient.
Use review_required only when evidence is genuinely insufficient. Return strict JSON
only, with exactly one top-level key: decisions. No markdown and no extra keys.
Every decision object MUST contain every schema key exactly once, including
review_reason with null when review is not needed. Never omit a key. Never duplicate a
key. Cardinality MUST agree with decision_type: one_to_one=1 org and 1 party;
one_to_many=1 org and 2+ party; many_to_one=2+ org and 1 party; many_to_many=2+ org
and 2+ party; ledger_only=rows from exactly one side; review_required=at least 1 row.
Use each supplied row ID at most once across the complete response. Prefer the smallest
justified group: use separate one_to_one decisions when rows can be paired separately.
For every decision calculate amount_delta exactly as
abs(abs(sum(org amount_org_perspective))-abs(sum(party amount_org_perspective))).
Do not report an amount_delta from an individual row when the decision contains a group.
For one_to_one decisions copy amount_delta and date_delta_days from the supplied
comparison facts. Use match_type exact_reference ONLY when reference_relation is exact.
The supplied comparisons are OPTIONS, not output decisions. Do NOT emit one decision per
comparison. Choose a non-overlapping final set. Once a row ID appears in one decision, do
not use it again. For rows without a valid counterpart, emit review_required with the row
ID on its correct side. Confidence is your confidence in the proposed final placement;
use review_required instead of a low-confidence final placement."""


def _json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _date_delta_days(a: str, b: str) -> int | None:
    try:
        return abs((datetime.strptime(a[:10], "%Y-%m-%d") - datetime.strptime(b[:10], "%Y-%m-%d")).days)
    except (TypeError, ValueError):
        return None


def _row_ids(record: MatchRecord, side: str) -> list[str]:
    plural = record.org_row_ids if side == "org" else record.party_row_ids
    singular = record.org_row_id if side == "org" else record.party_row_id
    return list(dict.fromkeys(plural or ([singular] if singular else [])))


def _rows_by_id(rows: list[ReconRow]) -> dict[str, ReconRow]:
    return {row.row_id: row for row in rows if row.record_kind == "Transaction" and row.row_id}


def _reference_similarity(org: ReconRow, party: ReconRow) -> float:
    left = org.normalized_reference
    right = party.normalized_reference
    if is_missing_reference(left) or is_missing_reference(right):
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.9
    return SequenceMatcher(a=left.lower(), b=right.lower()).ratio()


def _comparison_reference_relation(org: ReconRow, party: ReconRow) -> str:
    left = org.normalized_reference
    right = party.normalized_reference
    if is_missing_reference(left) or is_missing_reference(right):
        return "missing"
    if left == right:
        return "exact"
    if left in right or right in left:
        return "contains"
    if _reference_similarity(org, party) >= 0.65:
        return "similar"
    return "different"


def _coherent_candidate(org: ReconRow, party: ReconRow, config: Settings) -> bool:
    """Keep packets compact while still allowing split/grouped resolution."""
    if not is_type_compatible(org.type_label, party.type_label):
        return False
    if not is_mirror_polarity_plausible(org, party):
        return False
    date_delta = _date_delta_days(org.date, party.date)
    if date_delta is not None and date_delta > config.ai_recon_date_tolerance_days:
        return False
    amount_delta = abs(abs(org.net_org) - abs(party.net_org))
    if amount_delta <= max(config.ai_recon_amount_tolerance, 1.0):
        return True
    if _reference_similarity(org, party) >= 0.55:
        return True
    # A smaller row may be one component of a grouped amount decision.
    return 0 < min(abs(org.net_org), abs(party.net_org)) < max(abs(org.net_org), abs(party.net_org))


def build_unresolved_clusters(
    org_rows: list[ReconRow],
    party_rows: list[ReconRow],
    deterministic_records: list[MatchRecord],
    *,
    config: Settings | None = None,
) -> list[UnresolvedDeterministicMatchCluster]:
    """Build connected unresolved components after preserving strong matches."""
    config = config or settings
    org_all = _rows_by_id(org_rows)
    party_all = _rows_by_id(party_rows)
    consumed_org: set[str] = set()
    consumed_party: set[str] = set()
    for record in deterministic_records:
        if record.match_status in ACCEPTED_DETERMINISTIC_STATUSES and not record.review_required:
            consumed_org.update(_row_ids(record, "org"))
            consumed_party.update(_row_ids(record, "party"))

    org = {key: row for key, row in org_all.items() if key not in consumed_org}
    party = {key: row for key, row in party_all.items() if key not in consumed_party}
    nodes = [f"org:{key}" for key in org] + [f"party:{key}" for key in party]
    parent = {node: node for node in nodes}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    reasons_by_node: dict[str, set[str]] = {node: set() for node in nodes}
    for record in deterministic_records:
        if not record.review_required:
            continue
        org_ids = [row_id for row_id in _row_ids(record, "org") if row_id in org]
        party_ids = [row_id for row_id in _row_ids(record, "party") if row_id in party]
        for row_id in org_ids:
            reasons_by_node[f"org:{row_id}"].add(record.review_reason or record.match_rule)
        for row_id in party_ids:
            reasons_by_node[f"party:{row_id}"].add(record.review_reason or record.match_rule)
        for org_id in org_ids:
            for party_id in party_ids:
                union(f"org:{org_id}", f"party:{party_id}")

    # Add nearby compatible rows so split/grouped decisions receive enough
    # evidence even when deterministic matching emitted only isolated unmatched rows.
    for org_id, org_row in org.items():
        for party_id, party_row in party.items():
            if _coherent_candidate(org_row, party_row, config):
                union(f"org:{org_id}", f"party:{party_id}")

    components: dict[str, list[str]] = {}
    for node in sorted(nodes):
        components.setdefault(find(node), []).append(node)

    clusters: list[UnresolvedDeterministicMatchCluster] = []
    for index, component in enumerate(sorted(components.values()), start=1):
        reasons = sorted({reason for node in component for reason in reasons_by_node[node] if reason})
        clusters.append(
            UnresolvedDeterministicMatchCluster(
                cluster_id=f"UC{index:06d}",
                org_row_ids=sorted(node[4:] for node in component if node.startswith("org:")),
                party_row_ids=sorted(node[6:] for node in component if node.startswith("party:")),
                deterministic_reason=" | ".join(reasons) or "No deterministic match found.",
            )
        )
    return clusters


def _candidate_from_row(
    row: ReconRow,
    *,
    side: str,
    intern_text,
) -> AIReconciliationCandidate:
    return AIReconciliationCandidate(
        side=side,
        row_id=row.row_id,
        type_label=row.type_label,
        raw_date=str(row.data.get("raw_date") or row.data.get("date") or ""),
        normalized_date=row.date,
        voucher_no=str(row.data.get("voucher_no") or ""),
        bill_no=str(row.data.get("bill_no") or ""),
        reference_no=row.reference,
        normalized_reference=row.normalized_reference,
        particulars_ref=intern_text("particulars", row.data.get("particulars")),
        account_ref=intern_text("account", row.data.get("account")),
        debit_source=_float(row.data.get("debit_source")),
        credit_source=_float(row.data.get("credit_source")),
        debit_org_perspective=_float(row.data.get("debit_org_perspective")),
        credit_org_perspective=_float(row.data.get("credit_org_perspective")),
        amount_org_perspective=row.net_org,
        source_file=str(row.data.get("source_file") or ""),
        page_number=str(row.data.get("page_number") or ""),
        source_row_number=str(row.data.get("source_row_number") or ""),
    )


def build_candidate_packets(
    org_rows: list[ReconRow],
    party_rows: list[ReconRow],
    deterministic_records: list[MatchRecord],
    *,
    config: Settings | None = None,
) -> list[AIReconciliationCandidatePacket]:
    """Create compact, deduplicated candidate packets for unresolved clusters."""
    config = config or settings
    org = _rows_by_id(org_rows)
    party = _rows_by_id(party_rows)
    packets: list[AIReconciliationCandidatePacket] = []
    for index, cluster in enumerate(
        build_unresolved_clusters(org_rows, party_rows, deterministic_records, config=config),
        start=1,
    ):
        text_dictionary: dict[str, str] = {}
        text_key_by_value: dict[tuple[str, str], str] = {}

        def intern_text(kind: str, value: object) -> str:
            text = str(value or "").strip()[:240]
            if not text:
                return ""
            lookup = (kind, text)
            if lookup not in text_key_by_value:
                key = f"T{len(text_dictionary) + 1:03d}"
                text_key_by_value[lookup] = key
                text_dictionary[key] = text
            return text_key_by_value[lookup]

        candidates = [
            *[
                _candidate_from_row(org[row_id], side="org", intern_text=intern_text)
                for row_id in cluster.org_row_ids
            ],
            *[
                _candidate_from_row(party[row_id], side="party", intern_text=intern_text)
                for row_id in cluster.party_row_ids
            ],
        ]
        comparisons = [
            AIReconciliationComparison(
                org_row_id=org_row_id,
                party_row_id=party_row_id,
                amount_delta=round(abs(abs(org[org_row_id].net_org) - abs(party[party_row_id].net_org)), 2),
                date_delta_days=_date_delta_days(org[org_row_id].date, party[party_row_id].date),
                reference_relation=_comparison_reference_relation(org[org_row_id], party[party_row_id]),
                reference_similarity=round(_reference_similarity(org[org_row_id], party[party_row_id]), 3),
                type_compatible=is_type_compatible(org[org_row_id].type_label, party[party_row_id].type_label),
                source_side_mirror=_sign(org[org_row_id].net_source) != _sign(party[party_row_id].net_source),
            )
            for org_row_id in cluster.org_row_ids
            for party_row_id in cluster.party_row_ids
        ]
        packet_id = f"ARP{index:06d}"
        base = {
            "prompt_version": PROMPT_VERSION,
            "packet_id": packet_id,
            "cluster_id": cluster.cluster_id,
            "deterministic_reason": cluster.deterministic_reason,
            "text_dictionary": text_dictionary,
            "candidates": [candidate.as_dict() for candidate in candidates],
            "comparisons": [comparison.as_dict() for comparison in comparisons],
        }
        packets.append(
            AIReconciliationCandidatePacket(
                packet_id=packet_id,
                cluster_id=cluster.cluster_id,
                deterministic_reason=cluster.deterministic_reason,
                candidates=candidates,
                comparisons=comparisons,
                text_dictionary=text_dictionary,
                fingerprint=_json_hash(base),
                over_candidate_limit=len(candidates) > max(1, config.ai_recon_max_candidates_per_packet),
            )
        )
    return packets


def build_ai_reconciliation_request(
    packet: AIReconciliationCandidatePacket,
) -> AIReconciliationRequest:
    """Build the severe JSON-only prompt for one compact packet."""
    schema = {
        "decisions": [
            {
                "decision_id": "string",
                "decision_type": "one_to_one | one_to_many | many_to_one | many_to_many | ledger_only | review_required",
                "org_row_ids": ["supplied org row IDs only"],
                "party_row_ids": ["supplied party row IDs only"],
                "match_type": "exact_reference | amount_date | grouped_amount | voucher_typo | date_shift | rounding | contextual | side_mirror | unmatched | review",
                "confidence": 0.0,
                "amount_delta": 0.0,
                "date_delta_days": 0,
                "reason_code": "compact_string",
                "explanation": "compact evidence references only",
                "review_reason": None,
            }
        ]
    }
    user_payload = {
        "instruction": (
            "Decide placement for this unresolved cluster. Check org/party mirror direction, "
            "amount totals, dates, references, vouchers, and grouped sums. Every supplied row "
            "must be covered exactly once by a decision or left for review. Return every "
            "required field for every decision. Use review_reason:null for accepted decisions. "
            "Use each supplied row ID at most once across the entire response. Prefer separate "
            "one_to_one decisions over broad groups when row-level pairing is supported. For "
            "one_to_one decisions copy amount_delta and date_delta_days from comparisons. Use "
            "exact_reference only when the comparison reference_relation is exact. Comparisons "
            "are candidate options, not decisions: choose a non-overlapping final set and never "
            "emit one decision per comparison. Use review_required instead of low-confidence "
            "final placement."
        ),
        "required_keys_every_decision": sorted(DECISION_KEYS),
        "required_schema": schema,
        "packet": packet.as_dict(),
    }
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        },
    ]
    prompt_fingerprint = _json_hash(messages)
    return AIReconciliationRequest(
        packet_id=packet.packet_id,
        prompt_fingerprint=prompt_fingerprint,
        estimated_input_tokens=estimate_tokens(json.dumps(messages, ensure_ascii=False)),
        messages=messages,
    )


def parse_ai_reconciliation_response(
    raw: str,
    *,
    prompt_fingerprint: str = "",
    response_fingerprint: str = "",
    cache_key: str = "",
    cache_hit: bool = False,
) -> AIReconciliationResponse:
    """Parse strict JSON and reject schema drift or invented output fields."""
    try:
        payload = json.loads((raw or "").strip(), object_pairs_hook=_object_without_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("AI response is not strict JSON.") from exc
    if not isinstance(payload, dict) or set(payload) != {"decisions"}:
        raise ValueError("AI response must contain exactly the top-level key 'decisions'.")
    decisions_payload = payload["decisions"]
    if not isinstance(decisions_payload, list):
        raise ValueError("AI response 'decisions' must be a list.")

    decisions: list[AIMatchDecision] = []
    seen_ids: set[str] = set()
    for item in decisions_payload:
        if not isinstance(item, dict) or set(item) != DECISION_KEYS:
            raise ValueError("AI decision schema invalid or contains extra fields.")
        decision_id = item["decision_id"]
        if not isinstance(decision_id, str) or not decision_id.strip() or decision_id in seen_ids:
            raise ValueError("AI decision_id must be a unique non-empty string.")
        seen_ids.add(decision_id)
        org_row_ids = item["org_row_ids"]
        party_row_ids = item["party_row_ids"]
        if not _valid_row_id_list(org_row_ids) or not _valid_row_id_list(party_row_ids):
            raise ValueError("AI org_row_ids and party_row_ids must be lists of unique strings.")
        confidence = item["confidence"]
        amount_delta = item["amount_delta"]
        date_delta_days = item["date_delta_days"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("AI confidence must be numeric.")
        if isinstance(amount_delta, bool) or not isinstance(amount_delta, (int, float)):
            raise ValueError("AI amount_delta must be numeric.")
        if isinstance(date_delta_days, bool) or not isinstance(date_delta_days, int):
            raise ValueError("AI date_delta_days must be an integer.")
        if not 0.0 <= float(confidence) <= 1.0 or float(amount_delta) < 0 or date_delta_days < 0:
            raise ValueError("AI confidence/delta values are out of range.")
        for key in ("decision_type", "match_type", "reason_code", "explanation"):
            if not isinstance(item[key], str):
                raise ValueError(f"AI {key} must be a string.")
        if item["review_reason"] is not None and not isinstance(item["review_reason"], str):
            raise ValueError("AI review_reason must be a string or null.")
        decisions.append(
            AIMatchDecision(
                decision_id=decision_id.strip(),
                decision_type=item["decision_type"].strip(),
                org_row_ids=org_row_ids,
                party_row_ids=party_row_ids,
                match_type=item["match_type"].strip(),
                confidence=float(confidence),
                amount_delta=float(amount_delta),
                date_delta_days=date_delta_days,
                reason_code=item["reason_code"].strip(),
                explanation=item["explanation"].strip(),
                review_reason=item["review_reason"],
            )
        )
    return AIReconciliationResponse(
        decisions=decisions,
        prompt_fingerprint=prompt_fingerprint,
        response_fingerprint=response_fingerprint or _text_hash(raw),
        cache_key=cache_key,
        cache_hit=cache_hit,
    )


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object keys rather than silently keeping the last value."""
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"AI response contains duplicate JSON key: {key}.")
        output[key] = value
    return output


def _valid_row_id_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and item.strip() for item in value)
        and len(value) == len(set(value))
    )


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _max_date_delta(org_rows: list[ReconRow], party_rows: list[ReconRow]) -> int | None:
    deltas = [
        delta
        for org in org_rows
        for party in party_rows
        if (delta := _date_delta_days(org.date, party.date)) is not None
    ]
    return max(deltas) if deltas else None


def _best_reference_similarity(org_rows: list[ReconRow], party_rows: list[ReconRow]) -> float:
    scores = [_reference_similarity(org, party) for org in org_rows for party in party_rows]
    return max(scores) if scores else 0.0


def validate_ai_decision(
    decision: AIMatchDecision,
    packet: AIReconciliationCandidatePacket,
    org_rows_by_id: dict[str, ReconRow],
    party_rows_by_id: dict[str, ReconRow],
    *,
    used_org_row_ids: set[str] | None = None,
    used_party_row_ids: set[str] | None = None,
    config: Settings | None = None,
) -> AIValidationResult:
    """Apply deterministic invariants to one AI-proposed decision."""
    config = config or settings
    used_org_row_ids = used_org_row_ids or set()
    used_party_row_ids = used_party_row_ids or set()
    supplied_org = {candidate.row_id for candidate in packet.candidates if candidate.side == "org"}
    supplied_party = {candidate.row_id for candidate in packet.candidates if candidate.side == "party"}

    def reject(reason: str, *, amount: float | None = None, date: int | None = None) -> AIValidationResult:
        return AIValidationResult(decision.decision_id, False, "REJECTED", reason, amount, date)

    if decision.decision_type not in ALLOWED_DECISION_TYPES:
        return reject("decision_type_not_allowed")
    if decision.match_type not in ALLOWED_MATCH_TYPES:
        return reject("match_type_not_allowed")
    if not set(decision.org_row_ids).issubset(supplied_org) or not set(decision.party_row_ids).issubset(supplied_party):
        return reject("row_id_not_supplied")
    if any(row_id not in org_rows_by_id for row_id in decision.org_row_ids) or any(
        row_id not in party_rows_by_id for row_id in decision.party_row_ids
    ):
        return reject("row_id_not_found")
    if used_org_row_ids.intersection(decision.org_row_ids) or used_party_row_ids.intersection(decision.party_row_ids):
        return reject("duplicate_row_ownership")
    if decision.confidence < config.ai_recon_min_confidence:
        return reject("confidence_below_threshold")

    cardinality = (len(decision.org_row_ids), len(decision.party_row_ids))
    valid_cardinality = {
        "one_to_one": cardinality == (1, 1),
        "one_to_many": cardinality[0] == 1 and cardinality[1] > 1,
        "many_to_one": cardinality[0] > 1 and cardinality[1] == 1,
        "many_to_many": cardinality[0] > 1 and cardinality[1] > 1,
        "ledger_only": (cardinality[0] > 0) ^ (cardinality[1] > 0),
        "review_required": cardinality[0] + cardinality[1] > 0,
    }[decision.decision_type]
    if not valid_cardinality:
        return reject("decision_cardinality_invalid")
    if decision.decision_type == "review_required":
        return reject(decision.review_reason or "ai_requested_human_review")
    if decision.decision_type == "ledger_only":
        if decision.match_type != "unmatched":
            return reject("ledger_only_requires_unmatched_match_type")
        return AIValidationResult(decision.decision_id, True, "ACCEPTED", actual_amount_delta=0.0)

    org_rows = [org_rows_by_id[row_id] for row_id in decision.org_row_ids]
    party_rows = [party_rows_by_id[row_id] for row_id in decision.party_row_ids]
    org_amount = sum(row.net_org for row in org_rows)
    party_amount = sum(row.net_org for row in party_rows)
    amount_delta = abs(abs(org_amount) - abs(party_amount))
    date_delta = _max_date_delta(org_rows, party_rows)
    if abs(decision.amount_delta - amount_delta) > config.ai_recon_amount_tolerance:
        return reject("invented_or_incorrect_amount_delta", amount=amount_delta, date=date_delta)
    if amount_delta > config.ai_recon_amount_tolerance:
        return reject("amount_mismatch", amount=amount_delta, date=date_delta)
    if date_delta is not None and date_delta > config.ai_recon_date_tolerance_days:
        return reject("date_tolerance_exceeded", amount=amount_delta, date=date_delta)
    if date_delta is not None and decision.date_delta_days != date_delta:
        return reject("invented_or_incorrect_date_delta", amount=amount_delta, date=date_delta)
    if _sign(org_amount) != _sign(party_amount):
        return reject("org_perspective_polarity_impossible", amount=amount_delta, date=date_delta)
    org_source = sum(row.net_source for row in org_rows)
    party_source = sum(row.net_source for row in party_rows)
    if _sign(org_source) == _sign(party_source):
        return reject("source_side_mirror_impossible", amount=amount_delta, date=date_delta)
    ref_similarity = _best_reference_similarity(org_rows, party_rows)
    if decision.match_type == "exact_reference" and ref_similarity < 1.0:
        return reject("exact_reference_not_supported", amount=amount_delta, date=date_delta)
    if decision.match_type == "voucher_typo" and ref_similarity < 0.65:
        return reject("voucher_typo_similarity_too_low", amount=amount_delta, date=date_delta)
    if decision.reason_code and not normalize_ai_reason_code(decision.reason_code):
        return reject("issue_code_not_allowed", amount=amount_delta, date=date_delta)
    return AIValidationResult(
        decision.decision_id,
        True,
        "ACCEPTED",
        actual_amount_delta=amount_delta,
        actual_date_delta_days=date_delta,
    )


def _joined(rows: list[ReconRow], attr: str) -> str:
    values = []
    for row in rows:
        value = getattr(row, attr)
        if value and value not in values:
            values.append(value)
    return " | ".join(values[:5])


def _record_from_ai_decision(
    decision: AIMatchDecision,
    validation: AIValidationResult,
    response: AIReconciliationResponse,
    org_rows_by_id: dict[str, ReconRow],
    party_rows_by_id: dict[str, ReconRow],
) -> MatchRecord:
    org_rows = [org_rows_by_id[row_id] for row_id in decision.org_row_ids]
    party_rows = [party_rows_by_id[row_id] for row_id in decision.party_row_ids]
    org_amount = sum(row.net_org for row in org_rows) if org_rows else None
    party_amount = sum(row.net_org for row in party_rows) if party_rows else None
    status = "ledger_only_ai" if decision.decision_type == "ledger_only" else "matched_ai"
    first = (org_rows or party_rows)[0]
    return MatchRecord(
        match_group_id=decision.decision_id,
        match_status=status,
        match_confidence=round(decision.confidence, 3),
        match_rule=f"ai_arbitration_{decision.match_type}",
        type_label=first.type_label,
        org_row_id=org_rows[0].row_id if org_rows else "",
        party_row_id=party_rows[0].row_id if party_rows else "",
        org_date=org_rows[0].date if org_rows else "",
        party_date=party_rows[0].date if party_rows else "",
        date_delta_days=validation.actual_date_delta_days,
        org_reference=_joined(org_rows, "reference"),
        party_reference=_joined(party_rows, "reference"),
        org_normalized_reference=_joined(org_rows, "normalized_reference"),
        party_normalized_reference=_joined(party_rows, "normalized_reference"),
        reference_relation=decision.match_type,
        org_amount=org_amount,
        party_amount=party_amount,
        amount_difference=validation.actual_amount_delta,
        org_raw_type=" | ".join(str(row.data.get("raw_type") or "") for row in org_rows),
        party_raw_type=" | ".join(str(row.data.get("raw_type") or "") for row in party_rows),
        org_particulars=" | ".join(str(row.data.get("particulars") or "") for row in org_rows),
        party_particulars=" | ".join(str(row.data.get("particulars") or "") for row in party_rows),
        decision_id=decision.decision_id,
        decision_source=FinalMatchDecisionSource.AI.value,
        match_type=decision.match_type,
        org_row_ids=decision.org_row_ids,
        party_row_ids=decision.party_row_ids,
        compact_explanation=decision.explanation,
        validation_status=validation.status,
        validation_reason=validation.rejection_reason,
        prompt_fingerprint=response.prompt_fingerprint,
        response_fingerprint=response.response_fingerprint,
        cache_key=response.cache_key,
        primary_issue_code=normalize_ai_reason_code(decision.reason_code)
        or PrimaryIssueCode.EXACT_MATCH.value,
    )


def _review_record(
    packet: AIReconciliationCandidatePacket,
    org_rows_by_id: dict[str, ReconRow],
    party_rows_by_id: dict[str, ReconRow],
    reason: str,
    *,
    org_row_ids: list[str] | None = None,
    party_row_ids: list[str] | None = None,
    response: AIReconciliationResponse | None = None,
) -> MatchRecord:
    org_ids = org_row_ids if org_row_ids is not None else [
        candidate.row_id for candidate in packet.candidates if candidate.side == "org"
    ]
    party_ids = party_row_ids if party_row_ids is not None else [
        candidate.row_id for candidate in packet.candidates if candidate.side == "party"
    ]
    if not org_ids and not party_ids:
        org_ids = [candidate.row_id for candidate in packet.candidates if candidate.side == "org"]
        party_ids = [candidate.row_id for candidate in packet.candidates if candidate.side == "party"]
    org_rows = [org_rows_by_id[row_id] for row_id in org_ids if row_id in org_rows_by_id]
    party_rows = [party_rows_by_id[row_id] for row_id in party_ids if row_id in party_rows_by_id]
    first = (org_rows or party_rows)[0]
    org_amount = sum(row.net_org for row in org_rows) if org_rows else None
    party_amount = sum(row.net_org for row in party_rows) if party_rows else None
    amount_delta = (
        abs(abs(org_amount) - abs(party_amount))
        if org_amount is not None and party_amount is not None
        else None
    )
    decision_id = f"{packet.packet_id}-REVIEW"
    return MatchRecord(
        match_group_id=decision_id,
        match_status="candidate_review",
        match_confidence=0.0,
        match_rule="ai_arbitration_review",
        type_label=first.type_label,
        org_row_id=org_rows[0].row_id if org_rows else "",
        party_row_id=party_rows[0].row_id if party_rows else "",
        org_date=org_rows[0].date if org_rows else "",
        party_date=party_rows[0].date if party_rows else "",
        date_delta_days=_max_date_delta(org_rows, party_rows),
        org_reference=_joined(org_rows, "reference"),
        party_reference=_joined(party_rows, "reference"),
        org_normalized_reference=_joined(org_rows, "normalized_reference"),
        party_normalized_reference=_joined(party_rows, "normalized_reference"),
        org_amount=org_amount,
        party_amount=party_amount,
        amount_difference=amount_delta,
        org_raw_type=" | ".join(str(row.data.get("raw_type") or "") for row in org_rows),
        party_raw_type=" | ".join(str(row.data.get("raw_type") or "") for row in party_rows),
        org_particulars=" | ".join(str(row.data.get("particulars") or "") for row in org_rows),
        party_particulars=" | ".join(str(row.data.get("particulars") or "") for row in party_rows),
        review_required=True,
        review_reason=reason,
        decision_id=decision_id,
        decision_source=FinalMatchDecisionSource.HUMAN_REVIEW_REQUIRED.value,
        match_type="review",
        org_row_ids=org_ids,
        party_row_ids=party_ids,
        compact_explanation=reason,
        validation_status="REJECTED",
        validation_reason=reason,
        prompt_fingerprint=(response.prompt_fingerprint if response else ""),
        response_fingerprint=(response.response_fingerprint if response else ""),
        cache_key=(response.cache_key if response else packet.fingerprint),
    )


def _cache_key(packet: AIReconciliationCandidatePacket, config: Settings) -> str:
    return _json_hash(
        {
            "prompt_version": PROMPT_VERSION,
            "packet_fingerprint": packet.fingerprint,
            "amount_tolerance": config.ai_recon_amount_tolerance,
            "date_tolerance_days": config.ai_recon_date_tolerance_days,
            "min_confidence": config.ai_recon_min_confidence,
            "model": config.ai_model_name,
        }
    )


def _cache_path(cache_key: str, config: Settings) -> Path:
    return config.resolved(config.ai_recon_cache_dir) / f"{cache_key}.json"


def _read_cached_raw(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = payload.get("raw_response") if isinstance(payload, dict) else None
    return raw if isinstance(raw, str) else None


def _write_cached_raw(path: Path, raw: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"raw_response": raw}, ensure_ascii=False), encoding="utf-8")


def _provider_config(config: Settings) -> Settings:
    return config.model_copy(
        update={
            "ai_max_input_tokens_per_request": config.ai_recon_max_input_tokens_per_request,
            "ai_max_output_tokens": config.ai_recon_max_output_tokens,
        }
    )


def arbitrate_unresolved_matches(
    org_rows: list[ReconRow],
    party_rows: list[ReconRow],
    deterministic: MatchResult | list[MatchRecord],
    *,
    config: Settings | None = None,
    client: AIClient | None = None,
    strict: bool = False,
) -> AIArbitrationResult:
    """Run bounded arbitration and return final workbook records."""
    config = config or settings
    deterministic_records = deterministic.records if isinstance(deterministic, MatchResult) else deterministic
    if not config.ai_reconciliation_active():
        return AIArbitrationResult(records=list(deterministic_records))

    strong = [
        record
        for record in deterministic_records
        if record.match_status in ACCEPTED_DETERMINISTIC_STATUSES and not record.review_required
    ]
    packets = build_candidate_packets(org_rows, party_rows, deterministic_records, config=config)
    result = AIArbitrationResult(records=list(strong), packets=packets)
    org_rows_by_id = _rows_by_id(org_rows)
    party_rows_by_id = _rows_by_id(party_rows)
    used_org: set[str] = set()
    used_party: set[str] = set()
    provider = client
    processed_batches = 0

    for packet in packets:
        if packet.over_candidate_limit:
            result.records.append(
                _review_record(packet, org_rows_by_id, party_rows_by_id, "candidate_packet_limit_exceeded")
            )
            continue
        if processed_batches >= max(0, config.ai_recon_max_batches):
            result.records.append(
                _review_record(packet, org_rows_by_id, party_rows_by_id, "ai_recon_max_batches_exceeded")
            )
            continue
        processed_batches += 1

        request = build_ai_reconciliation_request(packet)
        if request.estimated_input_tokens > config.ai_recon_max_input_tokens_per_request:
            result.records.append(
                _review_record(packet, org_rows_by_id, party_rows_by_id, "ai_recon_input_token_budget_exceeded")
            )
            continue

        cache_key = _cache_key(packet, config)
        cache_path = _cache_path(cache_key, config)
        raw = _read_cached_raw(cache_path) if config.ai_recon_cache_enabled and cache_path.exists() else None
        cache_hit = raw is not None
        if cache_hit:
            result.cache_hit_count += 1
        else:
            result.cache_miss_count += 1
            try:
                if provider is None:
                    config.ensure_reconciliation_ai_allowed()
                    provider = build_ai_client(_provider_config(config))
                raw = provider.complete_chat(request.messages, request_json_object=True)
                result.provider_call_count += 1
                if config.ai_recon_cache_enabled:
                    _write_cached_raw(cache_path, raw)
            except Exception as exc:  # noqa: BLE001 - review fallback is intentional
                if strict:
                    raise
                logger.warning("AI arbitration call failed for %s: %s", packet.packet_id, type(exc).__name__)
                result.records.append(
                    _review_record(packet, org_rows_by_id, party_rows_by_id, "ai_provider_failure")
                )
                continue

        try:
            response = parse_ai_reconciliation_response(
                raw or "",
                prompt_fingerprint=request.prompt_fingerprint,
                response_fingerprint=_text_hash(raw or ""),
                cache_key=cache_key,
                cache_hit=cache_hit,
            )
        except ValueError:
            result.records.append(
                _review_record(packet, org_rows_by_id, party_rows_by_id, "ai_response_schema_invalid")
            )
            continue
        if not response.decisions:
            result.records.append(
                _review_record(packet, org_rows_by_id, party_rows_by_id, "ai_response_has_no_decisions", response=response)
            )
            continue

        covered_org: set[str] = set()
        covered_party: set[str] = set()
        rejected_reasons: list[str] = []
        accepted_in_packet: list[tuple[AIMatchDecision, AIValidationResult]] = []
        for decision in response.decisions:
            validation = validate_ai_decision(
                decision,
                packet,
                org_rows_by_id,
                party_rows_by_id,
                used_org_row_ids=used_org | covered_org,
                used_party_row_ids=used_party | covered_party,
                config=config,
            )
            result.validation_results.append(validation)
            if not validation.accepted:
                if not config.ai_recon_review_on_validation_failure:
                    raise RuntimeError(
                        f"AI reconciliation validation failed for {decision.decision_id}: "
                        f"{validation.rejection_reason}"
                    )
                rejected_reasons.append(f"{decision.decision_id}:{validation.rejection_reason}")
                continue
            accepted_in_packet.append((decision, validation))
            covered_org.update(decision.org_row_ids)
            covered_party.update(decision.party_row_ids)

        supplied_org = {candidate.row_id for candidate in packet.candidates if candidate.side == "org"}
        supplied_party = {candidate.row_id for candidate in packet.candidates if candidate.side == "party"}
        remaining_org = sorted(supplied_org - covered_org)
        remaining_party = sorted(supplied_party - covered_party)
        if rejected_reasons:
            result.records.append(
                _review_record(
                    packet,
                    org_rows_by_id,
                    party_rows_by_id,
                    " | ".join(rejected_reasons),
                    response=response,
                )
            )
            continue
        for decision, validation in accepted_in_packet:
            result.records.append(
                _record_from_ai_decision(decision, validation, response, org_rows_by_id, party_rows_by_id)
            )
            used_org.update(decision.org_row_ids)
            used_party.update(decision.party_row_ids)
            result.accepted_decision_count += 1
        if remaining_org or remaining_party:
            result.records.append(
                _review_record(
                    packet,
                    org_rows_by_id,
                    party_rows_by_id,
                    "ai_decision_did_not_cover_all_supplied_rows",
                    org_row_ids=remaining_org,
                    party_row_ids=remaining_party,
                    response=response,
                )
            )

    result.review_required_count = sum(1 for record in result.records if record.review_required)
    return result
