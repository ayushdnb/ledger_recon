"""Shared, read/write Excel helpers used across the standardization stage.

Keeps the workbook style consistent with the raw-extraction workbook: a README
sheet plus data sheets with a frozen header row and capped auto-sized columns.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from openpyxl.utils import get_column_letter

COLUMN_WIDTH_CAP = 60


def autosize_and_freeze(writer: pd.ExcelWriter, sheet_name: str, df: pd.DataFrame) -> None:
    """Freeze the header row and cap-autosize columns on a data sheet."""
    worksheet = writer.sheets[sheet_name]
    worksheet.freeze_panes = "A2"
    for col_index, column in enumerate(df.columns, start=1):
        header_len = len(str(column))
        sample = df[column].head(200)
        # Compute lengths defensively: missing values (NaN / pd.NA) are treated as
        # empty so we never call len() on a float/NA.
        lengths = [0 if pd.isna(value) else len(str(value)) for value in sample]
        max_cell = max(lengths) if lengths else 0
        width = min(max(header_len, max_cell) + 2, COLUMN_WIDTH_CAP)
        worksheet.column_dimensions[get_column_letter(col_index)].width = width


def write_workbook(
    output_path: Path,
    readme_text: str,
    data_sheets: list[tuple[str, pd.DataFrame]],
) -> None:
    """Write a README sheet followed by the given data sheets."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    readme_df = pd.DataFrame({"info": [readme_text]})
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        readme_df.to_excel(writer, sheet_name="README", index=False)
        for sheet_name, df in data_sheets:
            safe_df = df if not df.empty else pd.DataFrame(columns=df.columns)
            safe_df.to_excel(writer, sheet_name=sheet_name, index=False)
            autosize_and_freeze(writer, sheet_name, safe_df)
