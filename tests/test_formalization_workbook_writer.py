"""Tests for formalized workbook creation and required sheet set."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.formalization.ledger_models import (
    LEDGER_SHEET_COLUMNS,
    ExtractionAuditRow,
    FormalizedLedger,
    LedgerIdentity,
    LedgerRow,
    RecordKind,
)
from src.formalization.validation import build_validation_report
from src.formalization.workbook_writer import (
    assign_sheet_names,
    write_formalized_workbook,
)


def _row(role: str, name: str, *, review: bool = False, **kw) -> LedgerRow:
    base = dict(
        row_id="id_" + name.replace(" ", "") + str(kw.get("source_row_number", 1)),
        record_kind=RecordKind.TRANSACTION,
        ledger_role=role,
        detected_ledger_name=name,
        source_file="src.pdf",
        page_number=1,
        source_row_number=1,
        date="2025-04-25",
        raw_type="Sale",
        type_label="Invoice",
        reference_no="DL/25-26/191",
        normalized_reference="DL2526191",
        debit_source=28630.0,
        raw_text="25-Apr-25 Sale DL/25-26/191 28,630.00",
        extraction_method="pymupdf_words_layout",
        confidence_score=100.0,
        review_flag=review,
        review_reason="low confidence" if review else "",
    )
    base.update(kw)
    return LedgerRow(**base)


def _ledger(role: str, name: str, rows: list[LedgerRow]) -> FormalizedLedger:
    identity = LedgerIdentity(
        ledger_role=role,
        source_file="src.pdf",
        detected_title=name,
        detection_method="account_label",
        confidence=0.95,
    )
    audit = [
        ExtractionAuditRow(
            ledger_role=role,
            source_file="src.pdf",
            page_number=1,
            extraction_method="pymupdf_words_layout",
            status="ok",
            rows_emitted=len(rows),
            rows_dropped=0,
        )
    ]
    return FormalizedLedger(
        ledger_role=role, identity=identity, sheet_name="", rows=rows, audit=audit
    )


def _build(tmp_path: Path) -> Path:
    org = _ledger("org_ledger", "GOOD LUCK", [_row("org_ledger", "GOOD LUCK")])
    party = _ledger(
        "party_ledger",
        "Baby And Mom Retail Pvt Ltd",
        [_row("party_ledger", "Baby And Mom Retail Pvt Ltd", review=True)],
    )
    assign_sheet_names([org, party])
    items = build_validation_report([org, party])
    out = tmp_path / "formalized_ledgers__pair_test.xlsx"
    write_formalized_workbook(out, "pair_test", [org, party], items)
    return out


def test_workbook_created_with_required_sheets(tmp_path: Path) -> None:
    out = _build(tmp_path)
    assert out.exists()
    names = pd.ExcelFile(out).sheet_names
    assert names[0] == "README"
    assert "GOOD LUCK" in names
    assert "Baby And Mom Retail Pvt Ltd" in names
    assert "Extraction_Audit" in names
    assert "Page_Analysis" in names
    assert "Validation_Report" in names
    assert "Review_Queue" in names
    # Exactly the two ledger sheets plus README + 4 audit/report sheets.
    assert len(names) == 7


def test_ledger_sheets_use_canonical_schema(tmp_path: Path) -> None:
    out = _build(tmp_path)
    df = pd.read_excel(out, sheet_name="GOOD LUCK")
    assert list(df.columns) == LEDGER_SHEET_COLUMNS


def test_review_queue_contains_flagged_rows(tmp_path: Path) -> None:
    out = _build(tmp_path)
    rq = pd.read_excel(out, sheet_name="Review_Queue")
    assert len(rq) == 1
    assert rq.iloc[0]["ledger_role"] == "party_ledger"
