"""Build the dynamic, label-driven Summary layout.

The layout is structural only: it declares the ordered rows and what each row
*means* (its ``kind``). The workbook writer is responsible for turning those
kinds into the correct formulas and styling, so financial logic stays in one
place and can never be silently duplicated here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

OPENING_LABEL = "OpeningBalance"
CLOSING_LABEL = "ClosingBalance"

SUMMARY_COLUMNS = [
    "S.No.",
    "Particulars",
    "Org Ledger Amount",
    "Party Ledger Amount",
    "Difference",
    "Annexure",
    "Review Status",
    "Remarks",
    "Reviewer Comment",
]

# Column letters for the summary table (1-based positions of SUMMARY_COLUMNS).
COL_SERIAL = "A"
COL_PARTICULARS = "B"
COL_ORG = "C"
COL_PARTY = "D"
COL_DIFF = "E"
COL_ANNEX = "F"
COL_REVIEW = "G"
COL_REMARKS = "H"
COL_REVIEWER = "I"


@dataclass
class SummaryRow:
    """One declarative summary row."""

    serial: object
    particulars: str
    kind: str
    label: str = ""
    annexure: str = ""
    review_status: str = ""
    remarks: str = ""


@dataclass
class SummaryLayout:
    """Declarative summary layout consumed by the workbook writer."""

    columns: list[str]
    identity_fields: list[tuple[str, str]]
    rows: list[SummaryRow]
    label_row_map: dict[str, int] = field(default_factory=dict)


def build_summary_layout(
    labels: list[str],
    *,
    include_zero_labels: bool = True,
    annex_sheet_by_label: dict[str, str] | None = None,
) -> SummaryLayout:
    """Create the ordered summary rows for the configured labels.

    ``OpeningBalance`` / ``ClosingBalance`` labels are surfaced as the dedicated
    Opening Balance / Closing Balance rows rather than duplicated in the generic
    label loop, mirroring the reference workbook layout while staying dynamic.
    """
    annex_sheet_by_label = annex_sheet_by_label or {}

    def annex_for(label: str) -> str:
        return annex_sheet_by_label.get(label, f"Annex_{label}")

    identity_fields = [
        ("Vendor / Party Name", "party_name"),
        ("Recon Period", "recon_period"),
        ("Generated Date", "generated_date"),
        ("Org Ledger", "org_ledger"),
        ("Party Ledger", "party_ledger"),
        ("Status", "status"),
    ]

    rows: list[SummaryRow] = []
    serial = 1

    rows.append(
        SummaryRow(serial, "Opening Balance", "opening", OPENING_LABEL, annex_for(OPENING_LABEL),
                   "Formula-linked", "")
    )
    serial += 1

    body_labels = [l for l in labels if l not in {OPENING_LABEL, CLOSING_LABEL}]
    if not include_zero_labels:
        body_labels = [l for l in body_labels if l]
    for label in body_labels:
        rows.append(
            SummaryRow(serial, label, "label", label, annex_for(label), "Formula-linked", "")
        )
        serial += 1

    rows.append(
        SummaryRow(serial, "Closing Balance", "closing", CLOSING_LABEL, annex_for(CLOSING_LABEL),
                   "Formula-linked", "")
    )
    serial += 1

    tail = [
        ("Balance as per Org ledger", "balance_org", "Formula-linked",
         "Opening + movements (org working ledger)."),
        ("Balance as per Party ledger", "balance_party", "Formula-linked",
         "Opening + movements (party working ledger)."),
        ("Matched Amount", "matched", "Accepted matches", "Deterministic strong + post-validated AI matches."),
        ("Candidate Review Amount", "candidate_review", "Review required", "See Review_Queue."),
        ("Unmatched Org Amount", "unmatched_org", "Review required", "See Review_Queue."),
        ("Unmatched Party Amount", "unmatched_party", "Review required", "See Review_Queue."),
        ("Review Pending Amount", "review_pending", "Review required", "See Review_Queue."),
        ("Net Difference", "net_diff", "Formula-linked", "Org closing minus party closing."),
        ("Closing Balance Difference", "closing_diff", "Formula-linked",
         "Stated closing: org vs party."),
        ("Validation Status", "validation_status", "", "See Validation_Report."),
        ("Formula Audit Status", "formula_audit_status", "", "See Formula_Audit."),
    ]
    for particulars, kind, review_status, remarks in tail:
        rows.append(SummaryRow(serial, particulars, kind, "", "", review_status, remarks))
        serial += 1

    return SummaryLayout(
        columns=list(SUMMARY_COLUMNS),
        identity_fields=identity_fields,
        rows=rows,
    )
