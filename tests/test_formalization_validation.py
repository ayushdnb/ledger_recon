"""Tests for the validation report generator."""

from __future__ import annotations

from src.formalization.ledger_models import (
    ExtractionAuditRow,
    FormalizedLedger,
    LedgerIdentity,
    LedgerRow,
    PageAnalysis,
    RecordKind,
)
from src.formalization.validation import build_validation_report


def _txn(**kw) -> LedgerRow:
    base = dict(
        row_id="x",
        record_kind=RecordKind.TRANSACTION,
        ledger_role="org_ledger",
        detected_ledger_name="GOOD LUCK",
        source_file="src.pdf",
        page_number=1,
        source_row_number=1,
        date="2025-04-25",
        raw_type="Sale",
        type_label="Invoice",
        reference_no="DL/25-26/191",
        normalized_reference="DL2526191",
        debit_source=100.0,
        raw_text="row",
        extraction_method="pymupdf_words_layout",
        confidence_score=100.0,
    )
    base.update(kw)
    return LedgerRow(**base)


def _special(kind: str) -> LedgerRow:
    return _txn(
        record_kind=kind,
        raw_type="",
        type_label="",
        reference_no="",
        normalized_reference="",
        date="",
        raw_text=f"{kind} row",
    )


def _ledger(rows, dropped: int = 0, confidence: float = 0.95) -> FormalizedLedger:
    identity = LedgerIdentity(
        ledger_role="org_ledger",
        source_file="src.pdf",
        detected_title="GOOD LUCK",
        detection_method="account_label",
        confidence=confidence,
    )
    audit = [
        ExtractionAuditRow(
            ledger_role="org_ledger",
            source_file="src.pdf",
            page_number=1,
            extraction_method="pymupdf_words_layout",
            status="ok",
            rows_emitted=len(rows),
            rows_dropped=dropped,
        )
    ]
    return FormalizedLedger(
        ledger_role="org_ledger",
        identity=identity,
        sheet_name="GOOD LUCK",
        rows=rows,
        audit=audit,
        page_analysis=[
            PageAnalysis(
                page_number=1,
                extraction_strategy_used="pymupdf_coordinate",
                confidence_score=88.0,
                detected_headers=["Date Type Debit Credit"],
                detected_columns=["date", "raw_type", "debit", "credit"],
                transaction_row_count=len(rows),
                special_row_count=0,
                unknown_row_count=0,
                review_row_count=0,
            )
        ],
        layout_stats={
            "ai_usage_count": 1,
            "ai_cache_hit_count": 1,
            "ai_cache_miss_count": 0,
        },
    )


def _by_check(items, check):
    return [i for i in items if i.check == check]


def test_report_includes_global_and_per_ledger_checks() -> None:
    led = _ledger([_txn(), _special(RecordKind.OPENING_BALANCE), _special(RecordKind.CLOSING_BALANCE)])
    items = build_validation_report([led, led])
    checks = {i.check for i in items}
    assert "source_pdfs_discovered" in checks
    assert "no_reconciliation_performed" in checks
    assert "rows_dropped" in checks
    assert "source_traceability_completeness" in checks
    assert "ledger_confidence" in checks
    assert "ai_usage_count" in checks
    assert "page_confidence" in checks


def test_rows_dropped_zero_passes_nonzero_fails() -> None:
    ok = _ledger([_txn()], dropped=0)
    bad = _ledger([_txn()], dropped=2)
    ok_item = _by_check(build_validation_report([ok, ok]), "rows_dropped")[0]
    bad_item = _by_check(build_validation_report([bad, bad]), "rows_dropped")[0]
    assert ok_item.status == "PASS"
    assert bad_item.status == "FAIL"
    assert bad_item.count == 2


def test_opening_closing_capture_status() -> None:
    led = _ledger([_txn(), _special(RecordKind.OPENING_BALANCE), _special(RecordKind.CLOSING_BALANCE)])
    items = build_validation_report([led, led])
    assert _by_check(items, "opening_balance_captured")[0].status == "PASS"
    assert _by_check(items, "closing_balance_captured")[0].status == "PASS"


def test_traceability_fails_when_evidence_missing() -> None:
    incomplete = _txn(raw_text="")  # missing raw_text breaks traceability
    led = _ledger([incomplete])
    item = _by_check(build_validation_report([led, led]), "source_traceability_completeness")[0]
    assert item.status == "FAIL"


def test_identity_low_confidence_flagged_for_review() -> None:
    led = _ledger([_txn()], confidence=0.4)
    item = _by_check(build_validation_report([led, led]), "ledger_identity_detected")[0]
    assert item.status == "REVIEW"
