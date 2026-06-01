"""Fixture-driven tests for extraction quality and ledger understanding."""

from __future__ import annotations

from src.formalization.formalize_pair_ledgers import _assemble_row
from src.formalization.ledger_models import RecordKind
from src.formalization.pdf_ledger_extractor import (
    STRATEGY_A,
    ExtractedRow,
    _extract_with_strategy_a,
)


def test_strategy_a_handles_column_order_changes() -> None:
    table = [
        ["Date", "Particulars", "Debit", "Credit", "Vch No", "Balance"],
        ["01-Apr-25", "Opening Balance", "", "", "", "10,000.00 Dr"],
        ["02-Apr-25", "Invoice A", "1,200.00", "", "DL/25-26/001", "11,200.00 Dr"],
        ["03-Apr-25", "Receipt A", "", "500.00", "RCPT/001", "10,700.00 Dr"],
    ]
    result = _extract_with_strategy_a([table], page_number=1, source_row_number=0)
    assert result.strategy_name == STRATEGY_A
    assert result.confidence_score > 0
    assert len(result.rows) == 3
    assert any(c.key == "date" for c in result.columns)
    assert any(c.key == "debit" for c in result.columns)
    assert any(c.key == "credit" for c in result.columns)
    assert any(c.key == "balance" for c in result.columns)


def test_strategy_a_missing_reference_column_preserves_rows() -> None:
    table = [
        ["Date", "Type", "Particulars", "Debit", "Credit"],
        ["01/04/2025", "Sale", "Invoice B", "2,000.00", ""],
        ["April 1 2025", "Rcpt", "Payment Against Invoice B", "", "2,000.00"],
    ]
    result = _extract_with_strategy_a([table], page_number=1, source_row_number=0)
    assert len(result.rows) == 2
    assert all(r.raw_text for r in result.rows)
    assert result.rows[0].record_kind == RecordKind.TRANSACTION
    assert result.rows[1].record_kind == RecordKind.TRANSACTION


def test_strategy_a_captures_opening_closing_and_forward_rows() -> None:
    table = [
        ["Date", "Particulars", "Debit", "Credit", "Balance"],
        ["", "Brought Forward", "", "", "5,000.00 Dr"],
        ["01-04-25", "Opening Balance", "", "", "5,000.00 Dr"],
        ["30-04-25", "Closing Balance", "", "", "9,000.00 Dr"],
        ["", "Carried Forward", "", "", "9,000.00 Dr"],
    ]
    result = _extract_with_strategy_a([table], page_number=1, source_row_number=0)
    kinds = [r.record_kind for r in result.rows]
    assert RecordKind.BROUGHT_FORWARD in kinds
    assert RecordKind.OPENING_BALANCE in kinds
    assert RecordKind.CLOSING_BALANCE in kinds
    assert RecordKind.CARRIED_FORWARD in kinds


def test_dr_cr_balance_style_and_traceability_are_preserved() -> None:
    table = [
        ["Date", "Type", "Particulars", "Vch No", "Debit", "Credit", "Balance"],
        ["01-Apr-25", "Sale", "Invoice X", "INV/001", "1,000.00", "", "1,000.00 Dr"],
        ["02-Apr-25", "Rcpt", "Receipt X", "RCPT/001", "", "400.00", "600.00 Dr"],
    ]
    result = _extract_with_strategy_a([table], page_number=4, source_row_number=10)
    assert len(result.rows) == 2
    first = result.rows[0]
    assert first.balance_raw
    assert first.balance_side_source in {"Dr", "Cr", ""}
    assert first.source_row_number >= 11
    assert first.page_number == 4
    assert first.raw_text


def test_low_confidence_rows_enter_review_without_disappearing() -> None:
    extracted = ExtractedRow(
        page_number=1,
        source_row_number=1,
        raw_text="xx/yy/zz weird line",
        record_kind=RecordKind.TRANSACTION,
        record_kind_reason="",
        extraction_strategy_used="line_reconstruction",
        row_confidence=42.0,
        layout_confidence=50.0,
        date_raw="xx/yy/zz",
        date_iso="",
        date_confidence=20.0,
        raw_type="mystery",
        particulars="ambiguous",
        debit_raw="(1,234.00)",
        credit_raw="",
        balance_raw="",
        debit_source=-1234.0,
        credit_source=None,
        balance_source=None,
        amount_confidence=60.0,
        column_confidence={"date": 45.0, "debit": 60.0},
    )
    row = _assemble_row(extracted, "org_ledger", "TEST LEDGER", "src.pdf")
    assert row.review_flag is True
    assert row.raw_text == extracted.raw_text
