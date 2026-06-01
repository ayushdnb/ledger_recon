"""Raw PDF context extraction for a ledger pair.

Reads the org-ledger and party-ledger PDFs for a given pair and writes their raw
text, lines, and tables into an Excel workbook. This is the raw-extraction stage
only: no column standardization, no debit/credit interpretation, no reconciliation,
no OCR, no AI.

Run from the project root:

    python -m src.extraction.extract_pdf_pair_to_workbook --pair-id pair_001_baby_and_mom__good_luck
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

import fitz  # PyMuPDF
import pandas as pd
import pdfplumber
from openpyxl.utils import get_column_letter

from src.config import settings

logger = logging.getLogger("extract_pdf_pair_to_workbook")

# Order is significant: org ledger first, party ledger second.
LEDGER_ROLES: tuple[str, ...] = ("org_ledger", "party_ledger")

EXCEL_CELL_LIMIT = 32767
TEXT_CHUNK_MAX_CHARS = 30000
MAX_TABLE_COLS = 15
COLUMN_WIDTH_CAP = 60

PYMUPDF_METHOD = "pymupdf"
PDFPLUMBER_METHOD = "pdfplumber"


def sha256_file(path: Path) -> str:
    """Return the hex SHA256 digest of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def chunk_text(text: str, max_chars: int = TEXT_CHUNK_MAX_CHARS) -> list[str]:
    """Split text into chunks no larger than max_chars (Excel cell-safe).

    Always returns at least one chunk (an empty string for empty input) so that
    every page is represented in the output.
    """
    if not text:
        return [""]
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


def discover_pair_pdfs(pair_root: Path) -> dict[str, list[Path]]:
    """Discover org-ledger and party-ledger PDFs for a pair.

    Looks under ``pair_root/input/<role>/*.pdf``. Fails clearly if either role
    folder is missing or contains no PDF.
    """
    if not pair_root.exists():
        raise FileNotFoundError(f"Pair workspace not found: {pair_root}")

    discovered: dict[str, list[Path]] = {}
    for role in LEDGER_ROLES:
        role_dir = pair_root / "input" / role
        if not role_dir.exists():
            raise FileNotFoundError(
                f"Missing input folder for role '{role}': {role_dir}"
            )
        pdfs = sorted(p for p in role_dir.glob("*.pdf") if p.is_file())
        if not pdfs:
            raise FileNotFoundError(
                f"No PDF found for role '{role}' in: {role_dir}"
            )
        discovered[role] = pdfs

    return discovered


