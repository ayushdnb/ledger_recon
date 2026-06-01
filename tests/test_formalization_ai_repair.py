"""Tests for token-controlled AI failed-row repair."""

from __future__ import annotations

import importlib
import json
from unittest.mock import MagicMock, patch

import pytest

from src.formalization.ai_failed_row_repair import (
    build_repair_evidence,
    is_date_ambiguous_row,
    repair_failed_rows_batch,
    select_failed_rows,
)
from src.formalization.ledger_models import LedgerRow, RecordKind, ROLE_PARTY


def _row(
    row_id: str = "abc123",
    *,
    date_confidence: float = 50.0,
    review_reason: str = "Low date confidence.",
    raw_date: str = "22-04-2025",
    normalized_date: str = "",
    raw_text: str = "22-04-2025 Sale 100.00",
) -> LedgerRow:
    return LedgerRow(
        row_id=row_id,
        record_kind=RecordKind.TRANSACTION,
        ledger_role=ROLE_PARTY,
        detected_ledger_name="Party",
        source_file="party.pdf",
        page_number=1,
        source_row_number=1,
        raw_date=raw_date,
        normalized_date=normalized_date,
        date_parse_confidence=date_confidence,
        review_flag=True,
        review_reason=review_reason,
        raw_text=raw_text,
    )


def _fresh_modules():
    import src.config as config
    import src.formalization.ai_failed_row_repair as repair

    importlib.reload(config)
    return importlib.reload(repair)


def test_repair_mode_does_not_call_ai_when_off(monkeypatch) -> None:
    monkeypatch.setenv("AI_FORMALIZATION_MODE", "off")
    repair = _fresh_modules()
    rows = [_row()]
    result = repair.repair_failed_rows_batch(rows, ledger_role=ROLE_PARTY, header_texts=[])
    assert result.used_ai is False
    assert result.applied_count == 0


def test_repair_mode_sends_only_failed_rows(monkeypatch) -> None:
    monkeypatch.setenv("AI_FORMALIZATION_MODE", "repair_failed_rows")
    monkeypatch.setenv("AI_MAX_FAILED_ROWS_PER_REQUEST", "10")
    repair = _fresh_modules()

    ok_row = _row("ok001", date_confidence=95.0, review_reason="", normalized_date="2025-04-22")
    bad_row = _row("bad001", date_confidence=40.0)
    selected = repair.select_failed_rows([ok_row, bad_row], ledger_role=ROLE_PARTY, max_rows=10)
    assert len(selected) == 1
    assert selected[0].row_id == "bad001"

    evidence = repair.build_repair_evidence(selected, [ok_row, bad_row], ["Date Type Debit Credit"])
    assert len(evidence["failed_rows"]) == 1
    assert evidence["failed_rows"][0]["row_id"] == "bad001"
    assert "debit_source" not in json.dumps(evidence)


def test_repair_mode_respects_batch_cap(monkeypatch) -> None:
    monkeypatch.setenv("AI_FORMALIZATION_MODE", "repair_failed_rows")
    monkeypatch.setenv("AI_MAX_FAILED_ROWS_PER_REQUEST", "3")
    repair = _fresh_modules()

    rows = [_row(f"id{i}", date_confidence=10.0) for i in range(8)]
    selected = repair.select_failed_rows(rows, ledger_role=ROLE_PARTY, max_rows=3)
    assert len(selected) == 3


