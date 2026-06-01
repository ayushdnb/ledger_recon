"""Standardize a raw ledger transaction-type string to a canonical label.

These labels are *standardization labels only*. They are NOT reconciliation
decisions. For example, a "Sale" on the organisation side may correspond to a
"Purchase" on the party side later; this module does not reconcile anything.

Matching order:
    1. Exact normalized match against an alias.
    2. Regex / substring match (alias appears within the normalized raw type).
    3. RapidFuzz fuzzy score (default threshold 85).

If nothing meets the threshold, ``canonical_type`` becomes ``"Unknown"`` and
``review_required`` is set to ``True``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

from src.config import settings

DEFAULT_FUZZY_THRESHOLD = 85

UNKNOWN_TYPE = "Unknown"

# Canonical labels permitted anywhere in the project, including the schema.
ALLOWED_CANONICAL_TYPES = (
    "Invoice",
    "CreditNote",
    "DebitNote",
    "Payment",
    "Receipt",
    "TDS",
    "PQV",
    "Journal",
    "OpeningBalance",
    "ClosingBalance",
    UNKNOWN_TYPE,
)


@dataclass
class TypeMatch:
    """Result of labeling a single raw type string."""

    raw_type: str
    canonical_type: str
    match_method: str
    match_score: float
    matched_alias: str | None
    review_required: bool
    review_reason: str

    def as_dict(self) -> dict:
        return {
            "raw_type": self.raw_type,
            "canonical_type": self.canonical_type,
            "match_method": self.match_method,
            "match_score": self.match_score,
            "matched_alias": self.matched_alias,
            "review_required": self.review_required,
            "review_reason": self.review_reason,
        }


@dataclass
class TypeLabeler:
    """Maps raw type strings to canonical labels using a label dictionary."""

    labels: dict[str, list[str]]
    threshold: float = DEFAULT_FUZZY_THRESHOLD
    _normalized_aliases: list[tuple[str, str, str]] = field(
        default_factory=list, init=False, repr=False
    )

    def __post_init__(self) -> None:
        # Precompute (canonical, raw_alias, normalized_alias) triples.
        for canonical, aliases in self.labels.items():
            for alias in aliases:
                self._normalized_aliases.append(
                    (canonical, alias, normalize(alias))
                )

    def label(self, raw_type: str | None) -> TypeMatch:
        raw_value = "" if raw_type is None else str(raw_type)
        normalized = normalize(raw_value)

        if not normalized:
            return TypeMatch(
                raw_type=raw_value,
                canonical_type=UNKNOWN_TYPE,
                match_method="none",
                match_score=0.0,
                matched_alias=None,
                review_required=True,
                review_reason="Empty or missing raw type.",
            )

        # 1. Exact normalized match.
        for canonical, alias, norm_alias in self._normalized_aliases:
            if normalized == norm_alias:
                return TypeMatch(
                    raw_type=raw_value,
                    canonical_type=canonical,
                    match_method="exact",
                    match_score=100.0,
                    matched_alias=alias,
                    review_required=False,
                    review_reason="",
                )

        # 2. Regex / substring match (longest alias wins to prefer specificity).
        substring_hits: list[tuple[int, str, str]] = []
        for canonical, alias, norm_alias in self._normalized_aliases:
            if not norm_alias:
                continue
            pattern = r"\b" + re.escape(norm_alias) + r"\b"
            if re.search(pattern, normalized) or norm_alias in normalized:
                substring_hits.append((len(norm_alias), canonical, alias))
        if substring_hits:
            substring_hits.sort(reverse=True)
            _, canonical, alias = substring_hits[0]
            return TypeMatch(
                raw_type=raw_value,
                canonical_type=canonical,
                match_method="substring",
                match_score=100.0,
                matched_alias=alias,
                review_required=False,
                review_reason="",
            )

        # 3. Fuzzy match.
        best_score = 0.0
        best_canonical: str | None = None
        best_alias: str | None = None
        for canonical, alias, norm_alias in self._normalized_aliases:
            if not norm_alias:
                continue
            score = fuzz.ratio(normalized, norm_alias)
            if score > best_score:
                best_score = score
                best_canonical = canonical
                best_alias = alias

        if best_canonical is not None and best_score >= self.threshold:
            return TypeMatch(
                raw_type=raw_value,
                canonical_type=best_canonical,
                match_method="fuzzy",
                match_score=float(best_score),
                matched_alias=best_alias,
                review_required=False,
                review_reason="",
            )

        return TypeMatch(
            raw_type=raw_value,
            canonical_type=UNKNOWN_TYPE,
            match_method="fuzzy" if best_canonical is not None else "none",
            match_score=float(best_score),
            matched_alias=best_alias,
            review_required=True,
            review_reason=(
                f"No alias matched at or above fuzzy threshold {self.threshold} "
                f"(best score {best_score:.0f})."
            ),
        )


def normalize(text: str) -> str:
    """Normalize a type string: lowercase, trim, collapse internal whitespace."""
    if text is None:
        return ""
    lowered = str(text).strip().lower()
    return re.sub(r"\s+", " ", lowered)


def load_type_labels(path: Path | None = None) -> dict[str, list[str]]:
    """Load the type-label dictionary from JSON."""
    resolved = (
        settings.resolved(path)
        if path is not None
        else settings.resolved(settings.type_labels_path)
    )
    if not resolved.exists():
        raise FileNotFoundError(f"Type labels file not found: {resolved}")
    with resolved.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Type labels file must be a JSON object: {resolved}")
    return data


@lru_cache(maxsize=1)
def get_default_labeler() -> TypeLabeler:
    """Return a labeler built from the configured type-labels file."""
    return TypeLabeler(labels=load_type_labels())


def label_type(raw_type: str | None, threshold: float | None = None) -> TypeMatch:
    """Convenience wrapper using the default (configured) labeler."""
    labeler = get_default_labeler()
    if threshold is not None and threshold != labeler.threshold:
        labeler = TypeLabeler(labels=labeler.labels, threshold=threshold)
    return labeler.label(raw_type)
