"""Tests for reconciliation engine hardening: tolerances, taxonomy, allocation, balances."""

from __future__ import annotations

from src.config import settings
from src.formalization.reference_normalization import normalize_reference
from src.reconciliation.allocation_engine import (
    find_many_to_one_allocations,
    find_one_to_many_allocations,
)
from src.reconciliation.balance_helpers import final_closing_balance
from src.reconciliation.issue_taxonomy import (
    PrimaryIssueCode,
    is_valid_primary_code,
    normalize_ai_reason_code,
)
from src.reconciliation.matching_engine import is_missing_reference, reconcile_rows
from src.reconciliation.recon_models import ReconRow
from src.reconciliation.tolerance_config import build_tolerance_policy


def _row(
    row_id: str,
    *,
    role: str,
    ref: str = "",
    nref: str = "",
    date: str = "2025-01-01",
    type_label: str = "Invoice",
    amount: float = 100.0,
    record_kind: str = "Transaction",
    page: int = 1,
    source_row: int = 1,
) -> ReconRow:
    source_amount = amount if role.startswith("org") else -amount
    return ReconRow(
        data={
            "row_id": row_id,
            "record_kind": record_kind,
            "ledger_role": role,
            "normalized_reference": nref,
            "reference_no": ref,
            "date": date,
            "normalized_date": date,
            "type_label": type_label,
            "debit_source": max(source_amount, 0.0),
            "credit_source": max(-source_amount, 0.0),
            "debit_org_perspective": amount if amount >= 0 else 0.0,
            "credit_org_perspective": abs(amount) if amount < 0 else 0.0,
            "raw_type": type_label,
            "particulars": f"{type_label} {row_id}",
            "source_file": f"{role}.pdf",
            "page_number": page,
            "source_row_number": source_row,
            "raw_text": f"{type_label} {row_id}",
        },
        sheet_name=role,
    )


def test_reference_normalization_casing_and_separators() -> None:
    assert normalize_reference("SA/PB/2425/017") == normalize_reference("sa/pb/2425/017")
    assert normalize_reference("dl/25-26/191") == "DL2526191"


def test_blank_and_numeric_reference_behavior() -> None:
    assert is_missing_reference("")
    assert is_missing_reference("None")
    assert not is_missing_reference("12345")


def test_date_tolerance_timing_match() -> None:
    org = [_row("o1", role="org_ledger", nref="INV1", amount=500.0, date="2025-01-01")]
    party = [_row("p1", role="party_ledger", nref="INV1", amount=500.0, date="2025-01-05")]
    result = reconcile_rows(org, party)
    assert any("timing" in r.match_rule for r in result.records)
    assert any(r.match_status == "matched_strong" for r in result.records)


def test_rounding_difference_match() -> None:
    policy = build_tolerance_policy(amount_tolerance=0.01, rounding_tolerance=0.05)
    org = [_row("o1", role="org_ledger", nref="R1", amount=100.00)]
    party = [_row("p1", role="party_ledger", nref="R1", amount=100.01)]
    result = reconcile_rows(org, party, tolerance_policy=policy)
    assert any(r.match_rule == "stage9_rounding_difference" for r in result.records)


def test_duplicate_reference_detection() -> None:
    org = [
        _row("o1", role="org_ledger", nref="DUP", amount=100.0),
        _row("o2", role="org_ledger", nref="DUP", amount=200.0),
    ]
    party = [_row("p1", role="party_ledger", nref="OTHER", amount=999.0)]
    result = reconcile_rows(org, party)
    assert any("duplicate_reference" in r.match_rule for r in result.records)


def test_one_to_many_allocation() -> None:
    policy = settings.reconciliation_tolerance_policy()
    org = [_row("o1", role="org_ledger", amount=100.0, type_label="Payment")]
    party = [
        _row("p1", role="party_ledger", amount=40.0, type_label="Receipt"),
        _row("p2", role="party_ledger", amount=60.0, type_label="Receipt"),
    ]
    groups = find_one_to_many_allocations(org, party, policy)
    assert len(groups) == 1
    assert groups[0].org_row_ids == ["o1"]
    assert set(groups[0].party_row_ids) == {"p1", "p2"}


def test_many_to_one_allocation() -> None:
    policy = settings.reconciliation_tolerance_policy()
    org = [
        _row("o1", role="org_ledger", amount=40.0, type_label="Payment"),
        _row("o2", role="org_ledger", amount=60.0, type_label="Payment"),
    ]
    party = [_row("p1", role="party_ledger", amount=100.0, type_label="Receipt")]
    groups = find_many_to_one_allocations(org, party, policy)
    assert len(groups) == 1
    assert groups[0].party_row_ids == ["p1"]


def test_ambiguous_one_to_many_allocation_routes_to_review() -> None:
    policy = settings.reconciliation_tolerance_policy()
    org = [_row("o1", role="org_ledger", amount=100.0, type_label="Payment")]
    party = [
        _row("p1", role="party_ledger", amount=25.0, type_label="Receipt"),
        _row("p2", role="party_ledger", amount=25.0, type_label="Receipt"),
        _row("p3", role="party_ledger", amount=50.0, type_label="Receipt"),
        _row("p4", role="party_ledger", amount=50.0, type_label="Receipt"),
    ]
    groups = find_one_to_many_allocations(org, party, policy)
    assert len(groups) == 1
    assert groups[0].review_required is True
    assert "ambiguous" in groups[0].rule


def test_credit_note_netting() -> None:
    org = [_row("o1", role="org_ledger", nref="CN1", amount=50.0, type_label="CreditNote")]
    party = [_row("p1", role="party_ledger", nref="CN1", amount=50.0, type_label="DebitNote")]
    result = reconcile_rows(org, party)
    assert any(
        "credit_debit" in r.match_rule or r.match_status in {"matched_strong", "matched_supported"}
        for r in result.records
    )


def test_final_closing_balance_not_summed() -> None:
    rows = [
        _row("c1", role="org_ledger", type_label="ClosingBalance", amount=100.0, record_kind="ClosingBalance", page=1),
        _row("c2", role="org_ledger", type_label="ClosingBalance", amount=500.0, record_kind="ClosingBalance", page=5),
    ]
    assert final_closing_balance(rows) == 500.0


def test_issue_taxonomy_has_23_codes() -> None:
    assert len(PrimaryIssueCode) == 23
    assert is_valid_primary_code("ExactMatch")
    assert normalize_ai_reason_code("amount_date_agree") == "ExactMatch"


def test_per_label_tolerance_stricter_for_opening_balance() -> None:
    policy = build_tolerance_policy()
    opening = policy.for_label("OpeningBalance")
    invoice = policy.for_label("Invoice")
    assert opening.amount_tolerance <= invoice.amount_tolerance


def test_overpayment_classification_on_exact_reference_residual() -> None:
    org = [_row("o1", role="org_ledger", nref="A", amount=100.0)]
    party = [_row("p1", role="party_ledger", nref="A", amount=90.0)]
    result = reconcile_rows(org, party)
    assert any(
        r.match_status == "candidate_review"
        and r.primary_issue_code == PrimaryIssueCode.OVERPAYMENT.value
        for r in result.records
    )


def test_operator_label_tolerance_override() -> None:
    policy = build_tolerance_policy(
        label_overrides={"Payment": {"date_tolerance_days": 28}}
    )
    assert policy.for_label("Payment").date_tolerance_days == 28
