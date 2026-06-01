"""Build a human-usable, prioritised review queue.

Only unresolved, formalization-flagged, or validation-rejected work is surfaced
here. Manual columns (reviewer_comment, manual_status, resolved_by, resolved_date)
are always emitted blank so a reviewer can fill them in Excel.
"""

from __future__ import annotations

from typing import Any

from src.reconciliation.balance_helpers import (
    CLOSING_LABEL,
    final_closing_balance,
    final_closing_row,
)
from src.reconciliation.issue_taxonomy import PrimaryIssueCode, is_valid_primary_code
from src.reconciliation.matching_engine import is_missing_reference
from src.config import settings
from src.reconciliation.recon_models import MatchRecord, ReconRow

REVIEW_QUEUE_COLUMNS = [
    "priority",
    "source_area",
    "source_sheet",
    "source_row_id",
    "source_row_ids",
    "source_file",
    "page_number",
    "source_row_number",
    "type_label",
    "date",
    "reference",
    "normalized_reference",
    "amount",
    "match_status",
    "primary_issue_code",
    "secondary_issue_tags",
    "reason",
    "suggested_action",
    "source_raw_text",
    "reviewer_comment",
    "manual_status",
    "manual_issue_code",
    "selected_match_group_id",
    "reviewer_selected_org_row_ids",
    "reviewer_selected_party_row_ids",
    "reviewed_by",
    "reviewed_at",
    "override_reason",
    "resolved_by",
    "resolved_date",
]

# Manual columns are reviewer-owned and must always be written blank.
MANUAL_COLUMNS = [
    "reviewer_comment",
    "manual_status",
    "manual_issue_code",
    "selected_match_group_id",
    "reviewer_selected_org_row_ids",
    "reviewer_selected_party_row_ids",
    "reviewed_by",
    "reviewed_at",
    "override_reason",
    "resolved_by",
    "resolved_date",
]

_AMOUNT_TOLERANCE = settings.recon_amount_tolerance
_PRIORITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
_LOW_CONFIDENCE = 80.0


def _blank_manuals(row: dict[str, Any]) -> dict[str, Any]:
    for col in MANUAL_COLUMNS:
        row[col] = ""
    return row


def _match_priority(m: MatchRecord) -> str:
    reason = (m.review_reason or "").lower()
    if "ambiguous" in reason or "multiple" in reason:
        return "HIGH"
    if (
        m.org_amount is not None
        and m.party_amount is not None
        and m.amount_difference is not None
        and m.amount_difference > _AMOUNT_TOLERANCE
    ):
        return "HIGH"
    if m.match_status in {"unmatched_org", "unmatched_party"}:
        return "MEDIUM"
    if (
        is_missing_reference(m.org_normalized_reference)
        or is_missing_reference(m.party_normalized_reference)
    ):
        return "MEDIUM"
    return "MEDIUM"


def _suggested_action(m: MatchRecord) -> str:
    if m.validation_status == "REJECTED":
        return "Review AI validation rejection against both source ledgers."
    if m.match_status == "unmatched_org":
        return "Locate corresponding party entry or confirm org-only item."
    if m.match_status == "unmatched_party":
        return "Locate corresponding org entry or confirm party-only item."
    if m.amount_difference and m.amount_difference > _AMOUNT_TOLERANCE:
        return "Resolve amount mismatch against source ledgers before closing."
    return "Manually confirm or reject this candidate against source ledgers."


def _match_row_ids(match: MatchRecord) -> list[str]:
    return list(
        dict.fromkeys(
            match.org_row_ids
            + ([match.org_row_id] if match.org_row_id else [])
            + match.party_row_ids
            + ([match.party_row_id] if match.party_row_id else [])
        )
    )


def _join_row_values(rows: list[ReconRow], field: str) -> str:
    values = list(
        dict.fromkeys(str(row.data.get(field) or "").strip() for row in rows)
    )
    return " | ".join(value for value in values if value)


def _trace_payload(rows: list[ReconRow]) -> dict[str, str]:
    return {
        "source_row_ids": " | ".join(row.row_id for row in rows if row.row_id),
        "source_file": _join_row_values(rows, "source_file"),
        "page_number": _join_row_values(rows, "page_number"),
        "source_row_number": _join_row_values(rows, "source_row_number"),
    }


