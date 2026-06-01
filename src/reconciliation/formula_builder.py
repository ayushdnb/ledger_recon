"""Formula helpers and audit extraction.

The audit is intentionally strict: it fails when a required computed cell is
blank/not a formula, when a required formula does not reference the expected
columns, and when a protected evidence cell unexpectedly contains a formula.
This prevents a false ``PASS`` when a formula is written into the wrong column.
"""

from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from src.reconciliation.recon_models import FormulaAuditItem


def is_formula(value: object) -> bool:
    return isinstance(value, str) and value.startswith("=")


def audit_formula_cells(
    ws: Worksheet,
    cells: list[str],
    category: str,
    *,
    expected_contains: list[str] | None = None,
) -> list[FormulaAuditItem]:
    """Audit required formula cells.

    A cell PASSES only when it contains a formula and, if ``expected_contains``
    is given, that formula references every required token (e.g. the source
    columns a difference formula must subtract). Otherwise it FAILS, so a
    formula written into the wrong column or referencing the wrong cells cannot
    masquerade as a pass.
    """
    items: list[FormulaAuditItem] = []
    for cell in cells:
        value = ws[cell].value
        present = is_formula(value)
        status = "PASS" if present else "FAIL"
        issue = "" if present else "Expected formula missing or cell is blank."
        if present and expected_contains:
            missing = [tok for tok in expected_contains if tok not in str(value)]
            if missing:
                status = "FAIL"
                issue = f"Formula does not reference expected column(s): {', '.join(missing)}."
        items.append(
            FormulaAuditItem(
                sheet_name=ws.title,
                cell=cell,
                expected_formula_category=category,
                formula_present=present,
                formula_text=str(value or ""),
                status=status,
                issue=issue,
            )
        )
    return items


def audit_protected_cells(
    ws: Worksheet,
    cells: list[str],
    category: str,
) -> list[FormulaAuditItem]:
    """Audit protected evidence cells, which must NOT contain a formula.

    Source/evidence columns (raw type, particulars, references, raw text,
    source file/row id) must never be overwritten by a formula. A formula in
    such a cell is a corruption signal and is reported as FAIL.
    """
    items: list[FormulaAuditItem] = []
    for cell in cells:
        value = ws[cell].value
        present = is_formula(value)
        items.append(
            FormulaAuditItem(
                sheet_name=ws.title,
                cell=cell,
                expected_formula_category=category,
                formula_present=present,
                formula_text=str(value or ""),
                status="FAIL" if present else "PASS",
                issue=(
                    "Protected evidence cell must not contain a formula."
                    if present
                    else ""
                ),
            )
        )
    return items

