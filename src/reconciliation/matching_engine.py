"""Deterministic matching engine for reconciliation candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Iterable

from src.reconciliation.recon_models import MatchRecord, ReconRow

DEFAULT_AMOUNT_TOLERANCE = 0.01
DEFAULT_DATE_TOLERANCE_DAYS = 7

# Tokens that must always be treated as a MISSING reference. Excel/text round
# trips turn empty cells into the literal strings "None"/"nan", so a naive
# truthiness check is unsafe and previously produced false strong matches.
MISSING_REFERENCE_TOKENS: frozenset[str] = frozenset(
    {"", "none", "nan", "null", "-", "--"}
)

MISSING_REFERENCE_REASON = (
    "Missing normalized reference on at least one side; no deterministic "
    "reference evidence. Amount/date/type candidate only."
)


def is_missing_reference(value: object) -> bool:
    """True when a normalized reference must be treated as absent.

    Blank, whitespace, ``None``/``"None"``, ``"nan"``, ``"null"``, ``"-"`` and
    ``"--"`` all count as missing. Such references must never be used for exact,
    containment, or fuzzy reference matching.
    """
    return str(value if value is not None else "").strip().lower() in MISSING_REFERENCE_TOKENS


@dataclass
class MatchResult:
    """Matching output plus convenience stats."""

    records: list[MatchRecord]
    strong_match_count: int
    review_match_count: int
    unmatched_org_count: int
    unmatched_party_count: int


def is_type_compatible(org_type: str, party_type: str) -> bool:
    """Conservative type compatibility rules."""
    a = (org_type or "").strip()
    b = (party_type or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    mirror = {
        ("Receipt", "Payment"),
        ("Payment", "Receipt"),
        ("CreditNote", "DebitNote"),
        ("DebitNote", "CreditNote"),
    }
    return (a, b) in mirror


def _parse_date(text: str) -> datetime | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _date_delta_days(a: str, b: str) -> int | None:
    da = _parse_date(a)
    db = _parse_date(b)
    if not da or not db:
        return None
    return abs((da - db).days)


def _reference_relation(org_ref: str, party_ref: str) -> tuple[str, float]:
    a = (org_ref or "").strip()
    b = (party_ref or "").strip()
    if is_missing_reference(a) or is_missing_reference(b):
        return "missing", 0.0
    if a == b:
        return "exact", 1.0
    if a in b:
        return "contained_by", 0.8
    if b in a:
        return "contains", 0.8
    score = SequenceMatcher(a=a.lower(), b=b.lower()).ratio()
    if score >= 0.85:
        return "fuzzy", score
    return "ambiguous", score


def _amount_close(a: float, b: float, tolerance: float) -> bool:
    return abs(abs(a) - abs(b)) <= tolerance


def _candidate_sort_key(org: ReconRow, party: ReconRow) -> tuple:
    relation, rel_score = _reference_relation(
        org.normalized_reference, party.normalized_reference
    )
    date_delta = _date_delta_days(org.date, party.date)
    return (
        {"exact": 5, "contains": 4, "contained_by": 4, "fuzzy": 3}.get(relation, 1),
        rel_score,
        -abs(abs(org.net_org) - abs(party.net_org)),
        -(9999 if date_delta is None else date_delta),
        -int(party.data.get("source_row_number") or 0),
        party.row_id,
    )


def _make_record(
    group_id: int,
    status: str,
    rule: str,
    org: ReconRow | None,
    party: ReconRow | None,
    confidence: float,
    review_required: bool,
    review_reason: str,
) -> MatchRecord:
    org_amount = org.net_org if org else None
    party_amount = party.net_org if party else None
    delta = None
    if org_amount is not None and party_amount is not None:
        delta = abs(abs(org_amount) - abs(party_amount))
    rel, _ = _reference_relation(
        org.normalized_reference if org else "",
        party.normalized_reference if party else "",
    )
    return MatchRecord(
        match_group_id=f"MG{group_id:06d}",
        match_status=status,
        match_confidence=round(confidence, 3),
        match_rule=rule,
        type_label=(org.type_label if org else party.type_label if party else ""),
        org_row_id=org.row_id if org else "",
        party_row_id=party.row_id if party else "",
        org_date=org.date if org else "",
        party_date=party.date if party else "",
        date_delta_days=_date_delta_days(org.date if org else "", party.date if party else ""),
        org_reference=org.reference if org else "",
        party_reference=party.reference if party else "",
        org_normalized_reference=org.normalized_reference if org else "",
        party_normalized_reference=party.normalized_reference if party else "",
        reference_relation=rel,
        org_amount=org_amount,
        party_amount=party_amount,
        amount_difference=delta,
        org_raw_type=str(org.data.get("raw_type", "")) if org else "",
        party_raw_type=str(party.data.get("raw_type", "")) if party else "",
        org_particulars=str(org.data.get("particulars", "")) if org else "",
        party_particulars=str(party.data.get("particulars", "")) if party else "",
        review_required=review_required,
        review_reason=review_reason,
    )


def _transaction_rows(rows: Iterable[ReconRow]) -> list[ReconRow]:
    return [r for r in rows if r.record_kind == "Transaction"]


def reconcile_rows(
    org_rows: list[ReconRow],
    party_rows: list[ReconRow],
    *,
    amount_tolerance: float = DEFAULT_AMOUNT_TOLERANCE,
    date_tolerance_days: int = DEFAULT_DATE_TOLERANCE_DAYS,
) -> MatchResult:
    """Run deterministic staged matching and candidate generation."""
    org_tx = sorted(_transaction_rows(org_rows), key=lambda r: r.row_id)
    party_tx = sorted(_transaction_rows(party_rows), key=lambda r: r.row_id)
    unmatched_org = {r.row_id: r for r in org_tx}
    unmatched_party = {r.row_id: r for r in party_tx}

    records: list[MatchRecord] = []
    group_id = 1

    def consume(org: ReconRow, party: ReconRow, status: str, rule: str, conf: float) -> None:
        nonlocal group_id
        records.append(
            _make_record(
                group_id,
                status,
                rule,
                org,
                party,
                conf,
                review_required=False,
                review_reason="",
            )
        )
        group_id += 1
        unmatched_org.pop(org.row_id, None)
        unmatched_party.pop(party.row_id, None)

    # Stage 1 + 2 exact ref + amount (+ optional type compatibility).
    # Hard safety rule: if the org reference is missing, stages 1-4 are forbidden.
    for org in list(unmatched_org.values()):
        if is_missing_reference(org.normalized_reference):
            continue
        candidates = [
            p
            for p in unmatched_party.values()
            if not is_missing_reference(p.normalized_reference)
            and p.normalized_reference == org.normalized_reference
            and _amount_close(org.net_org, p.net_org, amount_tolerance)
        ]
        if not candidates:
            continue
        typed = [p for p in candidates if is_type_compatible(org.type_label, p.type_label)]
        pool = typed or candidates
        if len(pool) == 1:
            consume(
                org,
                pool[0],
                "matched_strong",
                "stage1_exact_ref_type_amount" if typed else "stage2_exact_ref_amount",
                1.0 if typed else 0.95,
            )
            continue
        # Ambiguous exact matches -> review.
        best = sorted(pool, key=lambda p: _candidate_sort_key(org, p), reverse=True)
        top = best[0]
        records.append(
            _make_record(
                group_id,
                "candidate_review",
                "stage1_2_ambiguous_exact_ref",
                org,
                top,
                0.7,
                review_required=True,
                review_reason="Multiple exact-reference candidates; deterministic tie unresolved.",
            )
        )
        group_id += 1

    # Stage 3/4/5 on still unmatched org rows.
    #
    # Hard safety rule: if the normalized reference is missing on EITHER side,
    # stages 1-4 (exact / containment / fuzzy reference) are forbidden. Such a
    # row may only be offered as a Stage-5 amount/date/type review candidate and
    # can never become a strong match.
    for org in list(unmatched_org.values()):
        org_ref_missing = is_missing_reference(org.normalized_reference)
        scored: list[tuple[tuple, ReconRow]] = []
        for party in unmatched_party.values():
            if not _amount_close(org.net_org, party.net_org, amount_tolerance):
                continue
            party_ref_missing = is_missing_reference(party.normalized_reference)
            relation, rel_score = _reference_relation(
                org.normalized_reference, party.normalized_reference
            )
            date_delta = _date_delta_days(org.date, party.date)
            compatible_date = date_delta is None or date_delta <= date_tolerance_days
            if org_ref_missing or party_ref_missing:
                # Stage 5 only: amount + (compatible date) + (compatible type).
                type_ok = is_type_compatible(org.type_label, party.type_label)
                if compatible_date and type_ok:
                    scored.append((_candidate_sort_key(org, party), party))
            elif relation in {"contains", "contained_by"}:
                if compatible_date:
                    scored.append((_candidate_sort_key(org, party), party))
            elif relation == "fuzzy" and compatible_date:
                scored.append((_candidate_sort_key(org, party), party))
        if not scored:
            continue
        scored.sort(key=lambda x: x[0], reverse=True)
        top_party = scored[0][1]
        top_party_ref_missing = is_missing_reference(top_party.normalized_reference)
        rel, rel_score = _reference_relation(org.normalized_reference, top_party.normalized_reference)

        # Strong containment is only permitted when BOTH references are present.
        if (
            not org_ref_missing
            and not top_party_ref_missing
            and rel in {"contains", "contained_by"}
            and rel_score >= 0.8
        ):
            consume(org, top_party, "matched_strong", "stage3_containment_ref_amount", 0.9)
            continue

        if org_ref_missing or top_party_ref_missing:
            rule = "stage5_no_ref_amount_date"
            reason = MISSING_REFERENCE_REASON
            confidence = 0.4
        elif rel == "fuzzy":
            rule = "stage4_fuzzy_ref_amount_date"
            reason = "Fuzzy/weak reference candidate requires manual review."
            confidence = 0.6
        else:
            rule = "stage5_no_ref_amount_date"
            reason = MISSING_REFERENCE_REASON
            confidence = 0.4
        records.append(
            _make_record(
                group_id,
                "candidate_review",
                rule,
                org,
                top_party,
                confidence,
                review_required=True,
                review_reason=reason,
            )
        )
        group_id += 1

    # Stage 6/7 unmatched rows.
    for org in sorted(unmatched_org.values(), key=lambda r: r.row_id):
        records.append(
            _make_record(
                group_id,
                "unmatched_org",
                "stage6_unmatched_org",
                org,
                None,
                0.0,
                review_required=True,
                review_reason="No deterministic match found for org row.",
            )
        )
        group_id += 1
    for party in sorted(unmatched_party.values(), key=lambda r: r.row_id):
        records.append(
            _make_record(
                group_id,
                "unmatched_party",
                "stage7_unmatched_party",
                None,
                party,
                0.0,
                review_required=True,
                review_reason="No deterministic match found for party row.",
            )
        )
        group_id += 1

    strong = sum(1 for r in records if r.match_status == "matched_strong")
    review = sum(1 for r in records if r.review_required)
    return MatchResult(
        records=records,
        strong_match_count=strong,
        review_match_count=review,
        unmatched_org_count=sum(1 for r in records if r.match_status == "unmatched_org"),
        unmatched_party_count=sum(1 for r in records if r.match_status == "unmatched_party"),
    )

