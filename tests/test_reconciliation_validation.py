"""Validation_Report must flag critical integrity issues, not hide them."""

from __future__ import annotations

from pathlib import Path

from src.reconciliation.recon_models import MatchRecord, ReconRow
from src.reconciliation.validation import build_validation_items


def _tx(row_id: str, role: str, nref: str) -> ReconRow:
    return ReconRow(
        data={
            "row_id": row_id,
            "record_kind": "Transaction",
            "ledger_role": role,
            "normalized_reference": nref,
            "type_label": "Invoice",
        },
        sheet_name=role,
    )


def _items(matches, org_rows=None, party_rows=None):
    return build_validation_items(
        pair_id="pair_x",
        formalized_path=Path("nonexistent.xlsx"),
        reference_workbook_found=True,
        labels=["Invoice", "Payment"],
        org_rows=org_rows or [],
        party_rows=party_rows or [],
        matches=matches,
        formula_audit_failures=0,
    )


def _status_of(items, check: str) -> str:
    for item in items:
        if item.check == check:
            return item.status
    raise AssertionError(f"check {check!r} not found")


def _count_of(items, check: str) -> int:
    for item in items:
        if item.check == check:
            return int(item.count)
    raise AssertionError(f"check {check!r} not found")


def test_missing_reference_strong_match_is_fail() -> None:
    bad = MatchRecord(
        match_group_id="MG1",
        match_status="matched_strong",
        match_confidence=1.0,
        match_rule="stage2_exact_ref_amount",
        type_label="Invoice",
        org_normalized_reference="None",
        party_normalized_reference="None",
    )
    items = _items([bad])
    assert _status_of(items, "strong_matches_missing_reference") == "FAIL"
    assert _count_of(items, "strong_matches_missing_reference") == 1


def test_clean_strong_match_passes() -> None:
    good = MatchRecord(
        match_group_id="MG1",
        match_status="matched_strong",
        match_confidence=1.0,
        match_rule="stage2_exact_ref_amount",
        type_label="Invoice",
        org_normalized_reference="ABC123",
        party_normalized_reference="ABC123",
    )
    items = _items([good])
    assert _status_of(items, "strong_matches_missing_reference") == "PASS"


def test_formalized_workbook_missing_is_fail() -> None:
    items = _items([])
    assert _status_of(items, "formalized_workbook_exists") == "FAIL"


def test_unknown_transaction_rows_flagged() -> None:
    org = [
        ReconRow(
            data={"row_id": "o1", "record_kind": "Transaction", "type_label": "Unknown"},
            sheet_name="org",
        )
    ]
    items = _items([], org_rows=org)
    assert _count_of(items, "transaction_rows_type_unknown") == 1
    assert _status_of(items, "transaction_rows_type_unknown") == "REVIEW"
