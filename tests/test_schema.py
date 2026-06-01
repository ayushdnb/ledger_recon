"""Tests for the strict standardization schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.standardization.ledger_schema import (
    AIStandardizationResult,
    SchemaError,
    StandardizedLedgerRow,
    parse_ai_result,
)


def test_valid_row_accepts_approved_type() -> None:
    row = StandardizedLedgerRow(ledger_role="org_ledger", canonical_type="Invoice")
    assert row.canonical_type == "Invoice"
    # Missing values default to None (null), never invented.
    assert row.transaction_date is None
    assert row.debit is None


def test_schema_rejects_invalid_canonical_type() -> None:
    with pytest.raises(ValidationError):
        StandardizedLedgerRow(ledger_role="org_ledger", canonical_type="NotAType")


def test_unknown_is_an_allowed_canonical_type() -> None:
    row = StandardizedLedgerRow(ledger_role="party_ledger", canonical_type="Unknown")
    assert row.canonical_type == "Unknown"


def test_parse_ai_result_rejects_malformed_json() -> None:
    with pytest.raises(SchemaError):
        parse_ai_result("{not valid json")


def test_parse_ai_result_rejects_non_object() -> None:
    with pytest.raises(SchemaError):
        parse_ai_result("[1, 2, 3]")


def test_parse_ai_result_rejects_empty() -> None:
    with pytest.raises(SchemaError):
        parse_ai_result("")


def test_parse_ai_result_accepts_minimal_object() -> None:
    result = parse_ai_result('{"pair_id": "pair_x"}')
    assert isinstance(result, AIStandardizationResult)
    assert result.pair_id == "pair_x"
    assert result.org_ledger_rows == []


def test_parse_ai_result_rejects_bad_canonical_type_in_rows() -> None:
    payload = (
        '{"pair_id": "p", "org_ledger_rows": '
        '[{"ledger_role": "org_ledger", "canonical_type": "Bogus"}]}'
    )
    with pytest.raises(SchemaError):
        parse_ai_result(payload)
