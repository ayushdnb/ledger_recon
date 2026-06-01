"""Deterministic transaction-type classification with confidence bands.

This wraps the existing project labeler
(:mod:`src.standardization.type_labeler`) and the authoritative label config
(``config/type_labels.json``) and adds explicit confidence banding so that
low-confidence rows are routed to review rather than silently accepted.

Semantic rule (important):
    ``Invoice`` is neutral. It covers organisation-side "Sale" and party-side
    "Purchase". This is a standardization label only and is NOT a reconciliation
    conclusion about sale vs. purchase.

Confidence bands (per the milestone spec):
    >= 90  -> high        (accepted)
    80-89  -> acceptable  (accepted)
    70-79  -> needs_review (kept, but flagged for review)
    < 70   -> unmatched   (label set to Unknown, flagged for review)
"""

from __future__ import annotations

from dataclasses import dataclass

from src.standardization.type_labeler import (
    UNKNOWN_TYPE,
    TypeLabeler,
    load_type_labels,
)

BAND_HIGH = "high"
BAND_ACCEPTABLE = "acceptable"
BAND_NEEDS_REVIEW = "needs_review"
BAND_UNMATCHED = "unmatched"

HIGH_MIN = 90.0
ACCEPTABLE_MIN = 80.0
REVIEW_MIN = 70.0

# Build the labeler at the lowest band threshold so we always learn the best
# candidate score, then apply banding ourselves. A single module-level instance
# is reused (the label config is static for a run).
_LABELER: TypeLabeler | None = None


def _labeler() -> TypeLabeler:
    global _LABELER
    if _LABELER is None:
        _LABELER = TypeLabeler(labels=load_type_labels(), threshold=REVIEW_MIN)
    return _LABELER


def band_for_score(score: float) -> str:
    if score >= HIGH_MIN:
        return BAND_HIGH
    if score >= ACCEPTABLE_MIN:
        return BAND_ACCEPTABLE
    if score >= REVIEW_MIN:
        return BAND_NEEDS_REVIEW
    return BAND_UNMATCHED


@dataclass
class TypeClassification:
    """Result of classifying a raw transaction-type string."""

    raw_type: str
    type_label: str
    confidence: float
    band: str
    match_method: str
    matched_alias: str | None
    review_required: bool
    review_reason: str


def classify_type(raw_type: str | None) -> TypeClassification:
    """Classify a raw type string into a canonical label with a confidence band."""
    match = _labeler().label(raw_type)
    score = float(match.match_score)
    band = band_for_score(score)

    label = match.canonical_type
    review_required = False
    review_reason = ""

    if band == BAND_UNMATCHED:
        label = UNKNOWN_TYPE
        review_required = True
        review_reason = (
            f"Type '{match.raw_type}' unmatched (best score {score:.0f} < {REVIEW_MIN:.0f})."
        )
    elif band == BAND_NEEDS_REVIEW:
        review_required = True
        review_reason = (
            f"Type '{match.raw_type}' low confidence (score {score:.0f}); needs review."
        )

    # An empty/Unknown result from the labeler is always review-worthy.
    if label == UNKNOWN_TYPE and not review_required:
        review_required = True
        review_reason = review_reason or "Type could not be classified."

    return TypeClassification(
        raw_type=match.raw_type,
        type_label=label,
        confidence=score,
        band=band,
        match_method=match.match_method,
        matched_alias=match.matched_alias,
        review_required=review_required,
        review_reason=review_reason,
    )
