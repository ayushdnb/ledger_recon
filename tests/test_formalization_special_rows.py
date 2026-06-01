"""Special-row classification must never absorb structural rows as transactions.

These exercise the exact text examples from the reconciliation hardening spec.
A wrong classification here lets a balance/total/forward line leak into
transaction matching, which is an audit failure.
"""

from __future__ import annotations

import pytest

from src.formalization.ledger_cleaning import detect_record_kind
from src.formalization.ledger_models import RecordKind, SPECIAL_RECORD_KINDS


def _kind(text: str) -> str:
    # Most of these rows carry amounts but no date/type marker; the record-kind
    # rule must still classify them structurally, not as transactions.
    kind, _ = detect_record_kind(
        text, has_amount=True, has_date=False, has_type=False
    )
    return kind


@pytest.mark.parametrize(
    "text",
    [
        "Totals c/o 33,20,860.00 30,06,992.00",
        "Totals b/d 33,20,860.00 30,06,992.00",
        "Debit Balance 4,68,847.00",
        "Credit Balance 4,68,847.00",
        "Grand Total 49,63,872.00 49,63,872.00",
        "Carried Over 9,52,807.00 10,47,985.00",
        "Brought Forward 9,52,807.00 10,47,985.00",
        "By Closing Balance 13,896.00",
        "Opening Balance 1,00,000.00",
        "Total 49,63,872.00 44,95,025.00",
    ],
)
def test_special_rows_are_not_transactions(text: str) -> None:
    kind = _kind(text)
    assert kind != RecordKind.TRANSACTION, f"{text!r} wrongly classified as Transaction"
    assert kind in SPECIAL_RECORD_KINDS, f"{text!r} -> {kind} is not a special record kind"


def test_totals_co_is_carried_forward() -> None:
    assert _kind("Totals c/o 33,20,860.00 30,06,992.00") == RecordKind.CARRIED_FORWARD


def test_totals_bd_is_brought_forward() -> None:
    assert _kind("Totals b/d 33,20,860.00 30,06,992.00") == RecordKind.BROUGHT_FORWARD


def test_debit_balance_is_closing_balance() -> None:
    assert _kind("Debit Balance 4,68,847.00") == RecordKind.CLOSING_BALANCE


def test_credit_balance_is_closing_balance() -> None:
    assert _kind("Credit Balance 9,99,999.00") == RecordKind.CLOSING_BALANCE


def test_grand_total_is_grand_total() -> None:
    assert _kind("Grand Total 49,63,872.00 49,63,872.00") == RecordKind.GRAND_TOTAL


def test_closing_balance_marker() -> None:
    assert _kind("By Closing Balance 13,896.00") == RecordKind.CLOSING_BALANCE


def test_ordinary_transaction_still_transaction() -> None:
    kind, _ = detect_record_kind(
        "22-04-2025 Sale SA/PB/2425/017 1,06,710.00",
        has_amount=True,
        has_date=True,
        has_type=True,
    )
    assert kind == RecordKind.TRANSACTION
