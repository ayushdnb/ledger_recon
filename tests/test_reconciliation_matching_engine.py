from __future__ import annotations

import pytest

from src.reconciliation.matching_engine import (
    is_missing_reference,
    is_type_compatible,
    reconcile_rows,
)
from src.reconciliation.issue_taxonomy import PrimaryIssueCode
from src.reconciliation.recon_models import ReconRow


def _row(
    row_id: str,
    *,
    role: str,
    ref: str = "",
    nref: str = "",
    date: str = "2025-01-01",
    type_label: str = "Invoice",
    amount: float = 100.0,
) -> ReconRow:
    source_amount = amount if role.startswith("org") else -amount
    return ReconRow(
        data={
            "row_id": row_id,
            "record_kind": "Transaction",
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
        },
        sheet_name=role,
    )


def test_exact_reference_match() -> None:
    org = [_row("o1", role="org_ledger", nref="ABC/1", amount=500.0)]
    party = [_row("p1", role="party_ledger", nref="ABC/1", amount=500.0)]
    result = reconcile_rows(org, party)
    assert any(r.match_status == "matched_strong" for r in result.records)


def test_containment_reference_match() -> None:
    org = [_row("o1", role="org_ledger", nref="ABC/1", amount=500.0)]
    party = [_row("p1", role="party_ledger", nref="X-ABC/1", amount=500.0)]
    result = reconcile_rows(org, party)
    assert any(r.match_rule == "stage5_containment_ref_amount" for r in result.records)


def test_fuzzy_reference_goes_review() -> None:
    org = [_row("o1", role="org_ledger", nref="SA/PB/2425/017", amount=500.0)]
    party = [
        _row(
            "p1",
            role="party_ledger",
            nref="SA/PB/2425/071",
            date="2025-01-02",
            amount=500.0,
        )
    ]
    result = reconcile_rows(org, party)
    assert any(r.review_required for r in result.records)


def test_missing_reference_goes_review() -> None:
    org = [_row("o1", role="org_ledger", nref="", amount=500.0)]
    party = [_row("p1", role="party_ledger", nref="", date="2025-01-02", amount=500.0)]
    result = reconcile_rows(org, party)
    assert any("stage15_no_ref" in r.match_rule for r in result.records)


def test_duplicate_candidates_go_review() -> None:
    org = [_row("o1", role="org_ledger", nref="ABC", amount=100.0)]
    party = [
        _row("p1", role="party_ledger", nref="ABC", amount=100.0),
        _row("p2", role="party_ledger", nref="ABC", amount=100.0),
    ]
    result = reconcile_rows(org, party)
    assert any("multiple exact-reference" in r.review_reason.lower() for r in result.records)


def test_exact_reference_amount_mismatch_routes_to_classified_review() -> None:
    org = [_row("o1", role="org_ledger", nref="ABC", amount=100.0)]
    party = [_row("p1", role="party_ledger", nref="ABC", amount=125.0)]
    result = reconcile_rows(org, party)
    assert any(
        r.match_status == "candidate_review"
        and r.primary_issue_code == PrimaryIssueCode.UNDERPAYMENT.value
        for r in result.records
    )


def test_type_compatibility() -> None:
    assert is_type_compatible("Invoice", "Invoice")
    assert is_type_compatible("Receipt", "Payment")
    assert not is_type_compatible("Invoice", "Payment")


# ---------------------------------------------------------------------------
# Missing-reference safety (Fix 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", [None, "None", "", "  ", "nan", "NaN", "null", "-", "--", "NONE"]
)
def test_is_missing_reference_tokens(value: object) -> None:
    assert is_missing_reference(value) is True


@pytest.mark.parametrize("value", ["ABC123", "SA/PB/2425/017", "0", "X-1"])
def test_present_reference_not_missing(value: str) -> None:
    assert is_missing_reference(value) is False


def test_none_vs_none_does_not_strong_match() -> None:
    # Empty cells round-trip from Excel as the literal string "None"; this must
    # never become an exact reference strong match.
    org = [_row("o1", role="org_ledger", nref="None", amount=500.0)]
    party = [_row("p1", role="party_ledger", nref="None", amount=500.0)]
    result = reconcile_rows(org, party)
    assert not any(r.match_status == "matched_strong" for r in result.records)


def test_blank_vs_blank_does_not_strong_match() -> None:
    org = [_row("o1", role="org_ledger", nref="", amount=500.0)]
    party = [_row("p1", role="party_ledger", nref="", amount=500.0)]
    result = reconcile_rows(org, party)
    assert not any(r.match_status == "matched_strong" for r in result.records)


def test_missing_ref_exact_amount_is_supported_but_never_strong() -> None:
    org = [_row("o1", role="org_ledger", nref="None", amount=500.0, type_label="Invoice")]
    party = [_row("p1", role="party_ledger", nref="None", amount=500.0, type_label="Invoice")]
    result = reconcile_rows(org, party)
    statuses = {r.match_status for r in result.records}
    assert "matched_strong" not in statuses
    assert "matched_supported" in statuses


def test_missing_ref_strong_match_count_is_zero() -> None:
    org = [_row(f"o{i}", role="org_ledger", nref="None", amount=100.0 + i) for i in range(3)]
    party = [_row(f"p{i}", role="party_ledger", nref="None", amount=100.0 + i) for i in range(3)]
    result = reconcile_rows(org, party)
    strong_missing = [
        r
        for r in result.records
        if r.match_status == "matched_strong"
        and (
            is_missing_reference(r.org_normalized_reference)
            or is_missing_reference(r.party_normalized_reference)
        )
    ]
    assert strong_missing == []


def test_missing_ref_candidate_carries_review_flag_and_reason() -> None:
    org = [_row("o1", role="org_ledger", nref="None", amount=500.0)]
    party = [_row("p1", role="party_ledger", nref="None", date="2025-01-02", amount=500.0)]
    result = reconcile_rows(org, party)
    candidates = [r for r in result.records if r.match_status == "candidate_review"]
    assert candidates, "expected at least one candidate_review row"
    for r in candidates:
        assert r.review_required is True
        assert "reference" in r.review_reason.lower()


def test_present_ref_vs_missing_ref_does_not_strong_match() -> None:
    org = [_row("o1", role="org_ledger", nref="ABC123", amount=500.0)]
    party = [_row("p1", role="party_ledger", nref="None", amount=500.0)]
    result = reconcile_rows(org, party)
    assert not any(r.match_status == "matched_strong" for r in result.records)


def test_same_side_polarity_is_not_accepted() -> None:
    org = [_row("o1", role="org_ledger", nref="", amount=500.0)]
    party = [_row("p1", role="org_ledger", nref="", amount=500.0)]
    result = reconcile_rows(org, party)
    assert not any(r.match_status in {"matched_strong", "matched_supported"} for r in result.records)


def test_candidate_row_is_not_emitted_again_as_unmatched() -> None:
    org = [_row("o1", role="org_ledger", nref="", amount=500.0)]
    party = [_row("p1", role="party_ledger", nref="", date="2025-01-02", amount=500.0)]
    result = reconcile_rows(org, party)
    assert [r.match_status for r in result.records] == ["candidate_review"]
