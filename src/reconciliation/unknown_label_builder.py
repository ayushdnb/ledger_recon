"""Deterministic unknown-label grouping report for the reconciliation workbook.

Scans the loaded org/party ledger rows for transactions the formalization stage
could not classify (``type_label == "Unknown"``) and groups them by their raw
type string. The result is an auditable report: raw label, normalized label,
affected counts per side, any AI grouping suggestion carried over from the
formalized rows, the review reason, and an always-true review flag.

This is pure deterministic aggregation. It works whether or not AI grouping was
ever run; when AI was off, the AI suggestion columns are simply empty and every
group is flagged for human review.
"""

from __future__ import annotations

from typing import Any

from src.reconciliation.recon_models import ReconRow
from src.standardization.type_labeler import UNKNOWN_TYPE, normalize

UNKNOWN_GROUPING_COLUMNS: list[str] = [
    "raw_label",
    "normalized_label",
    "affected_row_count",
    "org_row_count",
    "party_row_count",
    "ai_suggested_label",
    "ai_confidence",
    "ai_reason",
    "classification_status",
    "review_required",
    "context_sample",
]

# Sentinel surfaced when no predefined label was confidently justified.
UNKNOWN_NEEDS_REVIEW = "UnknownNeedsReview"

_CONTEXT_CAP = 160


def is_unknown_transaction(row: ReconRow) -> bool:
    """True for a transaction row left as the canonical ``Unknown`` label."""
    return row.record_kind == "Transaction" and row.type_label == UNKNOWN_TYPE


def has_unknown_rows(org_rows: list[ReconRow], party_rows: list[ReconRow]) -> bool:
    return any(is_unknown_transaction(r) for r in (*org_rows, *party_rows))


def _context_sample(row: ReconRow) -> str:
    text = str(
        row.data.get("particulars") or row.data.get("raw_text") or ""
    ).strip()
    return text[:_CONTEXT_CAP]


def build_unknown_label_groups(
    org_rows: list[ReconRow], party_rows: list[ReconRow]
) -> list[dict[str, Any]]:
    """Group unknown transaction rows by raw type into auditable report rows."""
    groups: dict[str, dict[str, Any]] = {}

    for side, rows in (("org", org_rows), ("party", party_rows)):
        for row in rows:
            if not is_unknown_transaction(row):
                continue
            raw_label = str(row.data.get("raw_type") or "").strip()
            key = normalize(raw_label)
            group = groups.get(key)
            if group is None:
                group = {
                    "raw_label": raw_label,
                    "normalized_label": key,
                    "affected_row_count": 0,
                    "org_row_count": 0,
                    "party_row_count": 0,
                    "ai_suggested_label": "",
                    "ai_confidence": "",
                    "ai_reason": "",
                    "classification_status": UNKNOWN_NEEDS_REVIEW,
                    "review_required": True,
                    "context_sample": _context_sample(row),
                }
                groups[key] = group
            group["affected_row_count"] += 1
            group[f"{side}_row_count"] += 1
            if not group["context_sample"]:
                group["context_sample"] = _context_sample(row)
            # Carry over any AI grouping suggestion recorded on the row. The first
            # non-empty suggestion in a group wins (rows share the same raw type).
            suggested = str(row.data.get("ai_label_suggested") or "").strip()
            if suggested and not group["ai_suggested_label"]:
                group["ai_suggested_label"] = suggested
                confidence = row.data.get("ai_label_confidence")
                group["ai_confidence"] = confidence if confidence not in (None, "") else ""
                group["ai_reason"] = str(row.data.get("ai_label_reason") or "").strip()

    # Stable order: most-affected groups first, then by raw label.
    return sorted(
        groups.values(),
        key=lambda g: (-g["affected_row_count"], g["raw_label"]),
    )


def unknown_rows_for_side(rows: list[ReconRow]) -> list[dict[str, Any]]:
    """Return the raw row dicts for unknown transactions on one side."""
    return [r.data for r in rows if is_unknown_transaction(r)]
