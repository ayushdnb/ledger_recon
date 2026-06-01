"""Compare a standardized workbook to a reference workbook (structure only).

Reports structural similarity: sheet names, header similarity, ledger-like sheet
counts, column-naming similarity, and presence of summary/review/audit sheets.
It deliberately does NOT compare transaction values and does NOT infer anything
about reconciliation quality.

Run from the project root::

    python -m src.excel.compare_standardized_to_reference --standardized "<path>" --reference "<path>"
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

from src.config import settings
from src.excel.profile_reference_workbook import profile_workbook
from src.excel.workbook_io import write_workbook

logger = logging.getLogger("compare_standardized_to_reference")

SPECIAL_SHEET_KINDS = {
    "summary": ("summary", "overview", "abstract"),
    "review": ("review", "queue"),
    "audit": ("audit", "log", "validation"),
}


def _headers_by_sheet(profile: dict) -> dict[str, list[str]]:
    return {s["sheet_name"]: s["headers"] for s in profile["sheets"]}


def _ledger_like_count(profile: dict) -> int:
    return sum(1 for s in profile["sheets"] if s["is_ledger_like"])


def _detect_special_sheets(profile: dict) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {kind: [] for kind in SPECIAL_SHEET_KINDS}
    for sheet in profile["sheets"]:
        name_lower = sheet["sheet_name"].lower()
        for kind, keywords in SPECIAL_SHEET_KINDS.items():
            if any(k in name_lower for k in keywords):
                found[kind].append(sheet["sheet_name"])
    return found


def _header_set_similarity(left: list[str], right: list[str]) -> float:
    left_text = " ".join(h.lower() for h in left if h)
    right_text = " ".join(h.lower() for h in right if h)
    if not left_text and not right_text:
        return 100.0
    if not left_text or not right_text:
        return 0.0
    return float(fuzz.token_set_ratio(left_text, right_text))


def compare_profiles(standardized: dict, reference: dict) -> dict[str, pd.DataFrame]:
    """Build structural comparison dataframes from two profiles."""
    std_sheets = set(standardized["sheet_names"])
    ref_sheets = set(reference["sheet_names"])

    presence_rows = []
    for name in sorted(std_sheets | ref_sheets):
        presence_rows.append(
            {
                "sheet_name": name,
                "in_standardized": name in std_sheets,
                "in_reference": name in ref_sheets,
            }
        )
    presence_df = pd.DataFrame(
        presence_rows, columns=["sheet_name", "in_standardized", "in_reference"]
    )

    std_headers = _headers_by_sheet(standardized)
    ref_headers = _headers_by_sheet(reference)

    # For each standardized sheet, find the best-matching reference sheet by
    # header similarity (structural only).
    similarity_rows = []
    for std_name, headers in std_headers.items():
        best_ref = None
        best_score = -1.0
        for ref_name, ref_h in ref_headers.items():
            score = _header_set_similarity(headers, ref_h)
            if score > best_score:
                best_score = score
                best_ref = ref_name
        similarity_rows.append(
            {
                "standardized_sheet": std_name,
                "best_matching_reference_sheet": best_ref,
                "header_similarity_score": round(best_score, 1),
                "standardized_header_count": len([h for h in headers if h]),
                "reference_header_count": (
                    len([h for h in ref_headers.get(best_ref, []) if h])
                    if best_ref
                    else 0
                ),
            }
        )
    similarity_df = pd.DataFrame(
        similarity_rows,
        columns=[
            "standardized_sheet",
            "best_matching_reference_sheet",
            "header_similarity_score",
            "standardized_header_count",
            "reference_header_count",
        ],
    )

    std_special = _detect_special_sheets(standardized)
    ref_special = _detect_special_sheets(reference)

    summary_rows = [
        {
            "metric": "sheet_count",
            "standardized": standardized["sheet_count"],
            "reference": reference["sheet_count"],
        },
        {
            "metric": "shared_sheet_names",
            "standardized": len(std_sheets & ref_sheets),
            "reference": len(std_sheets & ref_sheets),
        },
        {
            "metric": "ledger_like_sheet_count",
            "standardized": _ledger_like_count(standardized),
            "reference": _ledger_like_count(reference),
        },
        {
            "metric": "has_summary_sheet",
            "standardized": bool(std_special["summary"]),
            "reference": bool(ref_special["summary"]),
        },
        {
            "metric": "has_review_sheet",
            "standardized": bool(std_special["review"]),
            "reference": bool(ref_special["review"]),
        },
        {
            "metric": "has_audit_sheet",
            "standardized": bool(std_special["audit"]),
            "reference": bool(ref_special["audit"]),
        },
    ]
    summary_df = pd.DataFrame(
        summary_rows, columns=["metric", "standardized", "reference"]
    )

    formatting_rows = []
    for label, profile in (("standardized", standardized), ("reference", reference)):
        total_merged = sum(s["merged_cell_count"] for s in profile["sheets"])
        total_formulas = sum(s["formula_count"] for s in profile["sheets"])
        formatting_rows.append(
            {
                "workbook": label,
                "total_merged_cell_ranges": total_merged,
                "total_formula_cells": total_formulas,
            }
        )
    formatting_df = pd.DataFrame(
        formatting_rows,
        columns=["workbook", "total_merged_cell_ranges", "total_formula_cells"],
    )

    return {
        "Comparison_Summary": summary_df,
        "Sheet_Presence": presence_df,
        "Header_Similarity": similarity_df,
        "Formatting_Notes": formatting_df,
    }


def run_comparison(standardized_path: Path, reference_path: Path) -> Path:
    """Profile both workbooks structurally and write a similarity report."""
    if not standardized_path.exists():
        raise FileNotFoundError(
            f"Standardized workbook not found: {standardized_path}"
        )
    if not reference_path.exists():
        raise FileNotFoundError(f"Reference workbook not found: {reference_path}")

    standardized_profile = profile_workbook(standardized_path)
    reference_profile = profile_workbook(reference_path)
    sheets = compare_profiles(standardized_profile, reference_profile)

    # Output lives under the pair's audit/ folder when the standardized file
    # follows the .../<pair_id>/output/<file>.xlsx layout.
    parent = standardized_path.parent
    if parent.name == "output":
        audit_dir = parent.parent / "audit"
    else:
        audit_dir = parent
    audit_dir.mkdir(parents=True, exist_ok=True)
    output_path = audit_dir / "standardized_vs_reference_similarity.xlsx"

    readme_text = (
        "STRUCTURAL SIMILARITY CHECK (structure only).\n\n"
        "Compares the standardized workbook to the reference workbook by structure: "
        "sheet names, header similarity, ledger-like sheet counts, column naming, and "
        "presence of summary/review/audit sheets. No transaction values were compared "
        "and no reconciliation quality was inferred.\n\n"
        f"standardized: {standardized_path.name}\n"
        f"reference: {reference_path.name}\n"
        f"generated (UTC): {datetime.now(timezone.utc).isoformat()}\n"
    )
    write_workbook(output_path, readme_text, list(sheets.items()))
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Structural similarity check of a standardized workbook vs a reference workbook.",
    )
    parser.add_argument("--standardized", required=True, help="Standardized .xlsx path.")
    parser.add_argument("--reference", required=True, help="Reference .xlsx path.")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    standardized_path = Path(args.standardized)
    if not standardized_path.is_absolute():
        standardized_path = settings.project_root / standardized_path
    reference_path = Path(args.reference)
    if not reference_path.is_absolute():
        reference_path = settings.project_root / reference_path

    output_path = run_comparison(standardized_path, reference_path)
    logger.info("Similarity report: %s", output_path)
    print(f"Similarity report: {output_path}")


if __name__ == "__main__":
    main()
