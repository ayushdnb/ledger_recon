"""Tests for pure parsing helpers: amounts, dates, balances, record kinds."""

from __future__ import annotations

import pytest

from src.formalization.ledger_cleaning import (
    detect_record_kind,
    parse_amount,
    parse_balance,
    parse_date,
)
from src.formalization.ledger_models import RecordKind


# --- amounts (Indian comma formatting) ------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("28,630.00", 28630.0),
        ("1,06,710.00", 106710.0),
        ("20,07,983.49", 2007983.49),
        ("Rs. 0.00", 0.0),
        ("28,630.00 Cr", 28630.0),
        ("13,896.00 Dr", 13896.0),
        ("(1,200.00)", -1200.0),
        ("500", 500.0),
    ],
)
def test_parse_amount(raw: str, expected: float) -> None:
    assert parse_amount(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "  ", "abc", "ICICI Bank", "DL/25-26/191"])
def test_parse_amount_returns_none_when_absent(raw) -> None:
    assert parse_amount(raw) is None


def test_parse_balance_side() -> None:
    assert parse_balance("28,630.00 Cr") == (pytest.approx(28630.0), "Cr")
    assert parse_balance("13,896.00 Dr") == (pytest.approx(13896.0), "Dr")
    assert parse_balance("1,000.00") == (pytest.approx(1000.0), "")
    assert parse_balance(None) == (None, "")


# --- dates -----------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("22-04-2025", "2025-04-22"),
        ("1-4-2025", "2025-04-01"),
        ("01-04-25", "2025-04-01"),
        ("22-Apr-25", "2025-04-22"),
        ("1-Apr-25", "2025-04-01"),
        ("01/04/2025", "2025-04-01"),
        ("April 1 2025", "2025-04-01"),
        ("30-5-2026", "2026-05-30"),
        ("26-May-26", "2026-05-26"),
    ],
)
def test_parse_date(raw: str, expected: str) -> None:
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "not-a-date", "2025", "Sales"])
def test_parse_date_unparseable(raw) -> None:
    assert parse_date(raw) is None


# --- record kinds ----------------------------------------------------------
def test_detect_opening_balance() -> None:
    kind, _ = detect_record_kind(
        "Opening Bal. = Rs. 0.00", has_amount=True, has_date=False, has_type=False
    )
    assert kind == RecordKind.OPENING_BALANCE


def test_detect_closing_balance() -> None:
    kind, _ = detect_record_kind(
        "By Closing Balance 13,896.00", has_amount=True, has_date=False, has_type=False
    )
    assert kind == RecordKind.CLOSING_BALANCE


def test_detect_total() -> None:
    kind, _ = detect_record_kind(
        "Total 20,07,983.49 20,07,983.49", has_amount=True, has_date=False, has_type=False
    )
    assert kind == RecordKind.TOTAL


def test_detect_untitled_subtotal_is_total_with_review() -> None:
    kind, reason = detect_record_kind(
        "17,75,015.00 17,61,119.00", has_amount=True, has_date=False, has_type=False
    )
    assert kind == RecordKind.TOTAL
    assert reason  # untitled subtotal should explain itself


def test_detect_carried_and_brought_forward() -> None:
    c, _ = detect_record_kind("Carried Over", has_amount=False, has_date=False, has_type=False)
    b, _ = detect_record_kind("Brought Forward", has_amount=False, has_date=False, has_type=False)
    assert c == RecordKind.CARRIED_FORWARD
    assert b == RecordKind.BROUGHT_FORWARD


def test_detect_transaction() -> None:
    kind, _ = detect_record_kind(
        "25-Apr-25 By Interstate Purchase DL/25-26/191 28,630.00",
        has_amount=True,
        has_date=True,
        has_type=True,
    )
    assert kind == RecordKind.TRANSACTION


def test_special_rows_not_confused_with_transactions() -> None:
    # An opening balance line that also carries an amount must NOT be a transaction.
    kind, _ = detect_record_kind(
        "1-Apr-26 To Opening Balance 13,896.00",
        has_amount=True,
        has_date=True,
        has_type=False,
    )
    assert kind == RecordKind.OPENING_BALANCE
