"""Tests for the canonical type labeler."""

from __future__ import annotations

import pytest

from src.standardization.type_labeler import (
    UNKNOWN_TYPE,
    TypeLabeler,
    label_type,
    load_type_labels,
)


@pytest.fixture(scope="module")
def labeler() -> TypeLabeler:
    return TypeLabeler(labels=load_type_labels())


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Rcpt", "Receipt"),
        ("Pymt", "Payment"),
        ("Sale", "Invoice"),
        ("Purchase", "Invoice"),
        ("SlRt", "CreditNote"),
        ("PrRt", "DebitNote"),
        ("Jrnl", "Journal"),
    ],
)
def test_known_mappings(labeler: TypeLabeler, raw: str, expected: str) -> None:
    match = labeler.label(raw)
    assert match.canonical_type == expected
    assert match.review_required is False


def test_unknown_type_requires_review(labeler: TypeLabeler) -> None:
    match = labeler.label("zzzqqq-not-a-type")
    assert match.canonical_type == UNKNOWN_TYPE
    assert match.review_required is True
    assert match.review_reason


def test_empty_type_requires_review(labeler: TypeLabeler) -> None:
    match = labeler.label("")
    assert match.canonical_type == UNKNOWN_TYPE
    assert match.review_required is True


def test_type_labels_file_loads() -> None:
    labels = load_type_labels()
    assert "Invoice" in labels
    assert "sale" in labels["Invoice"]


def test_default_labeler_wrapper() -> None:
    match = label_type("Rcpt")
    assert match.canonical_type == "Receipt"
