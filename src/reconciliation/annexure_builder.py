"""Annexure layout helpers for label-driven reconciliation sheets."""

from __future__ import annotations

from dataclasses import dataclass


ANNEX_ORG_HEADERS = [
    "source_row_id",
    "date",
    "type_label",
    "reference_no",
    "normalized_reference",
    "debit_org_perspective",
    "credit_org_perspective",
    "net_org_perspective",
    "match_status",
    "decision_source",
    "match_type",
    "review_flag",
]

ANNEX_PARTY_HEADERS = [
    "source_row_id",
    "date",
    "type_label",
    "reference_no",
    "normalized_reference",
    "debit_org_perspective",
    "credit_org_perspective",
    "net_org_perspective",
    "match_status",
    "decision_source",
    "match_type",
    "review_flag",
]

ANNEX_MATCH_HEADERS = [
    "match_group_id",
    "match_status",
    "match_rule",
    "match_confidence",
    "decision_source",
    "match_type",
    "org_row_ids",
    "party_row_ids",
    "org_row_id",
    "party_row_id",
    "org_amount",
    "party_amount",
    "amount_difference",
    "review_required",
    "review_reason",
    "primary_issue_code",
    "secondary_issue_tags",
    "validation_status",
    "reviewer_comment",
    "manual_status",
]


@dataclass
class AnnexurePlan:
    """Sheet name and the label represented by that sheet."""

    label: str
    sheet_name: str


def sanitize_annexure_name(label: str, used: set[str]) -> str:
    """Excel-safe deterministic annexure sheet naming."""
    clean = f"Annex_{label}".replace("/", "_").replace("\\", "_").replace("*", "_")
    clean = clean.replace("?", "_").replace("[", "_").replace("]", "_").replace(":", "_")
    clean = clean[:31] if clean else "Annex"
    name = clean
    idx = 2
    lowered = {u.lower() for u in used}
    while name.lower() in lowered:
        suffix = f"_{idx}"
        name = f"{clean[: 31 - len(suffix)]}{suffix}"
        idx += 1
    return name


def build_annexure_plans(labels: list[str]) -> list[AnnexurePlan]:
    """Build deterministic annexure sheet plans for every configured label."""
    used: set[str] = set()
    plans: list[AnnexurePlan] = []
    for label in labels:
        sheet_name = sanitize_annexure_name(label, used)
        used.add(sheet_name)
        plans.append(AnnexurePlan(label=label, sheet_name=sheet_name))
    return plans
