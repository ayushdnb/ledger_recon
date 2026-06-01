"""Derive a curated visual theme from the reference workbook profile.

The reference workbook (STARTLING ACHIEVEMENT) is profiled read-only by
:mod:`src.excel.profile_reference_workbook`, which now captures fonts, fills,
freeze panes, column widths and page setup. This module turns that structural
profile into a small, robust :class:`WorkbookTheme` that the workbook writer can
apply on top of its clean default palette.

Design:
- Every field has a safe fallback to the existing curated default, so a missing
  or partial profile never degrades output.
- We only adopt reference colors when they are real solid fills (this avoids
  importing noise from a heavily hand-formatted source workbook).
- Financial values are never touched; this is presentation only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.reconciliation import workbook_style as st
from src.reconciliation.reference_profile import load_existing_reference_profile


@dataclass
class WorkbookTheme:
    """A curated, fallback-safe set of presentation choices."""

    banner_fill: str = st.COLOR_BANNER
    header_fill: str = st.COLOR_HEADER_DARK
    header_font_color: str = st.COLOR_WHITE
    section_fill: str = st.COLOR_SECTION
    title_font_name: str = "Calibri"
    amount_format: str = st.FMT_AMOUNT
    date_format: str = st.FMT_DATE
    page_orientation: str = "landscape"
    fit_to_width: int = 1
    source: str = "curated_default"


def _looks_like_color(value: object) -> bool:
    return isinstance(value, str) and len(value) in (6, 8) and value not in (
        "",
        "00000000",
        "FFFFFFFF",
        "FFFFFF",
    )


def _pick_reference_sheet(profile: dict[str, Any]) -> Optional[dict]:
    sheets = profile.get("sheets") or []
    summary = [s for s in sheets if s.get("is_summary_like")]
    if summary:
        return summary[0]
    ledger = [s for s in sheets if s.get("is_ledger_like")]
    if ledger:
        return ledger[0]
    return sheets[0] if sheets else None


def _dominant_header_fill(sheet: dict) -> str:
    counts: dict[str, int] = {}
    for style in sheet.get("header_cell_styles") or []:
        fill = style.get("fill_color")
        if _looks_like_color(fill):
            counts[fill] = counts.get(fill, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def derive_theme(profile: dict[str, Any] | None = None) -> WorkbookTheme:
    """Build a :class:`WorkbookTheme` from a reference profile (with fallbacks)."""
    theme = WorkbookTheme()
    if profile is None:
        profile = load_existing_reference_profile()
    if not profile:
        return theme

    sheet = _pick_reference_sheet(profile)
    if not sheet:
        theme.source = "reference_profile_no_sheets"
        return theme

    # Header band color/font from the reference's dominant header cells.
    header_fill = _dominant_header_fill(sheet)
    if header_fill:
        theme.header_fill = header_fill[-6:]
    header_styles = sheet.get("header_cell_styles") or []
    if header_styles:
        font_color = header_styles[0].get("font_color")
        if _looks_like_color(font_color):
            theme.header_font_color = font_color[-6:]
        name = header_styles[0].get("font_name")
        if isinstance(name, str) and name:
            theme.title_font_name = name

    # Banner color from the title cell, when it is a real solid fill.
    title_style = sheet.get("title_style") or {}
    banner_fill = title_style.get("fill_color")
    if _looks_like_color(banner_fill):
        theme.banner_fill = banner_fill[-6:]

    # Page setup from the reference sheet.
    page = sheet.get("page_setup") or {}
    orientation = page.get("orientation")
    if isinstance(orientation, str) and orientation in ("portrait", "landscape"):
        theme.page_orientation = orientation
    fit_w = page.get("fit_to_width")
    if isinstance(fit_w, int) and fit_w > 0:
        theme.fit_to_width = fit_w

    theme.source = f"reference_profile:{sheet.get('sheet_name', '')}"
    return theme
