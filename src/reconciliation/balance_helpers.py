"""Shared closing-balance helpers for reconciliation validation and review queue."""

from __future__ import annotations

from src.reconciliation.recon_models import ReconRow

CLOSING_LABEL = "ClosingBalance"


def _row_order_key(row: ReconRow) -> tuple[int, int, str]:
    page = 0
    row_num = 0
    try:
        page = int(row.data.get("page_number") or 0)
    except (TypeError, ValueError):
        pass
    try:
        row_num = int(row.data.get("source_row_number") or 0)
    except (TypeError, ValueError):
        pass
    return (page, row_num, row.row_id)


def final_closing_balance(rows: list[ReconRow]) -> float | None:
    """Return the final-period stated closing balance (never sum across sections).

    When multiple closing-balance rows exist (repeated fiscal sections), select the
    row with the highest page/source order — the last closing in document order.
    """
    final = final_closing_row(rows)
    return final.net_org if final is not None else None


def final_closing_row(rows: list[ReconRow]) -> ReconRow | None:
    """Return the final-period closing-balance row with its source trace."""
    closing_rows = [r for r in rows if r.type_label == CLOSING_LABEL]
    return max(closing_rows, key=_row_order_key) if closing_rows else None
