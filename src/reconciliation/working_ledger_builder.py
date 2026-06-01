"""Build working ledger rows/columns from formalized rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.formalization.reference_normalization import normalize_reference
from src.reconciliation.recon_models import MatchRecord, ReconRow

WORKING_COLUMNS = [
    "source_row_id",
    "period",
    "record_kind",
    "date",
    "raw_type",
    "type_label",
    "voucher_no",
    "reference_no",
    "normalized_reference",
    "particulars",
    "account",
    "debit_source",
    "credit_source",
    "net_source",
    "debit_org_perspective",
    "credit_org_perspective",
    "net_org_perspective",
    "match_key_primary",
    "match_key_secondary",
    "match_group_id",
    "match_status",
    "decision_source",
    "match_type",
    "match_confidence",
    "validation_status",
    "review_flag",
    "review_reason",
    "reviewer_status",
    "reviewer_comment",
    "selected_match_group_id",
    "reviewer_selected_org_row_ids",
    "reviewer_selected_party_row_ids",
    "manual_issue_code",
    "reviewed_by",
    "reviewed_at",
    "override_reason",
    "primary_issue_code",
    "amount_tolerance_used",
    "date_tolerance_days_used",
    "source_file",
    "page_number",
    "source_row_number",
    "raw_text",
]


@dataclass
class WorkingRow:
    """Working ledger row payload."""

    values: dict[str, Any]


def _period_from_date(date_text: str) -> str:
    text = (date_text or "").strip()
    if not text:
        return ""
    try:
        dt = datetime.strptime(text[:10], "%Y-%m-%d")
        return dt.strftime("%Y-%m")
    except ValueError:
        return ""


def build_working_rows(rows: list[ReconRow]) -> list[WorkingRow]:
    """Map formalized rows into working ledger rows."""
    output: list[WorkingRow] = []
    for row in rows:
        date_text = row.date
        norm_ref = row.normalized_reference or normalize_reference(row.reference)
        match_key_primary = f"{norm_ref}|{row.type_label}|{abs(row.net_org):.2f}" if norm_ref else ""
        match_key_secondary = f"{row.type_label}|{abs(row.net_org):.2f}|{date_text[:10]}"
        output.append(
            WorkingRow(
                values={
                    "source_row_id": row.row_id,
                    "period": _period_from_date(date_text),
                    "record_kind": row.record_kind,
                    "date": date_text,
                    "raw_type": row.data.get("raw_type", ""),
                    "type_label": row.type_label,
                    "voucher_no": row.data.get("voucher_no", ""),
                    "reference_no": row.data.get("reference_no", ""),
                    "normalized_reference": norm_ref,
                    "particulars": row.data.get("particulars", ""),
                    "account": row.data.get("account", ""),
                    "debit_source": row.data.get("debit_source"),
                    "credit_source": row.data.get("credit_source"),
                    "net_source": None,  # formula at write time
                    "debit_org_perspective": row.data.get("debit_org_perspective"),
                    "credit_org_perspective": row.data.get("credit_org_perspective"),
                    "net_org_perspective": None,  # formula at write time
                    "match_key_primary": match_key_primary,
                    "match_key_secondary": match_key_secondary,
                    "match_group_id": "",
                    "match_status": "",
                    "decision_source": "",
                    "match_type": "",
                    "match_confidence": None,
                    "validation_status": "",
                    "review_flag": row.data.get("review_flag", False),
                    "review_reason": row.data.get("review_reason", ""),
                    "reviewer_status": "",
                    "reviewer_comment": "",
                    "selected_match_group_id": "",
                    "reviewer_selected_org_row_ids": "",
                    "reviewer_selected_party_row_ids": "",
                    "manual_issue_code": "",
                    "reviewed_by": "",
                    "reviewed_at": "",
                    "override_reason": "",
                    "primary_issue_code": "",
                    "amount_tolerance_used": None,
                    "date_tolerance_days_used": None,
                    "source_file": row.data.get("source_file", ""),
                    "page_number": row.data.get("page_number", ""),
                    "source_row_number": row.data.get("source_row_number", ""),
                    "raw_text": row.data.get("raw_text", ""),
                }
            )
        )
    return output


def apply_match_records(
    org_working: list[WorkingRow],
    party_working: list[WorkingRow],
    matches: list[MatchRecord],
) -> None:
    """Write final deterministic/AI placement directly onto both working ledgers."""
    priority = {
        "matched_ai": 5,
        "matched_strong": 4,
        "matched_supported": 3,
        "ledger_only_ai": 2,
        "candidate_review": 1,
    }

    def ids(record: MatchRecord, side: str) -> list[str]:
        plural = record.org_row_ids if side == "org" else record.party_row_ids
        singular = record.org_row_id if side == "org" else record.party_row_id
        return plural or ([singular] if singular else [])

    def apply(rows: list[WorkingRow], side: str) -> None:
        by_id: dict[str, MatchRecord] = {}
        for record in matches:
            for row_id in ids(record, side):
                previous = by_id.get(row_id)
                if previous is None or priority.get(record.match_status, 0) > priority.get(previous.match_status, 0):
                    by_id[row_id] = record
        for row in rows:
            record = by_id.get(str(row.values.get("source_row_id") or ""))
            if record is None:
                continue
            row.values.update(
                {
                    "match_group_id": record.match_group_id,
                    "match_status": record.match_status,
                    "decision_source": record.decision_source,
                    "match_type": record.match_type,
                    "match_confidence": record.match_confidence,
                    "validation_status": record.validation_status,
                    "review_flag": bool(row.values.get("review_flag")) or record.review_required,
                    "review_reason": record.review_reason or row.values.get("review_reason", ""),
                    "primary_issue_code": getattr(record, "primary_issue_code", ""),
                    "amount_tolerance_used": getattr(record, "amount_tolerance_used", None),
                    "date_tolerance_days_used": getattr(record, "date_tolerance_days_used", None),
                }
            )

    apply(org_working, "org")
    apply(party_working, "party")