def extract_pdf_text_with_pymupdf(
    pdf_path: Path,
    pair_id: str,
    ledger_role: str,
) -> dict[str, list[dict]]:
    """Extract page-level text, lines, blocks, and words from a PDF using PyMuPDF.

    Returns a dict with ``page_text`` rows (chunked), ``text_lines`` rows,
    ``text_blocks`` rows, ``words`` rows, and ``audit`` rows. Per-page errors are
    logged and recorded but do not abort the rest of the document.
    """
    source_file_name = pdf_path.name
    page_text_rows: list[dict] = []
    text_line_rows: list[dict] = []
    text_block_rows: list[dict] = []
    word_rows: list[dict] = []
    audit_rows: list[dict] = []

    with fitz.open(pdf_path) as doc:
        page_count = doc.page_count
        for page_index in range(page_count):
            page_number = page_index + 1
            try:
                page = doc.load_page(page_index)
                text = page.get_text("text") or ""

                for chunk_index, chunk in enumerate(chunk_text(text)):
                    page_text_rows.append(
                        {
                            "pair_id": pair_id,
                            "ledger_role": ledger_role,
                            "source_file_name": source_file_name,
                            "page_number": page_number,
                            "extraction_method": PYMUPDF_METHOD,
                            "text_chunk_index": chunk_index,
                            "text_chunk": chunk,
                            "char_count": len(chunk),
                        }
                    )

                lines = text.split("\n")
                for line_index, line_text in enumerate(lines):
                    text_line_rows.append(
                        {
                            "pair_id": pair_id,
                            "ledger_role": ledger_role,
                            "source_file_name": source_file_name,
                            "page_number": page_number,
                            "line_number": line_index + 1,
                            "line_text": line_text,
                        }
                    )

                # PyMuPDF text blocks: tuples of
                # (x0, y0, x1, y1, "text", block_no, block_type).
                blocks = page.get_text("blocks") or []
                for block_index, block in enumerate(blocks):
                    x0, y0, x1, y1 = block[0], block[1], block[2], block[3]
                    block_text = block[4] if len(block) > 4 else ""
                    if block_text is None:
                        block_text = ""
                    block_text = chunk_text(block_text)[0]
                    text_block_rows.append(
                        {
                            "pair_id": pair_id,
                            "ledger_role": ledger_role,
                            "source_file_name": source_file_name,
                            "page_number": page_number,
                            "block_index": block_index,
                            "block_text": block_text,
                            "bbox_x0": x0,
                            "bbox_y0": y0,
                            "bbox_x1": x1,
                            "bbox_y1": y1,
                            "char_count": len(block_text),
                        }
                    )

                # PyMuPDF words: tuples of
                # (x0, y0, x1, y1, "word", block_no, line_no, word_no).
                words = page.get_text("words") or []
                for word_index, word in enumerate(words):
                    x0, y0, x1, y1 = word[0], word[1], word[2], word[3]
                    word_text = word[4] if len(word) > 4 else ""
                    if word_text is None:
                        word_text = ""
                    word_rows.append(
                        {
                            "pair_id": pair_id,
                            "ledger_role": ledger_role,
                            "source_file_name": source_file_name,
                            "page_number": page_number,
                            "word_index": word_index,
                            "word_text": word_text,
                            "bbox_x0": x0,
                            "bbox_y0": y0,
                            "bbox_x1": x1,
                            "bbox_y1": y1,
                        }
                    )

                audit_rows.append(
                    {
                        "pair_id": pair_id,
                        "ledger_role": ledger_role,
                        "source_file_name": source_file_name,
                        "page_number": page_number,
                        "status": "ok",
                        "message": (
                            f"pymupdf extracted {len(text)} chars, {len(lines)} lines, "
                            f"{len(blocks)} blocks, {len(words)} words"
                        ),
                        "exception_type": "",
                    }
                )
            except Exception as exc:  # noqa: BLE001 - record and continue
                logger.warning(
                    "pymupdf failed on %s page %s: %s",
                    source_file_name,
                    page_number,
                    exc,
                )
                audit_rows.append(
                    {
                        "pair_id": pair_id,
                        "ledger_role": ledger_role,
                        "source_file_name": source_file_name,
                        "page_number": page_number,
                        "status": "error",
                        "message": str(exc),
                        "exception_type": type(exc).__name__,
                    }
                )

    return {
        "page_text": page_text_rows,
        "text_lines": text_line_rows,
        "text_blocks": text_block_rows,
        "words": word_rows,
        "audit": audit_rows,
        "page_count": page_count,
    }