def build_review_queue(
    org_rows: list[ReconRow],
    party_rows: list[ReconRow],
    matches: list[MatchRecord],
) -> list[dict[str, Any]]:
    """Assemble prioritised review rows from formalization + reconciliation."""
    rows: list[dict[str, Any]] = []
    source_by_id = {row.row_id: row for row in org_rows + party_rows if row.row_id}

    # 1) Formalization-side review items (low confidence / flagged / AI-repaired).
    for row in org_rows + party_rows:
        if row.record_kind != "Transaction":
            continue
        ai_repaired = bool(row.data.get("ai_repair_applied", False))
        low_conf = row.confidence_score and row.confidence_score < _LOW_CONFIDENCE
        if not (row.review_flag or low_conf or ai_repaired):
            continue
        if ai_repaired:
            priority = "MEDIUM"
            reason = "AI-repaired row requires human confirmation."
        elif row.review_flag:
            priority = "MEDIUM"
            reason = row.data.get("review_reason", "") or "Row flagged for review during formalization."
        else:
            priority = "LOW"
            reason = "Low extraction confidence; otherwise traceable."
        rows.append(
            _blank_manuals(
                {
                    "priority": priority,
                    "source_area": "formalization",
                    "source_sheet": row.sheet_name,
                    "source_row_id": row.row_id,
                    **_trace_payload([row]),
                    "type_label": row.type_label,
                    "date": row.date,
                    "reference": row.reference,
                    "normalized_reference": row.normalized_reference,
                    "amount": row.net_org,
                    "match_status": "",
                    "primary_issue_code": PrimaryIssueCode.DATA_QUALITY_ISSUE.value,
                    "secondary_issue_tags": (
                        "ai_repaired"
                        if ai_repaired
                        else "formalization_flag"
                        if row.review_flag
                        else "low_extraction_confidence"
                    ),
                    "reason": reason,
                    "suggested_action": "Verify extracted row against the source ledger PDF.",
                    "source_raw_text": row.data.get("raw_text", ""),
                }
            )
        )

    # 2) Reconciliation-side review items (candidate / unmatched / mismatch).
    for m in matches:
        if not m.review_required:
            continue
        amount = m.org_amount if m.org_amount is not None else m.party_amount
        source_rows = [
            source_by_id[row_id]
            for row_id in _match_row_ids(m)
            if row_id in source_by_id
        ]
        issue_code = getattr(m, "primary_issue_code", "")
        if not is_valid_primary_code(issue_code):
            issue_code = PrimaryIssueCode.MANUAL_REVIEW_REQUIRED.value
        rows.append(
            _blank_manuals(
                {
                    "priority": _match_priority(m),
                    "source_area": "reconciliation",
                    "source_sheet": "Match_Evidence",
                    "source_row_id": m.org_row_id or m.party_row_id or m.match_group_id,
                    **_trace_payload(source_rows),
                    "type_label": m.type_label,
                    "date": m.org_date or m.party_date,
                    "reference": m.org_reference or m.party_reference,
                    "normalized_reference": m.org_normalized_reference
                    or m.party_normalized_reference,
                    "amount": amount,
                    "match_status": m.match_status,
                    "primary_issue_code": issue_code,
                    "secondary_issue_tags": getattr(m, "secondary_issue_tags", ""),
                    "reason": m.review_reason,
                    "suggested_action": _suggested_action(m),
                    "source_raw_text": " | ".join(
                        t for t in (m.org_particulars, m.party_particulars) if t
                    ),
                }
            )
        )

    # 3) Closing-balance integrity items (HIGH): stated org vs party closing.
    org_close = _stated_closing(org_rows)
    party_close = _stated_closing(party_rows)
    if org_close is not None and party_close is not None:
        if abs(abs(org_close) - abs(party_close)) > _AMOUNT_TOLERANCE:
            closing_rows = [
                row
                for row in (final_closing_row(org_rows), final_closing_row(party_rows))
                if row is not None
            ]
            rows.append(
                _blank_manuals(
                    {
                        "priority": "HIGH",
                        "source_area": "validation",
                        "source_sheet": "Summary",
                        "source_row_id": "closing_balance",
                        **_trace_payload(closing_rows),
                        "type_label": CLOSING_LABEL,
                        "date": "",
                        "reference": "",
                        "normalized_reference": "",
                        "amount": org_close - party_close,
                        "match_status": "closing_balance_difference",
                        "primary_issue_code": PrimaryIssueCode.PERIOD_CUTOFF_ISSUE.value,
                        "secondary_issue_tags": "closing_balance_gap",
                        "reason": (
                            "Stated closing balances differ between org and party "
                            "ledgers; confirm reconciliation difference."
                        ),
                        "suggested_action": "Investigate closing balance gap before sign-off.",
                        "source_raw_text": "",
                    }
                )
            )

    rows.sort(
        key=lambda r: (
            _PRIORITY_ORDER.get(r["priority"], 9),
            str(r.get("source_area", "")),
            str(r.get("source_row_id", "")),
        )
    )
    return rows


def _stated_closing(rows: list[ReconRow]) -> float | None:
    return final_closing_balance(rows)
