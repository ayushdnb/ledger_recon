"""Tests for the standardization prompt builder."""

from __future__ import annotations

import pandas as pd

from src.standardization.prompt_builder import (
    SYSTEM_INSTRUCTIONS,
    build_messages,
    build_messages_for_chunk,
    build_raw_context_text,
    messages_char_count,
    plan_context_chunks,
)
from src.standardization.type_labeler import ALLOWED_CANONICAL_TYPES, load_type_labels


def test_system_instructions_demand_json_only() -> None:
    text = SYSTEM_INSTRUCTIONS.lower()
    assert "json" in text
    assert "no prose" in text
    assert "no markdown" in text
    assert "null" in text


def test_system_instructions_list_canonical_types() -> None:
    for canonical in ALLOWED_CANONICAL_TYPES:
        assert canonical in SYSTEM_INSTRUCTIONS


def test_build_raw_context_text_preserves_all_rows() -> None:
    df = pd.DataFrame({"line_text": ["alpha", "beta", "gamma"]})
    text = build_raw_context_text({"PDF_Text_Lines": df})
    assert "alpha" in text
    assert "beta" in text
    assert "gamma" in text
    assert "truncated" not in text


def _write_raw_workbook(path) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(
            {
                "ledger_role": ["org_ledger"],
                "source_file_name": ["org.pdf"],
                "page_number": [1],
                "line_number": [1],
                "line_text": ["Sales Invoice 100"],
            }
        ).to_excel(writer, sheet_name="PDF_Text_Lines", index=False)
        pd.DataFrame(
            {
                "ledger_role": ["party_ledger"],
                "source_file_name": ["party.pdf"],
                "page_number": [1],
                "text_chunk_index": [0],
                "text_chunk": ["raw page text"],
            }
        ).to_excel(writer, sheet_name="PDF_Page_Text", index=False)


def test_build_messages_has_system_and_user(tmp_path) -> None:
    raw_workbook = tmp_path / "raw.xlsx"
    _write_raw_workbook(raw_workbook)
    messages = build_messages(
        pair_id="pair_test",
        raw_workbook_path=raw_workbook,
        reference_profile=None,
        type_labels=load_type_labels(),
        max_input_chars=10000,
    )
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "pair_test" in messages[1]["content"]
    assert "Sales Invoice 100" in messages[1]["content"]
    assert messages_char_count(messages) <= 10000


def test_plan_context_chunks_respects_max_input_chars(tmp_path) -> None:
    raw_workbook = tmp_path / "raw_large.xlsx"
    rows = []
    for page in range(1, 21):
        for line in range(1, 51):
            rows.append(
                {
                    "ledger_role": "org_ledger" if page % 2 else "party_ledger",
                    "source_file_name": "ledger.pdf",
                    "page_number": page,
                    "line_number": line,
                    "line_text": f"Transaction detail line {page}-{line} " + ("x" * 80),
                }
            )
    with pd.ExcelWriter(raw_workbook, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name="PDF_Text_Lines", index=False)

    context = {"PDF_Text_Lines": pd.read_excel(raw_workbook, sheet_name="PDF_Text_Lines")}
    max_chars = 8000
    chunks = plan_context_chunks(
        context=context,
        pair_id="pair_chunk_test",
        reference_profile=None,
        type_labels={},
        max_input_chars=max_chars,
    )
    assert len(chunks) > 1
    for idx, chunk in enumerate(chunks, start=1):
        messages = build_messages_for_chunk(
            pair_id="pair_chunk_test",
            chunk=chunk,
            chunk_index=idx,
            chunk_total=len(chunks),
            reference_profile=None,
            type_labels={},
            max_input_chars=max_chars,
        )
        assert messages_char_count(messages) <= max_chars
        assert chunk.source_ledger
        assert chunk.page_range


def test_chunks_preserve_source_metadata(tmp_path) -> None:
    raw_workbook = tmp_path / "raw_meta.xlsx"
    _write_raw_workbook(raw_workbook)
    context = {
        name: pd.read_excel(raw_workbook, sheet_name=name)
        for name in ("PDF_Text_Lines", "PDF_Page_Text")
    }
    chunks = plan_context_chunks(
        context=context,
        pair_id="pair_meta",
        reference_profile=None,
        type_labels={},
        max_input_chars=10000,
    )
    combined = "\n".join(c.context_text for c in chunks)
    assert "Sales Invoice 100" in combined
    assert "raw page text" in combined
    assert "org.pdf" in combined
    assert "party.pdf" in combined