def extract_pdf_tables_with_pdfplumber(
    pdf_path: Path,
    pair_id: str,
    ledger_role: str,
) -> dict[str, list[dict]]:
    """Extract tables from a PDF using pdfplumber.

    Returns a dict with ``tables`` rows (one per table row, raw JSON + first 15
    cells) and ``audit`` rows. None cells become empty strings; nothing else is
    normalized.
    """
    source_file_name = pdf_path.name
    table_rows: list[dict] = []
    audit_rows: list[dict] = []
    diagnostics_rows: list[dict] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf.pages):
            page_number = page_index + 1
            try:
                tables = page.extract_tables() or []
                page_row_count = 0
                for table_index, table in enumerate(tables):
                    for row_index, row in enumerate(table):
                        page_row_count += 1
                        cells = list(row) if row is not None else []
                        normalized = ["" if c is None else c for c in cells]
                        raw_row_json = json.dumps(normalized, ensure_ascii=False)

                        row_record: dict = {
                            "pair_id": pair_id,
                            "ledger_role": ledger_role,
                            "source_file_name": source_file_name,
                            "page_number": page_number,
                            "table_index": table_index,
                            "table_row_index": row_index,
                            "raw_row_json": raw_row_json,
                        }
                        for col_position in range(MAX_TABLE_COLS):
                            col_name = f"col_{col_position + 1:02d}"
                            if col_position < len(normalized):
                                row_record[col_name] = normalized[col_position]
                            else:
                                row_record[col_name] = ""
                        table_rows.append(row_record)

                warning = "" if tables else "no tables detected by pdfplumber on this page"
                diagnostics_rows.append(
                    {
                        "pair_id": pair_id,
                        "ledger_role": ledger_role,
                        "source_file_name": source_file_name,
                        "page_number": page_number,
                        "method": PDFPLUMBER_METHOD,
                        "table_count": len(tables),
                        "row_count": page_row_count,
                        "warning": warning,
                    }
                )

                audit_rows.append(
                    {
                        "pair_id": pair_id,
                        "ledger_role": ledger_role,
                        "source_file_name": source_file_name,
                        "page_number": page_number,
                        "status": "ok",
                        "message": f"pdfplumber found {len(tables)} table(s)",
                        "exception_type": "",
                    }
                )
            except Exception as exc:  # noqa: BLE001 - record and continue
                logger.warning(
                    "pdfplumber failed on %s page %s: %s",
                    source_file_name,
                    page_number,
                    exc,
                )
                diagnostics_rows.append(
                    {
                        "pair_id": pair_id,
                        "ledger_role": ledger_role,
                        "source_file_name": source_file_name,
                        "page_number": page_number,
                        "method": PDFPLUMBER_METHOD,
                        "table_count": 0,
                        "row_count": 0,
                        "warning": f"pdfplumber error: {exc}",
                    }
                )
                audit_rows.append(
                    {
                        "pair_id": pair_id,
                        "ledger_role": ledger_role,
                        "source_file_name": source_file_name,
                        "page_number": page_number,
                        "status": "error",
                        "message": str(exc),
                        "exception_type": type(exc).__name__,
                    }
                )

    return {
        "tables": table_rows,
        "audit": audit_rows,
        "table_diagnostics": diagnostics_rows,
    }


def _autosize_and_freeze(writer: pd.ExcelWriter, sheet_name: str, df: pd.DataFrame) -> None:
    """Freeze the header row and cap-autosize columns on a data sheet."""
    worksheet = writer.sheets[sheet_name]
    worksheet.freeze_panes = "A2"
    for col_index, column in enumerate(df.columns, start=1):
        header_len = len(str(column))
        sample = df[column].astype(str).head(200)
        max_cell = int(sample.map(len).max()) if not sample.empty else 0
        width = min(max(header_len, max_cell) + 2, COLUMN_WIDTH_CAP)
        worksheet.column_dimensions[get_column_letter(col_index)].width = width


