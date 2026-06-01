"""Deterministic matching engine for reconciliation candidates.

Staged pipeline (deterministic-first):
  1. Data quality: transaction rows only; opening/closing excluded upstream.
  2. Exact normalized reference + amount + polarity/type.
  3. Exact reference with date tolerance (timing difference).
  4. Exact reference with amount (type-relaxed).
  5. Mutually unique containment-reference matches.
  6. Exact amount/date/type singleton match.
  7. Supported unique mirror amount+date+type.
  8. Credit/debit note netting.
  9. Rounding-difference classification (within rounding tolerance).
 10. One-to-many allocation.
 11. Many-to-one allocation.
 12. Bounded many-to-many allocation (review-aware).
 13. Duplicate reference detection (review).
 14. Fuzzy/containment weak candidates -> review.
 15. Missing-reference amount/date candidates -> review.
 16. Unmatched org / unmatched party with issue classification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Iterable

from src.reconciliation.allocation_engine import (
    AllocationGroup,
    detect_duplicate_references,
    find_credit_debit_netting,
    find_many_to_many_allocations,
    find_many_to_one_allocations,
    find_one_to_many_allocations,
)
from src.reconciliation.issue_taxonomy import (
    PrimaryIssueCode,
    classify_amount_residual,
    classify_match_record,
    classify_unmatched_row,
    ensure_codes_on_records,
)
from src.reconciliation.recon_models import (
    FinalMatchDecisionSource,
    MatchRecord,
    ReconRow,
)
from src.reconciliation.tolerance_config import (
    DEFAULT_AMOUNT_TOLERANCE,
    DEFAULT_DATE_TOLERANCE_DAYS,
    ReconTolerancePolicy,
    build_tolerance_policy,
)

from src.reconciliation.matching_helpers import (
    amount_close as _amount_close,
    is_mirror_polarity_plausible,
    is_missing_reference,
    is_type_compatible,
)


ACCEPTED_DETERMINISTIC_STATUSES: frozenset[str] = frozenset(
    {"matched_strong", "matched_supported"}
)

MISSING_REFERENCE_REASON = (
    "Missing normalized reference on at least one side; no deterministic "
    "reference evidence. Amount/date/type candidate only."
)


@dataclass
class MatchResult:
    """Matching output plus convenience stats."""

    records: list[MatchRecord]
    strong_match_count: int
    supported_match_count: int
    review_match_count: int
    unmatched_org_count: int
    unmatched_party_count: int


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


def _reference_relation(
    org_ref: str,
    party_ref: str,
    *,
    fuzzy_threshold: float = 0.85,
) -> tuple[str, float]:
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
    if score >= fuzzy_threshold:
        return "fuzzy", score
    return "ambiguous", score


def _candidate_sort_key(org: ReconRow, party: ReconRow, *, fuzzy_threshold: float) -> tuple:
    relation, rel_score = _reference_relation(
        org.normalized_reference, party.normalized_reference, fuzzy_threshold=fuzzy_threshold
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
    *,
    org_rows: list[ReconRow] | None = None,
    party_rows: list[ReconRow] | None = None,
    amount_tolerance_used: float | None = None,
    date_tolerance_days_used: int | None = None,
    primary_issue_code: str = "",
) -> MatchRecord:
    org_list = org_rows or ([org] if org else [])
    party_list = party_rows or ([party] if party else [])
    org = org or (org_list[0] if org_list else None)
    party = party or (party_list[0] if party_list else None)
    org_amount = sum(r.net_org for r in org_list) if org_list else None
    party_amount = sum(r.net_org for r in party_list) if party_list else None
    delta = None
    if org_amount is not None and party_amount is not None:
        delta = abs(abs(org_amount) - abs(party_amount))
    rel, _ = _reference_relation(
        org.normalized_reference if org else "",
        party.normalized_reference if party else "",
    )
    match_group_id = f"MG{group_id:06d}"
    record = MatchRecord(
        match_group_id=match_group_id,
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
        decision_id=match_group_id,
        decision_source=(
            FinalMatchDecisionSource.HUMAN_REVIEW_REQUIRED.value
            if review_required
            else FinalMatchDecisionSource.DETERMINISTIC.value
        ),
        match_type=rule,
        org_row_ids=[r.row_id for r in org_list],
        party_row_ids=[r.row_id for r in party_list],
        compact_explanation=review_reason or "Accepted by deterministic reconciliation rule.",
        validation_status="REVIEW" if review_required else "ACCEPTED",
        amount_tolerance_used=amount_tolerance_used,
        date_tolerance_days_used=date_tolerance_days_used,
        primary_issue_code=primary_issue_code,
    )
    if not record.primary_issue_code:
        code, tags = classify_match_record(record)
        record.primary_issue_code = code
        record.secondary_issue_tags = tags
    return record


def _record_from_allocation(group_id: int, alloc: AllocationGroup, org_by_id: dict, party_by_id: dict) -> MatchRecord:
    org_rows = [org_by_id[i] for i in alloc.org_row_ids]
    party_rows = [party_by_id[i] for i in alloc.party_row_ids]
    status = "matched_supported" if not alloc.review_required else "candidate_review"
    return _make_record(
        group_id,
        status,
        alloc.rule,
        org_rows[0] if org_rows else None,
        party_rows[0] if party_rows else None,
        alloc.confidence,
        review_required=alloc.review_required,
        review_reason=alloc.review_reason,
        org_rows=org_rows,
        party_rows=party_rows,
        amount_tolerance_used=alloc.amount_tolerance_used,
        date_tolerance_days_used=alloc.date_tolerance_days_used,
    )


def _transaction_rows(rows: Iterable[ReconRow]) -> list[ReconRow]:
    return [r for r in rows if r.record_kind == "Transaction"]


def reconcile_rows(
    org_rows: list[ReconRow],
    party_rows: list[ReconRow],
    *,
    amount_tolerance: float | None = None,
    date_tolerance_days: int | None = None,
    tolerance_policy: ReconTolerancePolicy | None = None,
) -> MatchResult:
    """Run deterministic staged matching and candidate generation."""
    policy = tolerance_policy or build_tolerance_policy(
        amount_tolerance=amount_tolerance or DEFAULT_AMOUNT_TOLERANCE,
        date_tolerance_days=date_tolerance_days or DEFAULT_DATE_TOLERANCE_DAYS,
    )
    amt_tol = policy.amount_tolerance
    date_tol = policy.date_tolerance_days
    fuzzy_threshold = policy.fuzzy_reference_threshold

    org_tx = sorted(_transaction_rows(org_rows), key=lambda r: r.row_id)
    party_tx = sorted(_transaction_rows(party_rows), key=lambda r: r.row_id)
    unmatched_org = {r.row_id: r for r in org_tx}
    unmatched_party = {r.row_id: r for r in party_tx}

    records: list[MatchRecord] = []
    group_id = 1
    represented_org_ids: set[str] = set()
    represented_party_ids: set[str] = set()

    def consume(
        org: ReconRow,
        party: ReconRow,
        status: str,
        rule: str,
        conf: float,
        *,
        org_rows: list[ReconRow] | None = None,
        party_rows: list[ReconRow] | None = None,
        amount_tol_used: float | None = None,
        date_tol_used: int | None = None,
    ) -> None:
        nonlocal group_id
        ptol = policy.pair_tolerance(org.type_label, party.type_label)
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
                org_rows=org_rows,
                party_rows=party_rows,
                amount_tolerance_used=amount_tol_used or ptol.amount_tolerance,
                date_tolerance_days_used=date_tol_used or ptol.date_tolerance_days,
            )
        )
        group_id += 1
        for r in (org_rows or [org]):
            unmatched_org.pop(r.row_id, None)
        for r in (party_rows or [party]):
            unmatched_party.pop(r.row_id, None)

    def add_review_candidate(
        org: ReconRow,
        party: ReconRow,
        rule: str,
        confidence: float,
        reason: str,
        *,
        issue_code: str = PrimaryIssueCode.AMBIGUOUS_MATCH.value,
    ) -> None:
        nonlocal group_id
        ptol = policy.pair_tolerance(org.type_label, party.type_label)
        rec = _make_record(
            group_id,
            "candidate_review",
            rule,
            org,
            party,
            confidence,
            review_required=True,
            review_reason=reason,
            amount_tolerance_used=ptol.amount_tolerance,
            date_tolerance_days_used=ptol.date_tolerance_days,
            primary_issue_code=issue_code,
        )
        records.append(rec)
        group_id += 1
        represented_org_ids.add(org.row_id)
        represented_party_ids.add(party.row_id)

    def apply_allocations(allocs: list[AllocationGroup]) -> None:
        nonlocal group_id
        org_by_id = {r.row_id: r for r in unmatched_org.values()}
        party_by_id = {r.row_id: r for r in unmatched_party.values()}
        for alloc in allocs:
            if any(i not in org_by_id for i in alloc.org_row_ids):
                continue
            if any(i not in party_by_id for i in alloc.party_row_ids):
                continue
            records.append(_record_from_allocation(group_id, alloc, org_by_id, party_by_id))
            group_id += 1
            for i in alloc.org_row_ids:
                unmatched_org.pop(i, None)
                org_by_id.pop(i, None)
            for i in alloc.party_row_ids:
                unmatched_party.pop(i, None)
                party_by_id.pop(i, None)

    # Stage 2: exact ref + amount (+ optional type).
    for org in list(unmatched_org.values()):
        if is_missing_reference(org.normalized_reference):
            continue
        ptol = policy.pair_tolerance(org.type_label, org.type_label)
        candidates = [
            p
            for p in unmatched_party.values()
            if not is_missing_reference(p.normalized_reference)
            and p.normalized_reference == org.normalized_reference
            and _amount_close(org.net_org, p.net_org, ptol.amount_tolerance)
            and (_date_delta_days(org.date, p.date) in (None, 0))
            and is_mirror_polarity_plausible(org, p)
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
                "stage2_exact_ref_type_amount" if typed else "stage2_exact_ref_amount",
                1.0 if typed else 0.95,
            )
            continue
        best = sorted(
            pool,
            key=lambda p: _candidate_sort_key(org, p, fuzzy_threshold=fuzzy_threshold),
            reverse=True,
        )
        add_review_candidate(
            org,
            best[0],
            "stage2_ambiguous_exact_ref",
            0.7,
            "Multiple exact-reference candidates; deterministic tie unresolved.",
        )

    # Stage 3: exact ref + amount with date tolerance (timing difference).
    for org in list(unmatched_org.values()):
        if is_missing_reference(org.normalized_reference):
            continue
        ptol = policy.pair_tolerance(org.type_label, org.type_label)
        candidates = []
        for party in unmatched_party.values():
            if is_missing_reference(party.normalized_reference):
                continue
            if party.normalized_reference != org.normalized_reference:
                continue
            if not _amount_close(org.net_org, party.net_org, ptol.amount_tolerance):
                continue
            date_delta = _date_delta_days(org.date, party.date)
            if date_delta is not None and date_delta > ptol.date_tolerance_days:
                continue
            if not is_type_compatible(org.type_label, party.type_label):
                continue
            if not is_mirror_polarity_plausible(org, party):
                continue
            candidates.append(party)
        if len(candidates) != 1:
            continue
        party = candidates[0]
        date_delta = _date_delta_days(org.date, party.date) or 0
        rule = (
            "stage3_timing_difference_exact_ref_amount"
            if date_delta > 0
            else "stage2_exact_ref_type_amount"
        )
        consume(org, party, "matched_strong", rule, 0.92 if date_delta > 0 else 1.0)

    # Stage 5: mutually unique containment-reference matches.
    for org in list(unmatched_org.values()):
        if is_missing_reference(org.normalized_reference):
            continue
        ptol = policy.pair_tolerance(org.type_label, org.type_label)
        candidates = []
        for party in unmatched_party.values():
            relation, _ = _reference_relation(
                org.normalized_reference,
                party.normalized_reference,
                fuzzy_threshold=fuzzy_threshold,
            )
            date_delta = _date_delta_days(org.date, party.date)
            if (
                not is_missing_reference(party.normalized_reference)
                and relation in {"contains", "contained_by"}
                and _amount_close(org.net_org, party.net_org, ptol.amount_tolerance)
                and (date_delta is None or date_delta <= ptol.date_tolerance_days)
                and is_type_compatible(org.type_label, party.type_label)
                and is_mirror_polarity_plausible(org, party)
            ):
                candidates.append(party)
        if len(candidates) != 1:
            continue
        party = candidates[0]
        reverse_candidates = []
        for other_org in unmatched_org.values():
            relation, _ = _reference_relation(
                other_org.normalized_reference,
                party.normalized_reference,
                fuzzy_threshold=fuzzy_threshold,
            )
            date_delta = _date_delta_days(other_org.date, party.date)
            if (
                not is_missing_reference(other_org.normalized_reference)
                and relation in {"contains", "contained_by"}
                and _amount_close(other_org.net_org, party.net_org, ptol.amount_tolerance)
                and (date_delta is None or date_delta <= ptol.date_tolerance_days)
                and is_type_compatible(other_org.type_label, party.type_label)
                and is_mirror_polarity_plausible(other_org, party)
            ):
                reverse_candidates.append(other_org)
        if len(reverse_candidates) == 1:
            consume(org, party, "matched_strong", "stage5_containment_ref_amount", 0.9)

    # Stage 7: supported unique mirror amount+date+type.
    def supported_candidates(org: ReconRow) -> list[ReconRow]:
        ptol = policy.pair_tolerance(org.type_label, org.type_label)
        return [
            party
            for party in unmatched_party.values()
            if _amount_close(org.net_org, party.net_org, ptol.amount_tolerance)
            and _date_delta_days(org.date, party.date) == 0
            and is_type_compatible(org.type_label, party.type_label)
            and is_mirror_polarity_plausible(org, party)
        ]

    by_org = {org.row_id: supported_candidates(org) for org in unmatched_org.values()}
    by_party: dict[str, list[ReconRow]] = {party.row_id: [] for party in unmatched_party.values()}
    for org in unmatched_org.values():
        for party in by_org[org.row_id]:
            by_party[party.row_id].append(org)
    for org in list(unmatched_org.values()):
        candidates = by_org[org.row_id]
        if len(candidates) != 1:
            continue
        party = candidates[0]
        if len(by_party[party.row_id]) == 1:
            consume(
                org,
                party,
                "matched_supported",
                "stage7_supported_unique_mirror_amount_date_type",
                0.85,
            )

    # Stage 8: credit/debit note netting.
    apply_allocations(
        find_credit_debit_netting(
            list(unmatched_org.values()),
            list(unmatched_party.values()),
            policy,
        )
    )

    # Stage 9: rounding difference (exact ref, amount within rounding tolerance only).
    for org in list(unmatched_org.values()):
        if is_missing_reference(org.normalized_reference):
            continue
        ptol = policy.pair_tolerance(org.type_label, org.type_label)
        for party in list(unmatched_party.values()):
            if party.normalized_reference != org.normalized_reference:
                continue
            if _amount_close(org.net_org, party.net_org, ptol.amount_tolerance):
                continue
            if not _amount_close(org.net_org, party.net_org, ptol.rounding_tolerance):
                continue
            if not is_mirror_polarity_plausible(org, party):
                continue
            delta = abs(abs(org.net_org) - abs(party.net_org))
            consume(
                org,
                party,
                "matched_supported",
                "stage9_rounding_difference",
                0.80,
                amount_tol_used=ptol.rounding_tolerance,
            )
            break

    # Stages 10-12: allocations.
    apply_allocations(
        find_one_to_many_allocations(
            list(unmatched_org.values()), list(unmatched_party.values()), policy
        )
    )
    apply_allocations(
        find_many_to_one_allocations(
            list(unmatched_org.values()), list(unmatched_party.values()), policy
        )
    )
    apply_allocations(
        find_many_to_many_allocations(
            list(unmatched_org.values()), list(unmatched_party.values()), policy
        )
    )

    # Stage 13: exact-reference residuals -> classified review (never auto-match).
    for org in list(unmatched_org.values()):
        if is_missing_reference(org.normalized_reference):
            continue
        ptol = policy.pair_tolerance(org.type_label, org.type_label)
        candidates = [
            party
            for party in unmatched_party.values()
            if party.normalized_reference == org.normalized_reference
            and is_type_compatible(org.type_label, party.type_label)
            and is_mirror_polarity_plausible(org, party)
            and (
                _date_delta_days(org.date, party.date) is None
                or _date_delta_days(org.date, party.date) <= ptol.date_tolerance_days
            )
            and not _amount_close(org.net_org, party.net_org, ptol.rounding_tolerance)
        ]
        if len(candidates) != 1:
            continue
        party = candidates[0]
        reverse = [
            other
            for other in unmatched_org.values()
            if other.normalized_reference == party.normalized_reference
            and is_type_compatible(other.type_label, party.type_label)
            and is_mirror_polarity_plausible(other, party)
        ]
        if len(reverse) != 1:
            continue
        residual = abs(abs(org.net_org) - abs(party.net_org))
        issue = classify_amount_residual(
            org.net_org,
            party.net_org,
            residual=residual,
            org_label=org.type_label,
            party_label=party.type_label,
            write_off_threshold=ptol.small_write_off_threshold,
            bank_charge_threshold=ptol.bank_charge_threshold,
            discount_threshold=ptol.discount_threshold,
        )
        add_review_candidate(
            org,
            party,
            "stage13_exact_reference_amount_residual_review",
            0.55,
            (
                f"Exact-reference amount residual {residual:.2f} classified as "
                f"{issue}; accountant review required."
            ),
            issue_code=issue,
        )

    # Stage 14: duplicate reference detection -> review (do not auto-match).
    org_dupes = detect_duplicate_references(list(unmatched_org.values()), side="org")
    party_dupes = detect_duplicate_references(list(unmatched_party.values()), side="party")
    for ref, ids in org_dupes.items():
        for row_id in ids:
            if row_id in represented_org_ids or row_id not in unmatched_org:
                continue
            org = unmatched_org[row_id]
            represented_org_ids.add(row_id)
            records.append(
                _make_record(
                    group_id,
                    "candidate_review",
                    "stage13_duplicate_reference_org",
                    org,
                    None,
                    0.5,
                    review_required=True,
                    review_reason=f"Duplicate normalized reference '{ref}' on org side.",
                    primary_issue_code=PrimaryIssueCode.DUPLICATE_TRANSACTION.value,
                )
            )
            group_id += 1
    for ref, ids in party_dupes.items():
        for row_id in ids:
            if row_id in represented_party_ids or row_id not in unmatched_party:
                continue
            party = unmatched_party[row_id]
            represented_party_ids.add(row_id)
            records.append(
                _make_record(
                    group_id,
                    "candidate_review",
                    "stage13_duplicate_reference_party",
                    None,
                    party,
                    0.5,
                    review_required=True,
                    review_reason=f"Duplicate normalized reference '{ref}' on party side.",
                    primary_issue_code=PrimaryIssueCode.DUPLICATE_TRANSACTION.value,
                )
            )
            group_id += 1

    # Stages 15-16: weak candidates on still unmatched org rows.
    for org in list(unmatched_org.values()):
        if org.row_id in represented_org_ids:
            continue
        org_ref_missing = is_missing_reference(org.normalized_reference)
        ptol = policy.pair_tolerance(org.type_label, org.type_label)
        scored: list[tuple[tuple, ReconRow]] = []
        for party in unmatched_party.values():
            if not _amount_close(org.net_org, party.net_org, ptol.amount_tolerance):
                continue
            if not is_mirror_polarity_plausible(org, party):
                continue
            party_ref_missing = is_missing_reference(party.normalized_reference)
            relation, rel_score = _reference_relation(
                org.normalized_reference,
                party.normalized_reference,
                fuzzy_threshold=fuzzy_threshold,
            )
            date_delta = _date_delta_days(org.date, party.date)
            compatible_date = date_delta is None or date_delta <= ptol.date_tolerance_days
            type_ok = is_type_compatible(org.type_label, party.type_label)
            if org_ref_missing or party_ref_missing:
                if compatible_date and type_ok:
                    scored.append(
                        (_candidate_sort_key(org, party, fuzzy_threshold=fuzzy_threshold), party)
                    )
            elif relation in {"contains", "contained_by"}:
                if compatible_date and type_ok:
                    scored.append(
                        (_candidate_sort_key(org, party, fuzzy_threshold=fuzzy_threshold), party)
                    )
            elif relation == "fuzzy" and compatible_date and type_ok:
                scored.append(
                    (_candidate_sort_key(org, party, fuzzy_threshold=fuzzy_threshold), party)
                )
        if not scored:
            continue
        scored.sort(key=lambda x: x[0], reverse=True)
        top_party = scored[0][1]
        top_party_ref_missing = is_missing_reference(top_party.normalized_reference)
        rel, _ = _reference_relation(
            org.normalized_reference,
            top_party.normalized_reference,
            fuzzy_threshold=fuzzy_threshold,
        )
        if org_ref_missing or top_party_ref_missing:
            rule = "stage15_no_ref_amount_date"
            reason = MISSING_REFERENCE_REASON
            confidence = 0.4
            issue = PrimaryIssueCode.DATA_QUALITY_ISSUE.value
        elif rel == "fuzzy":
            rule = "stage14_fuzzy_ref_amount_date"
            reason = "Fuzzy/weak reference candidate requires manual review."
            confidence = 0.6
            issue = PrimaryIssueCode.REFERENCE_MISMATCH.value
        elif rel in {"contains", "contained_by"}:
            rule = "stage14_ambiguous_containment_ref_amount"
            reason = "Containment-reference candidate is not mutually unique; manual review required."
            confidence = 0.6
            issue = PrimaryIssueCode.AMBIGUOUS_MATCH.value
        else:
            rule = "stage15_no_ref_amount_date"
            reason = MISSING_REFERENCE_REASON
            confidence = 0.4
            issue = PrimaryIssueCode.DATA_QUALITY_ISSUE.value
        add_review_candidate(org, top_party, rule, confidence, reason, issue_code=issue)

    # Stage 17-18: unmatched rows.
    for org in sorted(unmatched_org.values(), key=lambda r: r.row_id):
        if org.row_id in represented_org_ids:
            continue
        issue = classify_unmatched_row(org, side="org")
        records.append(
            _make_record(
                group_id,
                "unmatched_org",
                "stage16_unmatched_org",
                org,
                None,
                0.0,
                review_required=True,
                review_reason="No deterministic match found for org row.",
                primary_issue_code=issue,
            )
        )
        group_id += 1
    for party in sorted(unmatched_party.values(), key=lambda r: r.row_id):
        if party.row_id in represented_party_ids:
            continue
        issue = classify_unmatched_row(party, side="party")
        records.append(
            _make_record(
                group_id,
                "unmatched_party",
                "stage17_unmatched_party",
                None,
                party,
                0.0,
                review_required=True,
                review_reason="No deterministic match found for party row.",
                primary_issue_code=issue,
            )
        )
        group_id += 1

    ensure_codes_on_records(records)

    strong = sum(1 for r in records if r.match_status == "matched_strong")
    supported = sum(1 for r in records if r.match_status == "matched_supported")
    review = sum(1 for r in records if r.review_required)
    return MatchResult(
        records=records,
        strong_match_count=strong,
        supported_match_count=supported,
        review_match_count=review,
        unmatched_org_count=sum(1 for r in records if r.match_status == "unmatched_org"),
        unmatched_party_count=sum(1 for r in records if r.match_status == "unmatched_party"),
    )
