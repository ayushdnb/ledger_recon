"""Tests for type classification with confidence bands."""

from __future__ import annotations

import pytest

from src.formalization.type_classification import (
    BAND_HIGH,
    BAND_UNMATCHED,
    band_for_score,
    classify_type,
)


@pytest.mark.parametrize(
    "raw, expected_label",
    [
        ("Rcpt", "Receipt"),
        ("Pymt", "Payment"),
        ("Payment", "Payment"),
        ("Sale", "Invoice"),
        ("Sales", "Invoice"),
        ("Purchase", "Invoice"),  # Invoice is neutral: party-side purchase
        ("SlRt", "CreditNote"),
        ("Debit Note", "DebitNote"),
        ("Jrnl", "Journal"),
    ],
)
def test_known_mappings_accepted(raw: str, expected_label: str) -> None:
    result = classify_type(raw)
    assert result.type_label == expected_label
    assert result.review_required is False
    assert result.confidence >= 80.0


def test_invoice_is_neutral_for_sale_and_purchase() -> None:
    assert classify_type("Sale").type_label == classify_type("Purchase").type_label == "Invoice"


def test_unknown_routed_to_review() -> None:
    result = classify_type("zzz-not-a-type")
    assert result.type_label == "Unknown"
    assert result.review_required is True
    assert result.band == BAND_UNMATCHED
    assert result.review_reason


def test_empty_requires_review() -> None:
    result = classify_type("")
    assert result.type_label == "Unknown"
    assert result.review_required is True


@pytest.mark.parametrize(
    "score, expected",
    [(100.0, BAND_HIGH), (90.0, BAND_HIGH), (85.0, "acceptable"), (75.0, "needs_review"), (50.0, BAND_UNMATCHED)],
)
def test_band_boundaries(score: float, expected: str) -> None:
    assert band_for_score(score) == expected
