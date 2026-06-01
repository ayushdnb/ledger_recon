"""Deterministic bounded allocation for one-to-many / many-to-one / many-to-many groups.

Uses candidate blocking and subset-sum search with strict caps. Falls back to review
when ambiguous. Every allocation validates row ownership and signed totals.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Callable

from src.reconciliation.matching_helpers import (
    amount_close as _amount_close,
    is_mirror_polarity_plausible,
    is_missing_reference,
    is_type_compatible,
)
from src.reconciliation.recon_models import ReconRow
from src.reconciliation.tolerance_config import LabelTolerance, ReconTolerancePolicy


@dataclass(frozen=True)
class AllocationGroup:
    """One deterministic allocation result."""

    org_row_ids: list[str]
    party_row_ids: list[str]
    rule: str
    confidence: float
    amount_delta: float
    review_required: bool
    review_reason: str
    amount_tolerance_used: float
    date_tolerance_days_used: int


def _amount_close(a: float, b: float, tolerance: float) -> bool:
    return abs(abs(a) - abs(b)) <= tolerance


def _parse_date_key(text: str) -> tuple[int, int, int]:
    raw = (text or "")[:10]
    try:
        parts = raw.split("-")
        return int(parts[0]), int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        return (9999, 12, 31)


def _date_delta_days(a: str, b: str) -> int | None:
    if not a or not b:
        return None
    ka, kb = _parse_date_key(a), _parse_date_key(b)
    try:
        from datetime import datetime

        da = datetime(ka[0], ka[1], ka[2])
        db = datetime(kb[0], kb[1], kb[2])
        return abs((da - db).days)
    except ValueError:
        return None


def _within_date_window(
    anchor: ReconRow,
    candidate: ReconRow,
    tolerance_days: int,
) -> bool:
    delta = _date_delta_days(anchor.date, candidate.date)
    return delta is None or delta <= tolerance_days


def _block_candidates(
    anchor: ReconRow,
    pool: list[ReconRow],
    *,
    tolerance: LabelTolerance,
    same_side: bool,
) -> list[ReconRow]:
    """Block candidates by date window, type compatibility, and polarity."""
    blocked: list[ReconRow] = []
    for row in pool:
        if same_side:
            if not _within_date_window(anchor, row, tolerance.date_tolerance_days):
                continue
            if row.type_label != anchor.type_label and not is_type_compatible(
                anchor.type_label, row.type_label
            ):
                continue
            blocked.append(row)
        else:
            if not _within_date_window(anchor, row, tolerance.date_tolerance_days):
                continue
            if not is_type_compatible(anchor.type_label, row.type_label):
                continue
            if not is_mirror_polarity_plausible(anchor, row):
                continue
            blocked.append(row)
    return blocked


def _subset_sum_solutions(
    target: float,
    candidates: list[ReconRow],
    *,
    tolerance: float,
    max_size: int,
) -> list[list[ReconRow]]:
    """Return bounded subsets whose |sum(net_org)| matches target within tolerance."""
    if max_size <= 0 or not candidates:
        return []
    n = min(len(candidates), max_size + 4)
    limited = candidates[:n]
    solutions: list[list[ReconRow]] = []
    for size in range(1, min(max_size, len(limited)) + 1):
        for combo in combinations(limited, size):
            total = sum(r.net_org for r in combo)
            if _amount_close(target, total, tolerance):
                solutions.append(list(combo))
    return solutions


def _unique_rows(groups: list[list[ReconRow]]) -> list[ReconRow]:
    by_id = {row.row_id: row for group in groups for row in group}
    return [by_id[row_id] for row_id in sorted(by_id)]


def _unique_one_to_one_by_amount_date(
    org_rows: list[ReconRow],
    party_rows: list[ReconRow],
    policy: ReconTolerancePolicy,
) -> list[AllocationGroup]:
    """Singleton exact amount+date matches (stage 5 helper)."""
    groups: list[AllocationGroup] = []
    used_party: set[str] = set()
    for org in org_rows:
        tol = policy.pair_tolerance(org.type_label, org.type_label)
        matches = [
            p
            for p in party_rows
            if p.row_id not in used_party
            and _amount_close(org.net_org, p.net_org, tol.amount_tolerance)
            and _date_delta_days(org.date, p.date) == 0
            and is_type_compatible(org.type_label, p.type_label)
            and is_mirror_polarity_plausible(org, p)
        ]
        if len(matches) != 1:
            continue
        party = matches[0]
        reverse = [
            o
            for o in org_rows
            if _amount_close(o.net_org, party.net_org, tol.amount_tolerance)
            and _date_delta_days(o.date, party.date) == 0
            and is_type_compatible(o.type_label, party.type_label)
            and is_mirror_polarity_plausible(o, party)
        ]
        if len(reverse) != 1:
            continue
        used_party.add(party.row_id)
        ptol = policy.pair_tolerance(org.type_label, party.type_label)
        groups.append(
            AllocationGroup(
                org_row_ids=[org.row_id],
                party_row_ids=[party.row_id],
                rule="stage5_exact_amount_date_singleton",
                confidence=0.88,
                amount_delta=abs(abs(org.net_org) - abs(party.net_org)),
                review_required=False,
                review_reason="",
                amount_tolerance_used=ptol.amount_tolerance,
                date_tolerance_days_used=ptol.date_tolerance_days,
            )
        )
    return groups


def find_one_to_many_allocations(
    org_rows: list[ReconRow],
    party_rows: list[ReconRow],
    policy: ReconTolerancePolicy,
) -> list[AllocationGroup]:
    """One org invoice matched to multiple party payments (bounded subset-sum)."""
    groups: list[AllocationGroup] = []
    used_party: set[str] = set()
    for org in org_rows:
        tol = policy.pair_tolerance(org.type_label, "Payment")
        available = [p for p in party_rows if p.row_id not in used_party]
        blocked = _block_candidates(org, available, tolerance=tol, same_side=False)
        if len(blocked) < 2:
            continue
        cap = min(tol.max_combination_search_size, policy.max_combination_search_size)
        solutions = _subset_sum_solutions(
            org.net_org, blocked, tolerance=tol.amount_tolerance, max_size=cap
        )
        if len(solutions) > 1:
            ambiguous = _unique_rows(solutions)
            used_party.update(row.row_id for row in ambiguous)
            groups.append(
                AllocationGroup(
                    org_row_ids=[org.row_id],
                    party_row_ids=[row.row_id for row in ambiguous],
                    rule="stage14_ambiguous_one_to_many_allocation",
                    confidence=0.40,
                    amount_delta=abs(
                        abs(org.net_org) - abs(sum(row.net_org for row in ambiguous))
                    ),
                    review_required=True,
                    review_reason="Multiple bounded one-to-many allocations have equal evidence.",
                    amount_tolerance_used=tol.amount_tolerance,
                    date_tolerance_days_used=tol.date_tolerance_days,
                )
            )
            continue
        if not solutions:
            if len(blocked) > cap + 4:
                limited = blocked[: cap + 4]
                used_party.update(row.row_id for row in limited)
                groups.append(
                    AllocationGroup(
                        org_row_ids=[org.row_id],
                        party_row_ids=[row.row_id for row in limited],
                        rule="stage14_one_to_many_search_limit_review",
                        confidence=0.0,
                        amount_delta=abs(
                            abs(org.net_org) - abs(sum(row.net_org for row in limited))
                        ),
                        review_required=True,
                        review_reason="One-to-many allocation exceeded the bounded search candidate limit.",
                        amount_tolerance_used=tol.amount_tolerance,
                        date_tolerance_days_used=tol.date_tolerance_days,
                    )
                )
            continue
        subset = solutions[0]
        for p in subset:
            used_party.add(p.row_id)
        total_party = sum(p.net_org for p in subset)
        groups.append(
            AllocationGroup(
                org_row_ids=[org.row_id],
                party_row_ids=[p.row_id for p in subset],
                rule="stage14_one_to_many_allocation",
                confidence=0.82,
                amount_delta=abs(abs(org.net_org) - abs(total_party)),
                review_required=False,
                review_reason="",
                amount_tolerance_used=tol.amount_tolerance,
                date_tolerance_days_used=tol.date_tolerance_days,
            )
        )
    return groups


def find_many_to_one_allocations(
    org_rows: list[ReconRow],
    party_rows: list[ReconRow],
    policy: ReconTolerancePolicy,
) -> list[AllocationGroup]:
    """Multiple org invoices matched to one party payment."""
    groups: list[AllocationGroup] = []
    used_org: set[str] = set()
    for party in party_rows:
        tol = policy.pair_tolerance("Invoice", party.type_label)
        available = [o for o in org_rows if o.row_id not in used_org]
        blocked = _block_candidates(party, available, tolerance=tol, same_side=False)
        if len(blocked) < 2:
            continue
        cap = min(tol.max_combination_search_size, policy.max_combination_search_size)
        solutions = _subset_sum_solutions(
            party.net_org, blocked, tolerance=tol.amount_tolerance, max_size=cap
        )
        if len(solutions) > 1:
            ambiguous = _unique_rows(solutions)
            used_org.update(row.row_id for row in ambiguous)
            groups.append(
                AllocationGroup(
                    org_row_ids=[row.row_id for row in ambiguous],
                    party_row_ids=[party.row_id],
                    rule="stage15_ambiguous_many_to_one_allocation",
                    confidence=0.40,
                    amount_delta=abs(
                        abs(sum(row.net_org for row in ambiguous)) - abs(party.net_org)
                    ),
                    review_required=True,
                    review_reason="Multiple bounded many-to-one allocations have equal evidence.",
                    amount_tolerance_used=tol.amount_tolerance,
                    date_tolerance_days_used=tol.date_tolerance_days,
                )
            )
            continue
        if not solutions:
            if len(blocked) > cap + 4:
                limited = blocked[: cap + 4]
                used_org.update(row.row_id for row in limited)
                groups.append(
                    AllocationGroup(
                        org_row_ids=[row.row_id for row in limited],
                        party_row_ids=[party.row_id],
                        rule="stage15_many_to_one_search_limit_review",
                        confidence=0.0,
                        amount_delta=abs(
                            abs(sum(row.net_org for row in limited)) - abs(party.net_org)
                        ),
                        review_required=True,
                        review_reason="Many-to-one allocation exceeded the bounded search candidate limit.",
                        amount_tolerance_used=tol.amount_tolerance,
                        date_tolerance_days_used=tol.date_tolerance_days,
                    )
                )
            continue
        subset = solutions[0]
        for o in subset:
            used_org.add(o.row_id)
        total_org = sum(o.net_org for o in subset)
        groups.append(
            AllocationGroup(
                org_row_ids=[o.row_id for o in subset],
                party_row_ids=[party.row_id],
                rule="stage15_many_to_one_allocation",
                confidence=0.82,
                amount_delta=abs(abs(total_org) - abs(party.net_org)),
                review_required=False,
                review_reason="",
                amount_tolerance_used=tol.amount_tolerance,
                date_tolerance_days_used=tol.date_tolerance_days,
            )
        )
    return groups


def find_many_to_many_allocations(
    org_rows: list[ReconRow],
    party_rows: list[ReconRow],
    policy: ReconTolerancePolicy,
) -> list[AllocationGroup]:
    """Bounded many-to-many: only when both sides have small equal-count subsets."""
    groups: list[AllocationGroup] = []
    cap = min(4, policy.max_combination_search_size)
    if cap < 2:
        return groups
    used_org: set[str] = set()
    used_party: set[str] = set()
    org_pool = [o for o in org_rows if o.row_id not in used_org][:cap + 2]
    party_pool = [p for p in party_rows if p.row_id not in used_party][:cap + 2]
    if len(org_pool) < 2 or len(party_pool) < 2:
        return groups
    tol = policy.pair_tolerance(org_pool[0].type_label, party_pool[0].type_label)
    solutions: list[tuple[list[ReconRow], list[ReconRow]]] = []
    for osize in range(2, min(cap, len(org_pool)) + 1):
        for psize in range(2, min(cap, len(party_pool)) + 1):
            for org_combo in combinations(org_pool, osize):
                org_total = sum(o.net_org for o in org_combo)
                for party_combo in combinations(party_pool, psize):
                    party_total = sum(p.net_org for p in party_combo)
                    if not _amount_close(org_total, party_total, tol.amount_tolerance):
                        continue
                    if not all(
                        is_type_compatible(o.type_label, p.type_label)
                        and is_mirror_polarity_plausible(o, p)
                        and _within_date_window(o, p, tol.date_tolerance_days)
                        for o in org_combo
                        for p in party_combo
                    ):
                        continue
                    solutions.append((list(org_combo), list(party_combo)))
    if len(solutions) > 1:
        org_rows = _unique_rows([org_combo for org_combo, _ in solutions])
        party_rows = _unique_rows([party_combo for _, party_combo in solutions])
        groups.append(
            AllocationGroup(
                org_row_ids=[row.row_id for row in org_rows],
                party_row_ids=[row.row_id for row in party_rows],
                rule="stage16_ambiguous_many_to_many_allocation",
                confidence=0.25,
                amount_delta=abs(
                    abs(sum(row.net_org for row in org_rows))
                    - abs(sum(row.net_org for row in party_rows))
                ),
                review_required=True,
                review_reason="Multiple bounded many-to-many allocations have equal evidence.",
                amount_tolerance_used=tol.amount_tolerance,
                date_tolerance_days_used=tol.date_tolerance_days,
            )
        )
        return groups
    if not solutions:
        return groups
    org_combo, party_combo = solutions[0]
    org_total = sum(o.net_org for o in org_combo)
    party_total = sum(p.net_org for p in party_combo)
    groups.append(
        AllocationGroup(
            org_row_ids=[o.row_id for o in org_combo],
            party_row_ids=[p.row_id for p in party_combo],
            rule="stage16_many_to_many_allocation",
            confidence=0.75,
            amount_delta=abs(abs(org_total) - abs(party_total)),
            review_required=True,
            review_reason="Many-to-many allocation is inherently ambiguous; manual review required.",
            amount_tolerance_used=tol.amount_tolerance,
            date_tolerance_days_used=tol.date_tolerance_days,
        )
    )
    return groups


def find_credit_debit_netting(
    org_rows: list[ReconRow],
    party_rows: list[ReconRow],
    policy: ReconTolerancePolicy,
) -> list[AllocationGroup]:
    """Net credit notes against debit notes (mirror types) with reference hints."""
    groups: list[AllocationGroup] = []
    note_pairs = (
        ("CreditNote", "DebitNote"),
        ("DebitNote", "CreditNote"),
    )
    used_org: set[str] = set()
    used_party: set[str] = set()
    for org_type, party_type in note_pairs:
        org_notes = [
            o
            for o in org_rows
            if o.type_label == org_type and o.row_id not in used_org
        ]
        party_notes = [
            p
            for p in party_rows
            if p.type_label == party_type and p.row_id not in used_party
        ]
        for org in org_notes:
            tol = policy.pair_tolerance(org_type, party_type)
            ref = org.normalized_reference
            candidates = []
            for party in party_notes:
                if party.row_id in used_party:
                    continue
                if not _amount_close(org.net_org, party.net_org, tol.amount_tolerance):
                    continue
                if ref and party.normalized_reference and ref != party.normalized_reference:
                    if ref not in party.normalized_reference and party.normalized_reference not in ref:
                        continue
                if not is_mirror_polarity_plausible(org, party):
                    continue
                candidates.append(party)
            if len(candidates) != 1:
                continue
            party = candidates[0]
            used_org.add(org.row_id)
            used_party.add(party.row_id)
            groups.append(
                AllocationGroup(
                    org_row_ids=[org.row_id],
                    party_row_ids=[party.row_id],
                    rule="stage9_credit_debit_note_netting",
                    confidence=0.90,
                    amount_delta=abs(abs(org.net_org) - abs(party.net_org)),
                    review_required=False,
                    review_reason="",
                    amount_tolerance_used=tol.amount_tolerance,
                    date_tolerance_days_used=tol.date_tolerance_days,
                )
            )
    return groups


def detect_duplicate_references(
    rows: list[ReconRow],
    *,
    side: str,
) -> dict[str, list[str]]:
    """Map normalized reference -> row_ids when reference appears more than once."""
    from src.reconciliation.matching_helpers import is_missing_reference

    buckets: dict[str, list[str]] = {}
    for row in rows:
        ref = row.normalized_reference
        if is_missing_reference(ref):
            continue
        buckets.setdefault(ref, []).append(row.row_id)
    return {ref: ids for ref, ids in buckets.items() if len(ids) > 1}
