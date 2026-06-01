"""Tests for Excel sheet-name sanitization and collision handling."""

from __future__ import annotations

from src.formalization.workbook_writer import (
    SHEET_NAME_MAX,
    sanitize_sheet_name,
)


def test_simple_name_preserved() -> None:
    assert sanitize_sheet_name("GOOD LUCK", set()) == "GOOD LUCK"


def test_party_name_preserved() -> None:
    assert sanitize_sheet_name("Baby And Mom Retail Pvt Ltd", set()) == "Baby And Mom Retail Pvt Ltd"


def test_invalid_characters_removed() -> None:
    name = sanitize_sheet_name("A/C: Good*Luck?[2025]", set())
    for bad in ":\\/?*[]":
        assert bad not in name


def test_length_capped_to_31() -> None:
    long = "X" * 60
    assert len(sanitize_sheet_name(long, set())) <= SHEET_NAME_MAX


def test_empty_falls_back() -> None:
    assert sanitize_sheet_name("", set()) == "Ledger"
    assert sanitize_sheet_name("///", set()) == "Ledger"


def test_collision_handling_is_deterministic() -> None:
    used: set[str] = set()
    first = sanitize_sheet_name("GOOD LUCK", used)
    used.add(first)
    second = sanitize_sheet_name("GOOD LUCK", used)
    assert first == "GOOD LUCK"
    assert second != first
    assert second.startswith("GOOD LUCK")
    # repeat deterministically
    used2 = {"GOOD LUCK"}
    assert sanitize_sheet_name("GOOD LUCK", used2) == second


def test_collision_respects_length_limit() -> None:
    base = "Z" * 31
    used = {base}
    result = sanitize_sheet_name(base, used)
    assert len(result) <= SHEET_NAME_MAX
    assert result != base
