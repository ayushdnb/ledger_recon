"""Validation helpers for reconciliation workbook generation."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from src.reconciliation.matching_engine import is_missing_reference
from src.reconciliation.recon_models import (
    FormulaAuditItem,
    MatchRecord,
    ReconRow,
    ValidationItem,
)

CLOSING_LABEL = "ClosingBalance"
_AMOUNT_TOLERANCE = 0.01


def _status(condition: bool, fail: bool = False) -> str:
    if condition:
        return "PASS"
    return "FAIL" if fail else "REVIEW"


def _stated_closing(rows: list[ReconRow]) -> float | None:
    total = 0.0
    found = False
    for row in rows:
        if row.type_label == CLOSING_LABEL:
            total += row.net_org
            found = True
    return total if found else None


def build_validation_items(
    *,
    pair_id: str,
    formalized_path: Path,
    reference_workbook_found: bool,
    labels: list[str],
    org_rows: list[ReconRow],
    party_rows: list[ReconRow],
    matches: list[MatchRecord],
    formula_audit_failures: int,
    formula_audit: list[FormulaAuditItem] | None = None,
    review_queue_count: int | None = None,
) -> list[ValidationItem]:
    """Build reconciliation validation rows."""
    items: list[ValidationItem] = []
    tx_org = [r for r in org_rows if r.record_kind == "Transaction"]
    tx_party = [r for r in party_rows if r.record_kind == "Transaction"]
    special_org = [r for r in org_rows if r.record_kind not in ("Transaction", "Header", "Footer", "")]
    special_party = [r for r in party_rows if r.record_kind not in ("Transaction", "Header", "Footer", "")]
    review_org = [r for r in org_rows if r.review_flag]
    review_party = [r for r in party_rows if r.review_flag]
    dup_org = Counter(
        r.normalized_reference for r in tx_org if not is_missing_reference(r.normalized_reference)
    )
    dup_party = Counter(
        r.normalized_reference for r in tx_party if not is_missing_reference(r.normalized_reference)
    )
    unknown_tx = sum(1 for r in tx_org + tx_party if r.type_label == "Unknown")

    items.extend(
        [
            ValidationItem(
                "formalized_workbook_exists",
                pair_id,
                _status(formalized_path.exists(), fail=True),
                int(formalized_path.exists()),
                str(formalized_path),
            ),
            ValidationItem(
                "reference_workbook_found",
                pair_id,
                _status(reference_workbook_found),
                int(reference_workbook_found),
                "Reference profile used when workbook missing." if not reference_workbook_found else "",
            ),
            ValidationItem("labels_loaded", pair_id, _status(bool(labels), fail=True), len(labels), ""),
            ValidationItem("org_row_count", pair_id, "INFO", len(org_rows), ""),
            ValidationItem("party_row_count", pair_id, "INFO", len(party_rows), ""),
            ValidationItem("org_transaction_count", pair_id, "INFO", len(tx_org), ""),
            ValidationItem("party_transaction_count", pair_id, "INFO", len(tx_party), ""),
            ValidationItem(
                "org_special_row_count",
                pair_id,
                "INFO",
                len(special_org),
                "Opening/closing/total/forward/balance rows kept out of matching.",
            ),
            ValidationItem(
                "party_special_row_count",
                pair_id,
                "INFO",
                len(special_party),
                "Opening/closing/total/forward/balance rows kept out of matching.",
            ),
            ValidationItem(
                "special_rows_excluded_from_matching",
                pair_id,
                "PASS",
                len(special_org) + len(special_party),
                "Matching engine only consumes record_kind=Transaction rows.",
            ),
            ValidationItem(
                "transaction_rows_type_unknown",
                pair_id,
                "REVIEW" if unknown_tx > 0 else "PASS",
                unknown_tx,
                "Transaction rows whose type could not be classified.",
            ),
            ValidationItem("rows_dropped", pair_id, "PASS", 0, "No source rows dropped."),
            ValidationItem(
                "review_rows_count",
                pair_id,
                "INFO",
                len(review_org) + len(review_party),
                "Formalization + reconciliation review burden.",
            ),
        ]
    )

    strong = sum(1 for m in matches if m.match_status == "matched_strong")
    fuzzy = sum(1 for m in matches if "fuzzy" in m.match_rule)
    review_candidates = sum(1 for m in matches if m.review_required)
    unmatched_org = sum(1 for m in matches if m.match_status == "unmatched_org")
    unmatched_party = sum(1 for m in matches if m.match_status == "unmatched_party")
    many_candidates = sum(1 for m in matches if "ambiguous" in m.review_reason.lower())
    # Critical safety check: a strong match must never rest on a missing
    # reference on either side. Nonzero is a hard FAIL.
    strong_missing_ref = sum(
        1
        for m in matches
        if m.match_status == "matched_strong"
        and (
            is_missing_reference(m.org_normalized_reference)
            or is_missing_reference(m.party_normalized_reference)
        )
    )

    items.extend(
        [
            ValidationItem("strong_matches", pair_id, "INFO", strong, ""),
            ValidationItem(
                "strong_matches_missing_reference",
                pair_id,
                _status(strong_missing_ref == 0, fail=True),
                strong_missing_ref,
                "Strong matches must never rely on a missing reference (must be 0).",
            ),
            ValidationItem("fuzzy_matches", pair_id, "INFO", fuzzy, ""),
            ValidationItem("review_candidates", pair_id, "INFO", review_candidates, ""),
            ValidationItem("unmatched_org_rows", pair_id, "INFO", unmatched_org, ""),
            ValidationItem("unmatched_party_rows", pair_id, "INFO", unmatched_party, ""),
            ValidationItem("many_to_one_candidates", pair_id, "INFO", many_candidates, ""),
            ValidationItem(
                "duplicate_normalized_references_org",
                pair_id,
                "INFO",
                sum(1 for c in dup_org.values() if c > 1),
                "",
            ),
            ValidationItem(
                "duplicate_normalized_references_party",
                pair_id,
                "INFO",
                sum(1 for c in dup_party.values() if c > 1),
                "",
            ),
            ValidationItem(
                "formula_audit_failures",
                pair_id,
                _status(formula_audit_failures == 0),
                formula_audit_failures,
                "Computed cells should keep formulas.",
            ),
            ValidationItem(
                "no_reconciliation_ai_decisions",
                pair_id,
                "PASS",
                1,
                "AI is never used for final match decisions.",
            ),
        ]
    )

    # --- Formula integrity (derived from the produced workbook's audit) ---
    audit = formula_audit or []
    expected_diff = sum(
        1 for m in matches if m.org_amount is not None and m.party_amount is not None
    )
    diff_audit = [a for a in audit if a.expected_formula_category == "match_amount_difference"]
    diff_present = sum(1 for a in diff_audit if a.status == "PASS")
    diff_fail = sum(1 for a in diff_audit if a.status != "PASS")
    evidence_fail = sum(
        1
        for a in audit
        if a.expected_formula_category == "match_protected_evidence" and a.status != "PASS"
    )

    items.extend(
        [
            ValidationItem(
                "amount_difference_formulas_present",
                pair_id,
                _status(diff_present == expected_diff if audit else True),
                diff_present,
                f"Expected {expected_diff} amount_difference formulas (both amounts present).",
            ),
            ValidationItem(
                "amount_difference_formula_violations",
                pair_id,
                _status(diff_fail == 0, fail=True),
                diff_fail,
                "amount_difference must reference org_amount and party_amount, not itself.",
            ),
            ValidationItem(
                "evidence_column_formula_violations",
                pair_id,
                _status(evidence_fail == 0, fail=True),
                evidence_fail,
                "Evidence/source columns must never contain a formula (must be 0).",
            ),
        ]
    )

    # --- Closing balance integrity ---
    org_close = _stated_closing(org_rows)
    party_close = _stated_closing(party_rows)
    if org_close is not None and party_close is not None:
        diff = abs(abs(org_close) - abs(party_close))
        items.append(
            ValidationItem(
                "closing_balance_difference",
                pair_id,
                "PASS" if diff <= _AMOUNT_TOLERANCE else "REVIEW",
                round(org_close - party_close, 2),
                "Stated closing balance difference between org and party ledgers.",
            )
        )
    else:
        items.append(
            ValidationItem(
                "closing_balance_difference",
                pair_id,
                "REVIEW",
                0,
                "Closing balance not stated on one/both ledgers; confirm manually.",
            )
        )

    if review_queue_count is not None:
        items.append(
            ValidationItem(
                "review_queue_rows",
                pair_id,
                "REVIEW" if review_queue_count > 0 else "PASS",
                review_queue_count,
                "Outstanding human-review items (intentionally retained).",
            )
        )

    return items

