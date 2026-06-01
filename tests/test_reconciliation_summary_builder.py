from __future__ import annotations

from src.reconciliation.summary_builder import build_summary_layout


def _particulars(layout) -> list[str]:
    return [r.particulars for r in layout.rows]


def test_summary_layout_includes_dynamic_labels() -> None:
    labels = ["OpeningBalance", "Invoice", "Payment", "TDS", "ClosingBalance"]
    layout = build_summary_layout(labels)
    particulars = _particulars(layout)
    assert "Opening Balance" in particulars
    assert "Closing Balance" in particulars
    # Body labels (excluding opening/closing) must each appear as their own row.
    for label in ("Invoice", "Payment", "TDS"):
        assert label in particulars


def test_summary_layout_has_top_block_and_status_rows() -> None:
    layout = build_summary_layout(["Invoice"])
    captions = {c for c, _ in layout.identity_fields}
    assert {"Vendor / Party Name", "Recon Period", "Status"}.issubset(captions)
    kinds = {r.kind for r in layout.rows}
    for kind in (
        "opening", "closing", "balance_org", "balance_party", "matched",
        "candidate_review", "unmatched_org", "unmatched_party", "review_pending",
        "net_diff", "closing_diff", "validation_status", "formula_audit_status",
    ):
        assert kind in kinds, f"missing summary row kind: {kind}"


def test_summary_annexure_links_use_provided_sheet_names() -> None:
    layout = build_summary_layout(
        ["Invoice"], annex_sheet_by_label={"Invoice": "Annex_Invoice"}
    )
    invoice_row = next(r for r in layout.rows if r.label == "Invoice")
    assert invoice_row.annexure == "Annex_Invoice"
