"""Tests for reference-derived workbook theming and its safe invariants."""

from __future__ import annotations

import importlib

from src.reconciliation.reference_style import WorkbookTheme, derive_theme


def test_theme_falls_back_to_curated_default_when_no_profile() -> None:
    theme = derive_theme({})  # empty profile -> curated default
    assert theme.source == "curated_default"
    assert "#,##0" in theme.amount_format
    assert theme.page_orientation in ("portrait", "landscape")


def test_theme_adopts_reference_header_fill_and_page_setup() -> None:
    profile = {
        "sheets": [
            {
                "sheet_name": "Summary",
                "is_summary_like": True,
                "is_ledger_like": False,
                "title_style": {"fill_color": "112233"},
                "header_cell_styles": [
                    {"fill_color": "445566", "font_color": "FFFFFF", "font_name": "Arial"},
                    {"fill_color": "445566", "font_color": "FFFFFF", "font_name": "Arial"},
                ],
                "page_setup": {"orientation": "landscape", "fit_to_width": 1},
            }
        ]
    }
    theme = derive_theme(profile)
    assert theme.header_fill == "445566"
    assert theme.banner_fill == "112233"
    assert theme.title_font_name == "Arial"
    assert theme.page_orientation == "landscape"
    assert theme.source.startswith("reference_profile:")


def test_apply_theme_preserves_audited_invariants() -> None:
    # Rebinding must keep bold headers and a thousands-separator amount format,
    # otherwise the formula/format audits and presentation tests would break.
    st = importlib.import_module("src.reconciliation.workbook_style")
    try:
        st.apply_theme(WorkbookTheme(header_fill="123456", amount_format="bad-format"))
        assert st.FONT_HEADER.bold is True
        assert "#,##0" in st.FMT_AMOUNT  # guarded: bad format rejected
        assert st.FILL_HEADER.fgColor.rgb.endswith("123456")
    finally:
        # Restore module defaults so global rebinding never leaks between tests.
        importlib.reload(st)
