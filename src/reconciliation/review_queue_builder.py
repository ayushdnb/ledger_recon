"""Build a human-usable, prioritised review queue.

Only unresolved, formalization-flagged, or validation-rejected work is surfaced
here. Manual columns (reviewer_comment, manual_status, resolved_by, resolved_date)
are always emitted blank so a reviewer can fill them in Excel.
"""

from __future__ import annotations

from typing import Any

from src.reconciliation.matching_engine import is_missing_reference
from src.reconciliation.recon_models import MatchRecord, ReconRow

REVIEW_QUEUE_COLUMNS = [
    "priority",
    "source_area",
    "source_sheet",
    "source_row_id",
    "type_label",
    "date",
    "reference",
    "normalized_reference",
    "amount",
    "match_status",
    "reason",
    "suggested_action",
    "source_raw_text",
    "reviewer_comment",
    "manual_status",
    "resolved_by",
    "resolved_date",
]

# Manual columns are reviewer-owned and must always be written blank.
MANUAL_COLUMNS = ["reviewer_comment", "manual_status", "resolved_by", "resolved_date"]

CLOSING_LABEL = "ClosingBalance"
_AMOUNT_TOLERANCE = 0.01
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


def build_review_queue(
    org_rows: list[ReconRow],
    party_rows: list[ReconRow],
    matches: list[MatchRecord],
) -> list[dict[str, Any]]:
    """Assemble prioritised review rows from formalization + reconciliation."""
    rows: list[dict[str, Any]] = []

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
                    "type_label": row.type_label,
                    "date": row.date,
                    "reference": row.reference,
                    "normalized_reference": row.normalized_reference,
                    "amount": row.net_org,
                    "match_status": "",
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
        rows.append(
            _blank_manuals(
                {
                    "priority": _match_priority(m),
                    "source_area": "reconciliation",
                    "source_sheet": "Match_Evidence",
                    "source_row_id": m.org_row_id or m.party_row_id or m.match_group_id,
                    "type_label": m.type_label,
                    "date": m.org_date or m.party_date,
                    "reference": m.org_reference or m.party_reference,
                    "normalized_reference": m.org_normalized_reference
                    or m.party_normalized_reference,
                    "amount": amount,
                    "match_status": m.match_status,
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
            rows.append(
                _blank_manuals(
                    {
                        "priority": "HIGH",
                        "source_area": "validation",
                        "source_sheet": "Summary",
                        "source_row_id": "closing_balance",
                        "type_label": CLOSING_LABEL,
                        "date": "",
                        "reference": "",
                        "normalized_reference": "",
                        "amount": org_close - party_close,
                        "match_status": "closing_balance_difference",
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
    total = 0.0
    found = False
    for row in rows:
        if row.type_label == CLOSING_LABEL:
            total += row.net_org
            found = True
    return total if found else None