def test_forbidden_fields_cannot_be_modified_by_ai(monkeypatch) -> None:
    monkeypatch.setenv("AI_FORMALIZATION_MODE", "repair_failed_rows")
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("AI_DATA_APPROVAL", "hosted_approved")
    monkeypatch.setenv("AI_API_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("AI_MODEL_NAME", "test-model")
    repair = _fresh_modules()

    row = _row("r1", date_confidence=40.0)
    ai_response = {
        "repairs": [
            {
                "row_id": "r1",
                "normalized_date": "2025-04-22",
                "date_parse_confidence": 85.0,
                "explanation": "parsed from raw_text",
                "debit_source": 9999.0,
            }
        ]
    }

    with patch.object(
        repair,
        "_request_ai_repairs",
        return_value=(ai_response, MagicMock(used_ai=True, cache_miss=True, cache_hit=False, warnings=[])),
    ):
        result = repair.repair_failed_rows_batch([row], ledger_role=ROLE_PARTY, header_texts=[])

    assert result.applied_count == 1
    assert row.debit_source is None
    assert row.ai_repair_applied is True


def test_repaired_date_preserves_source_evidence(monkeypatch) -> None:
    monkeypatch.setenv("AI_FORMALIZATION_MODE", "repair_failed_rows")
    repair = _fresh_modules()

    row = _row("r2", date_confidence=40.0, raw_date="22-04-2025", normalized_date="")
    original_raw = row.raw_text
    original_raw_date = row.raw_date

    ai_response = {
        "repairs": [
            {
                "row_id": "r2",
                "normalized_date": "2025-04-22",
                "date_parse_confidence": 88.0,
                "explanation": "ISO date from DD-MM-YYYY in raw_text",
            }
        ]
    }

    with patch.object(
        repair,
        "_request_ai_repairs",
        return_value=(ai_response, MagicMock(used_ai=True, cache_miss=True, cache_hit=False, warnings=[])),
    ):
        repair.repair_failed_rows_batch([row], ledger_role=ROLE_PARTY, header_texts=[])

    assert row.raw_text == original_raw
    assert row.raw_date == original_raw_date
    assert row.normalized_date == "2025-04-22"
    assert row.ai_repair_applied is True
    assert row.ai_repair_normalized_date == "2025-04-22"


def test_cache_hit_avoids_repeated_ai_call(monkeypatch) -> None:
    monkeypatch.setenv("AI_FORMALIZATION_MODE", "repair_failed_rows")
    repair = _fresh_modules()

    row = _row("r3", date_confidence=40.0)
    cached_response = {
        "repairs": [
            {
                "row_id": "r3",
                "normalized_date": "2025-04-22",
                "date_parse_confidence": 80.0,
                "explanation": "cached",
            }
        ]
    }

    with patch.object(
        repair,
        "_request_ai_repairs",
        return_value=(cached_response, MagicMock(used_ai=True, cache_hit=True, cache_miss=False, warnings=[])),
    ) as mock_request:
        result = repair.repair_failed_rows_batch([row], ledger_role=ROLE_PARTY, header_texts=[])

    assert result.cache_hit is True
    assert result.applied_count == 1
    mock_request.assert_called_once()


def test_validation_stable_after_repair(monkeypatch) -> None:
    monkeypatch.setenv("AI_FORMALIZATION_MODE", "repair_failed_rows")
    repair = _fresh_modules()

    from src.formalization.formalize_pair_ledgers import _assemble_row
    from src.formalization.ledger_models import ROLE_ORG, FormalizedLedger, LedgerIdentity
    from src.formalization.validation import build_validation_report
    from src.formalization.pdf_ledger_extractor import ExtractedRow

    er = ExtractedRow(
        page_number=1,
        source_row_number=1,
        raw_text="22-04-2025 Sale",
        record_kind=RecordKind.TRANSACTION,
        record_kind_reason="",
        date_raw="22-04-2025",
        date_iso="",
        date_confidence=40.0,
    )
    ledger_row = _assemble_row(er, ROLE_PARTY, "Party", "party.pdf")

    ai_response = {
        "repairs": [
            {
                "row_id": ledger_row.row_id,
                "normalized_date": "2025-04-22",
                "date_parse_confidence": 85.0,
                "explanation": "ok",
            }
        ]
    }

    with patch.object(
        repair,
        "_request_ai_repairs",
        return_value=(ai_response, MagicMock(used_ai=True, cache_miss=True, cache_hit=False, warnings=[])),
    ):
        repair.repair_failed_rows_batch([ledger_row], ledger_role=ROLE_PARTY, header_texts=[])

    ledger = FormalizedLedger(
        ledger_role=ROLE_PARTY,
        identity=LedgerIdentity(
            ledger_role=ROLE_PARTY,
            source_file="party.pdf",
            detected_title="Party",
            confidence=0.9,
        ),
        sheet_name="Party",
        rows=[ledger_row],
        layout_stats={"ai_repair_applied_count": 1, "ai_repair_rejected_count": 0},
    )
    org_ledger = FormalizedLedger(
        ledger_role=ROLE_ORG,
        identity=LedgerIdentity(
            ledger_role=ROLE_ORG,
            source_file="org.pdf",
            detected_title="Org",
            confidence=0.9,
        ),
        sheet_name="Org",
        rows=[],
    )
    items = build_validation_report([org_ledger, ledger])
    failures = [i for i in items if i.status == "FAIL"]
    assert failures == []


def test_is_date_ambiguous_row() -> None:
    assert is_date_ambiguous_row(_row(date_confidence=40.0)) is True
    assert is_date_ambiguous_row(
        _row(date_confidence=95.0, review_reason="", normalized_date="2025-04-22")
    ) is False


def teardown_module(module) -> None:  # noqa: ARG001
    import src.config

    importlib.reload(src.config)
