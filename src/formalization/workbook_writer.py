"""Workbook assembly for the formalized two-ledger output.

Writes a workbook containing exactly:
- README
- one organisation-ledger sheet (named from the detected ledger identity)
- one party-ledger sheet (named from the detected ledger identity)
- Extraction_Audit
- Validation_Report
- Review_Queue

Sheet names derived from ledger identity are sanitized for Excel limits (<=31
chars, no invalid characters) with deterministic collision handling. The
original detected title and final sheet name are recorded in the audit/README.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.excel.workbook_io import write_workbook
from src.formalization.ledger_models import (
    LEDGER_SHEET_COLUMNS,
    FormalizedLedger,
    ValidationItem,
)

# Excel forbids these characters in sheet names, plus a 31-char limit.
_INVALID_SHEET_CHARS = re.compile(r"[:\\/?*\[\]]")
SHEET_NAME_MAX = 31

RESERVED_SHEETS = (
    "README",
    "Extraction_Audit",
    "Validation_Report",
    "Review_Queue",
    "Page_Analysis",
)

AUDIT_COLUMNS = [
    "ledger_role",
    "source_file",
    "page_number",
    "extraction_method",
    "status",
    "rows_emitted",
    "rows_dropped",
    "detail",
]

VALIDATION_COLUMNS = ["check", "scope", "status", "count", "details"]

PAGE_ANALYSIS_COLUMNS = [
    "ledger_role",
    "sheet_name",
    "page_number",
    "extraction_strategy_used",
    "confidence_score",
    "detected_headers",
    "detected_columns",
    "column_confidence",
    "transaction_row_count",
    "special_row_count",
    "unknown_row_count",
    "review_row_count",
]

REVIEW_COLUMNS = [
    "row_id",
    "ledger_role",
    "detected_ledger_name",
    "record_kind",
    "source_file",
    "page_number",
    "source_row_number",
    "date",
    "raw_type",
    "type_label",
    "reference_no",
    "normalized_reference",
    "confidence_score",
    "review_reason",
    "raw_text",
]


def sanitize_sheet_name(title: str, used: set[str]) -> str:
    """Return an Excel-safe, unique sheet name derived from ``title``.

    Steps: strip invalid characters, collapse whitespace, trim leading/trailing
    apostrophes, cap at 31 chars, fall back to a placeholder if empty, then
    resolve collisions deterministically by appending ``_2``, ``_3`` ...
    """
    cleaned = _INVALID_SHEET_CHARS.sub("", title or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip("'")
    if not cleaned:
        cleaned = "Ledger"
    cleaned = cleaned[:SHEET_NAME_MAX]

    candidate = cleaned
    suffix = 2
    lowered_used = {u.lower() for u in used}
    while candidate.lower() in lowered_used:
        tail = f"_{suffix}"
        candidate = cleaned[: SHEET_NAME_MAX - len(tail)] + tail
        suffix += 1
    return candidate


def assign_sheet_names(ledgers: list[FormalizedLedger]) -> None:
    """Populate ``sheet_name`` on each ledger, avoiding reserved/collision names."""
    used: set[str] = set(RESERVED_SHEETS)
    for ledger in ledgers:
        name = sanitize_sheet_name(ledger.identity.detected_title, used)
        ledger.sheet_name = name
        used.add(name)


def _ledger_df(ledger: FormalizedLedger) -> pd.DataFrame:
    records = [r.as_dict() for r in ledger.rows]
    return pd.DataFrame(records, columns=LEDGER_SHEET_COLUMNS)


def _audit_df(ledgers: list[FormalizedLedger]) -> pd.DataFrame:
    records = [a.as_dict() for ledger in ledgers for a in ledger.audit]
    return pd.DataFrame(records, columns=AUDIT_COLUMNS)


def _validation_df(items: list[ValidationItem]) -> pd.DataFrame:
    records = [i.as_dict() for i in items]
    return pd.DataFrame(records, columns=VALIDATION_COLUMNS)


def _page_analysis_df(ledgers: list[FormalizedLedger]) -> pd.DataFrame:
    records = []
    for ledger in ledgers:
        for pa in ledger.page_analysis:
            records.append(
                {
                    "ledger_role": ledger.ledger_role,
                    "sheet_name": ledger.sheet_name,
                    "page_number": pa.page_number,
                    "extraction_strategy_used": pa.extraction_strategy_used,
                    "confidence_score": pa.confidence_score,
                    "detected_headers": " | ".join(pa.detected_headers),
                    "detected_columns": ", ".join(pa.detected_columns),
                    "column_confidence": str(pa.column_confidence),
                    "transaction_row_count": pa.transaction_row_count,
                    "special_row_count": pa.special_row_count,
                    "unknown_row_count": pa.unknown_row_count,
                    "review_row_count": pa.review_row_count,
                }
            )
    return pd.DataFrame(records, columns=PAGE_ANALYSIS_COLUMNS)


def _review_df(ledgers: list[FormalizedLedger]) -> pd.DataFrame:
    records = []
    for ledger in ledgers:
        for row in ledger.rows:
            if row.review_flag:
                records.append({col: getattr(row, col, "") for col in REVIEW_COLUMNS})
    return pd.DataFrame(records, columns=REVIEW_COLUMNS)


def _readme_text(pair_id: str, ledgers: list[FormalizedLedger]) -> str:
    generated = datetime.now(timezone.utc).isoformat()
    lines = [
        "FORMALIZED LEDGERS (deterministic formalization stage).",
        "",
        "This workbook contains two standardized/formalized ledger sheets produced",
        "deterministically from the source PDFs by Python (PyMuPDF word-layout).",
        "NO reconciliation, matching, annexure, or final-balance logic is performed.",
        "Type labels (Invoice, Payment, ...) are standardization labels only and are",
        "NOT reconciliation conclusions. 'Invoice' is neutral and covers org-side",
        "Sale and party-side Purchase.",
        "",
        "Organisation perspective: source debit/credit are preserved verbatim in the",
        "*_source columns. *_org_perspective columns are derived reversibly - the org",
        "ledger maps directly; the party ledger is mirrored (debit<->credit) so both",
        "books can be read from the organisation's point of view. Source is never lost.",
        "",
        f"pair_id: {pair_id}",
        f"generated (UTC): {generated}",
        "",
        "Ledger identity -> sheet name:",
    ]
    for ledger in ledgers:
        ident = ledger.identity
        lines.append(
            f"  {ledger.ledger_role}: detected '{ident.detected_title}' "
            f"(method={ident.detection_method}, confidence={ident.confidence:.2f}) "
            f"-> sheet '{ledger.sheet_name}'"
        )
    lines += [
        "",
        "Sheets:",
        "  README             - this sheet.",
    ]
    for ledger in ledgers:
        lines.append(f"  {ledger.sheet_name:<18} - {ledger.ledger_role} formalized rows.")
    lines += [
        "  Extraction_Audit   - per-page extraction status and counts.",
        "  Page_Analysis     - per-page strategy and confidence analysis.",
        "  Validation_Report  - per-check validation results.",
        "  Review_Queue       - low-confidence / unknown / ambiguous rows for review.",
    ]
    return "\n".join(lines)


def write_formalized_workbook(
    output_path: Path,
    pair_id: str,
    ledgers: list[FormalizedLedger],
    validation_items: list[ValidationItem],
) -> dict[str, str]:
    """Write the formalized workbook. Returns the role->sheet-name mapping."""
    data_sheets: list[tuple[str, pd.DataFrame]] = []
    for ledger in ledgers:
        data_sheets.append((ledger.sheet_name, _ledger_df(ledger)))
    data_sheets.append(("Extraction_Audit", _audit_df(ledgers)))
    data_sheets.append(("Page_Analysis", _page_analysis_df(ledgers)))
    data_sheets.append(("Validation_Report", _validation_df(validation_items)))
    data_sheets.append(("Review_Queue", _review_df(ledgers)))

    readme_text = _readme_text(pair_id, ledgers)
    write_workbook(output_path, readme_text, data_sheets)
    return {ledger.ledger_role: ledger.sheet_name for ledger in ledgers}