def build_workbook(
    output_path: Path,
    pair_id: str,
    extraction_timestamp: str,
    manifest_rows: list[dict],
    page_text_rows: list[dict],
    text_line_rows: list[dict],
    text_block_rows: list[dict],
    word_rows: list[dict],
    table_rows: list[dict],
    table_diagnostics_rows: list[dict],
    audit_rows: list[dict],
    summary_rows: list[dict],
) -> None:
    """Write all sheets to an Excel workbook with openpyxl."""
    readme_text = (
        "RAW EXTRACTION CONTEXT ONLY.\n\n"
        "This workbook contains the raw text, lines, blocks, words, and tables "
        "extracted from the source PDFs for this ledger pair. No AI standardization, "
        "no column mapping, no debit/credit interpretation, and no reconciliation has "
        "been performed.\n\n"
        f"pair_id: {pair_id}\n"
        f"generated (UTC, timezone-aware ISO 8601): {extraction_timestamp}\n\n"
        "Sheets:\n"
        "  Source_Manifest                 - source files, sizes, SHA256, methods used.\n"
        "  PDF_Page_Text                   - per-page text (PyMuPDF), chunked for Excel limits.\n"
        "  PDF_Text_Lines                  - per-page text split into lines (PyMuPDF).\n"
        "  PDF_Text_Blocks                 - per-page text blocks with bounding boxes (PyMuPDF).\n"
        "  PDF_Words                       - per-page words with bounding boxes (PyMuPDF).\n"
        "  PDF_Tables_Raw                  - per-page raw table rows (pdfplumber).\n"
        "  PDF_Table_Extraction_Diagnostics- per-page table extraction status / counts.\n"
        "  Extraction_Audit                - per-page status / warnings / exceptions.\n"
        "  Extraction_Summary              - per-file counts.\n"
    )
    readme_df = pd.DataFrame({"info": [readme_text]})

    manifest_columns = [
        "pair_id",
        "ledger_role",
        "source_file_name",
        "source_file_path",
        "file_size_bytes",
        "sha256",
        "extraction_timestamp",
        "extraction_methods_used",
    ]
    page_text_columns = [
        "pair_id",
        "ledger_role",
        "source_file_name",
        "page_number",
        "extraction_method",
        "text_chunk_index",
        "text_chunk",
        "char_count",
    ]
    text_line_columns = [
        "pair_id",
        "ledger_role",
        "source_file_name",
        "page_number",
        "line_number",
        "line_text",
    ]
    text_block_columns = [
        "pair_id",
        "ledger_role",
        "source_file_name",
        "page_number",
        "block_index",
        "block_text",
        "bbox_x0",
        "bbox_y0",
        "bbox_x1",
        "bbox_y1",
        "char_count",
    ]
    word_columns = [
        "pair_id",
        "ledger_role",
        "source_file_name",
        "page_number",
        "word_index",
        "word_text",
        "bbox_x0",
        "bbox_y0",
        "bbox_x1",
        "bbox_y1",
    ]
    table_diagnostics_columns = [
        "pair_id",
        "ledger_role",
        "source_file_name",
        "page_number",
        "method",
        "table_count",
        "row_count",
        "warning",
    ]
    table_columns = [
        "pair_id",
        "ledger_role",
        "source_file_name",
        "page_number",
        "table_index",
        "table_row_index",
        "raw_row_json",
    ] + [f"col_{i + 1:02d}" for i in range(MAX_TABLE_COLS)]
    audit_columns = [
        "pair_id",
        "ledger_role",
        "source_file_name",
        "page_number",
        "status",
        "message",
        "exception_type",
    ]
    summary_columns = [
        "pair_id",
        "ledger_role",
        "source_file_name",
        "page_count",
        "extracted_text_pages",
        "extracted_line_count",
        "extracted_block_count",
        "extracted_word_count",
        "extracted_table_count",
        "extracted_table_row_count",
        "warnings_count",
    ]

    manifest_df = pd.DataFrame(manifest_rows, columns=manifest_columns)
    page_text_df = pd.DataFrame(page_text_rows, columns=page_text_columns)
    text_line_df = pd.DataFrame(text_line_rows, columns=text_line_columns)
    text_block_df = pd.DataFrame(text_block_rows, columns=text_block_columns)
    word_df = pd.DataFrame(word_rows, columns=word_columns)
    table_df = pd.DataFrame(table_rows, columns=table_columns)
    table_diagnostics_df = pd.DataFrame(
        table_diagnostics_rows, columns=table_diagnostics_columns
    )
    audit_df = pd.DataFrame(audit_rows, columns=audit_columns)
    summary_df = pd.DataFrame(summary_rows, columns=summary_columns)

    data_sheets = [
        ("Source_Manifest", manifest_df),
        ("PDF_Page_Text", page_text_df),
        ("PDF_Text_Lines", text_line_df),
        ("PDF_Text_Blocks", text_block_df),
        ("PDF_Words", word_df),
        ("PDF_Tables_Raw", table_df),
        ("PDF_Table_Extraction_Diagnostics", table_diagnostics_df),
        ("Extraction_Audit", audit_df),
        ("Extraction_Summary", summary_df),
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        readme_df.to_excel(writer, sheet_name="README", index=False)
        for sheet_name, df in data_sheets:
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            _autosize_and_freeze(writer, sheet_name, df)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_extraction(pair_id: str, pair_root: Path) -> dict[str, Path]:
    """Extract raw context for a pair and write the workbook(s)."""
    pair_dir = pair_root / pair_id
    discovered = discover_pair_pdfs(pair_dir)
    extraction_timestamp = _now_iso()

    manifest_rows: list[dict] = []
    page_text_rows: list[dict] = []
    text_line_rows: list[dict] = []
    text_block_rows: list[dict] = []
    word_rows: list[dict] = []
    table_rows: list[dict] = []
    table_diagnostics_rows: list[dict] = []
    audit_rows: list[dict] = []
    summary_rows: list[dict] = []

    for role in LEDGER_ROLES:
        for pdf_path in discovered[role]:
            logger.info("Extracting %s ledger: %s", role, pdf_path.name)

            text_result = extract_pdf_text_with_pymupdf(pdf_path, pair_id, role)
            table_result = extract_pdf_tables_with_pdfplumber(pdf_path, pair_id, role)

            page_text_rows.extend(text_result["page_text"])
            text_line_rows.extend(text_result["text_lines"])
            text_block_rows.extend(text_result["text_blocks"])
            word_rows.extend(text_result["words"])
            table_rows.extend(table_result["tables"])
            table_diagnostics_rows.extend(table_result["table_diagnostics"])

            file_audit = text_result["audit"] + table_result["audit"]
            audit_rows.extend(file_audit)

            manifest_rows.append(
                {
                    "pair_id": pair_id,
                    "ledger_role": role,
                    "source_file_name": pdf_path.name,
                    "source_file_path": str(pdf_path),
                    "file_size_bytes": pdf_path.stat().st_size,
                    "sha256": sha256_file(pdf_path),
                    "extraction_timestamp": extraction_timestamp,
                    "extraction_methods_used": f"{PYMUPDF_METHOD},{PDFPLUMBER_METHOD}",
                }
            )

            text_pages = {
                r["page_number"]
                for r in text_result["page_text"]
                if r["char_count"] > 0
            }
            table_indices = {
                (r["page_number"], r["table_index"]) for r in table_result["tables"]
            }
            warnings_count = sum(1 for r in file_audit if r["status"] != "ok")

            summary_rows.append(
                {
                    "pair_id": pair_id,
                    "ledger_role": role,
                    "source_file_name": pdf_path.name,
                    "page_count": text_result["page_count"],
                    "extracted_text_pages": len(text_pages),
                    "extracted_line_count": len(text_result["text_lines"]),
                    "extracted_block_count": len(text_result["text_blocks"]),
                    "extracted_word_count": len(text_result["words"]),
                    "extracted_table_count": len(table_indices),
                    "extracted_table_row_count": len(table_result["tables"]),
                    "warnings_count": warnings_count,
                }
            )

    workbook_name = f"raw_pdf_context__{pair_id}.xlsx"
    pair_output_path = pair_dir / "output" / workbook_name

    build_workbook(
        output_path=pair_output_path,
        pair_id=pair_id,
        extraction_timestamp=extraction_timestamp,
        manifest_rows=manifest_rows,
        page_text_rows=page_text_rows,
        text_line_rows=text_line_rows,
        text_block_rows=text_block_rows,
        word_rows=word_rows,
        table_rows=table_rows,
        table_diagnostics_rows=table_diagnostics_rows,
        audit_rows=audit_rows,
        summary_rows=summary_rows,
    )

    central_dir = settings.resolved(settings.raw_context_output_dir)
    central_dir.mkdir(parents=True, exist_ok=True)
    central_output_path = central_dir / workbook_name
    shutil.copyfile(pair_output_path, central_output_path)

    return {"pair_output": pair_output_path, "central_output": central_output_path}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Raw PDF context extraction for a ledger pair (no standardization, no reconciliation).",
    )
    parser.add_argument(
        "--pair-id",
        default=settings.default_pair_id,
        help="Pair id, e.g. pair_001_baby_and_mom__good_luck",
    )
    parser.add_argument(
        "--pair-root",
        default=None,
        help="Override the work-pairs root (default from settings.input_pair_root).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.pair_root is not None:
        pair_root = Path(args.pair_root)
        if not pair_root.is_absolute():
            pair_root = settings.project_root / pair_root
    else:
        pair_root = settings.resolved(settings.input_pair_root)

    logger.info("Pair id: %s", args.pair_id)
    logger.info("Pair root: %s", pair_root)

    outputs = run_extraction(args.pair_id, pair_root)

    logger.info("Workbook written: %s", outputs["pair_output"])
    logger.info("Central copy written: %s", outputs["central_output"])
    print(f"Pair workbook:   {outputs['pair_output']}")
    print(f"Central workbook: {outputs['central_output']}")


if __name__ == "__main__":
    main()
