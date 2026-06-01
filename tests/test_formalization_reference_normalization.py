"""Tests for reference normalization (no matching is performed)."""

from __future__ import annotations

import pytest

from src.formalization.reference_normalization import normalize_reference


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("SA/PB/2425/017", "SAPB2425017"),
        ("SA/PB/2425/017SC", "SAPB2425017SC"),
        ("SA/PB/25-26/001", "SAPB2526001"),
        ("64858/SA/PB/2425/017SC", "64858SAPB2425017SC"),
        ("DL/25-26/191", "DL2526191"),
        ("dl/25-26/191", "DL2526191"),
        ("cn/dl/25-26/063", "CNDL2526063"),
        ("  DL / 25-26 / 191  ", "DL2526191"),
        ("DL.25.26.191", "DL2526191"),
    ],
)
def test_normalize_reference(raw: str, expected: str) -> None:
    assert normalize_reference(raw) == expected


def test_case_insensitive_consistency() -> None:
    assert normalize_reference("dl/25-26/191") == normalize_reference("DL/25-26/191")


@pytest.mark.parametrize("value", [None, "", "   ", "///---..."])
def test_empty_or_noise_returns_empty(value) -> None:
    assert normalize_reference(value) == ""
