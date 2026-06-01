"""Build the strict AI standardization prompt.

The prompt is assembled from:
- the raw extraction workbook sheets (text, lines, blocks, tables),
- the reference workbook structural profile (optional context),
- the type-label dictionary (config/type_labels.json),
- the standardized output schema (field names + canonical types).

When the full context exceeds ``AI_MAX_INPUT_CHARS_PER_REQUEST``, it is split into
deterministic chunks by source ledger, page number, and row order. No raw source
text is dropped silently.

The prompt instructs the model to return JSON only (no prose, no markdown), to
use ``null`` for missing values, to preserve page references and raw text, and to
mark ``review_required`` when uncertain. It performs no reconciliation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src.config import settings
from src.standardization.type_labeler import ALLOWED_CANONICAL_TYPES

# Sheets fed to the model, in priority order. Diagnostics/audit are excluded to
# keep the payload focused on source content.
CONTEXT_SHEETS = (
    "PDF_Page_Text",
    "PDF_Text_Lines",
    "PDF_Tables_Raw",
    "PDF_Text_Blocks",
)

SYSTEM_INSTRUCTIONS = (
    "You are a meticulous finance data standardization assistant. You convert raw "
    "ledger text extracted from PDFs into a strict JSON structure. You DO NOT "
    "reconcile, match, or compare the two ledgers. You DO NOT invent values.\n\n"
    "Hard rules:\n"
    "- Return a single JSON object ONLY. No prose. No markdown. No code fences.\n"
    "- Use null for any value that is missing or not present in the source.\n"
    "- Never fabricate dates, amounts, voucher numbers, or names.\n"
    "- Preserve the source page number for every row (source_page).\n"
    "- Preserve the original source text for every transaction row (raw_text).\n"
    "- Set review_required=true (with a short review_reason) whenever a value is "
    "uncertain, ambiguous, or could not be parsed.\n"
    "- Treat opening and closing balances as metadata in opening_closing_balances, "
    "NOT as transaction rows, unless they are clearly presented as transaction "
    "rows in the source.\n"
    "- Standardize BOTH the organisation ledger and the party ledger.\n"
    "- canonical_type MUST be exactly one of: "
    + ", ".join(ALLOWED_CANONICAL_TYPES)
    + ".\n"
    "- Labelling a row's type is standardization only; it is NOT a reconciliation "
    "decision (a Sale on one side may map to a Purchase on the other later)."
)

CHUNK_SYSTEM_SUFFIX = (
    "\n\nThis request covers a PARTIAL context chunk only. Standardize ONLY the "
    "transaction rows and balances visible in the RAW EXTRACTION CONTEXT below. "
    "Do not infer or invent rows from pages not included in this chunk."
)


@dataclass(frozen=True)
class ContextUnit:
    """One auditable row from a context sheet, with ordering metadata."""

    ledger_role: str
    source_file_name: str
    page_number: int
    sheet_name: str
    row_order: int
    line_order: int
    text: str


@dataclass
class ContextChunk:
    """A deterministic slice of raw extraction context for one AI request."""

    chunk_id: str
    source_ledger: str
    page_range: str
    source_files: list[str] = field(default_factory=list)
    context_text: str = ""
    unit_count: int = 0


def _schema_skeleton() -> dict:
    """A compact example of the expected JSON shape (keys only, values null)."""
    row_keys = [
        "source_file",
        "source_page",
        "source_row_number",
        "ledger_role",
        "party_name",
        "ledger_period_start",
        "ledger_period_end",
        "transaction_date",
        "raw_type",
        "canonical_type",
        "type_match_method",
        "type_match_score",
        "voucher_or_bill_no",
        "reference_no",
        "account_or_particulars",
        "narration",
        "debit",
        "credit",
        "net_amount",
        "balance",
        "balance_side",
        "extraction_confidence",
        "review_required",
        "review_reason",
        "raw_text",
        "extraction_method",
    ]
    balance_keys = [
        "balance_kind",
        "ledger_role",
        "source_file_name",
        "amount",
        "balance_side",
        "date_or_period",
        "source_page",
        "source_text",
        "found_location",
        "confidence",
        "review_required",
        "review_reason",
    ]
    row_example = {k: None for k in row_keys}
    row_example["ledger_role"] = "org_ledger"
    row_example["canonical_type"] = "Unknown"
    row_example["review_required"] = False
    balance_example = {k: None for k in balance_keys}
    balance_example["balance_kind"] = "OpeningBalance"
    balance_example["ledger_role"] = "org_ledger"
    balance_example["review_required"] = False
    return {
        "pair_id": "string",
        "detected_org_name": None,
        "detected_party_name": None,
        "detected_account_name": None,
        "detected_period_start": None,
        "detected_period_end": None,
        "opening_closing_balances": [balance_example],
        "org_ledger_rows": [row_example],
        "party_ledger_rows": [row_example],
        "uncertain_fields": [],
        "extraction_warnings": [],
    }


def _sheet_row_to_line(sheet_name: str, row: pd.Series) -> str:
    """Render one context row as a pipe-delimited line preserving column values."""
    return " | ".join("" if pd.isna(v) else str(v) for v in row.tolist())


def _line_order_from_row(sheet_name: str, row: pd.Series, row_order: int) -> int:
    for col in ("line_number", "table_row_index", "block_index", "text_chunk_index"):
        if col in row.index and not pd.isna(row[col]):
            try:
                return int(row[col])
            except (ValueError, TypeError):
                pass
    return row_order


def _iter_context_units(context: dict[str, pd.DataFrame]) -> list[ContextUnit]:
    """Flatten context sheets into sorted, auditable units."""
    sheet_rank = {name: idx for idx, name in enumerate(CONTEXT_SHEETS)}
    units: list[ContextUnit] = []
    for sheet_name in CONTEXT_SHEETS:
        df = context.get(sheet_name)
        if df is None or df.empty:
            continue
        for row_order, (_, row) in enumerate(df.iterrows()):
            ledger_role = "" if pd.isna(row.get("ledger_role")) else str(row["ledger_role"])
            source_file = (
                "" if pd.isna(row.get("source_file_name")) else str(row["source_file_name"])
            )
            page_raw = row.get("page_number")
            try:
                page_number = int(page_raw) if not pd.isna(page_raw) else 0
            except (ValueError, TypeError):
                page_number = 0
            line_order = _line_order_from_row(sheet_name, row, row_order)
            header = (
                f"[{sheet_name}] ledger_role={ledger_role} "
                f"source_file={source_file} page={page_number} order={line_order}"
            )
            units.append(
                ContextUnit(
                    ledger_role=ledger_role,
                    source_file_name=source_file,
                    page_number=page_number,
                    sheet_name=sheet_name,
                    row_order=row_order,
                    line_order=line_order,
                    text=f"{header}\n{_sheet_row_to_line(sheet_name, row)}",
                )
            )
    units.sort(
        key=lambda u: (
            u.ledger_role,
            u.source_file_name,
            u.page_number,
            sheet_rank.get(u.sheet_name, 99),
            u.line_order,
            u.row_order,
        )
    )
    return units


def _units_to_context_text(units: list[ContextUnit]) -> str:
    if not units:
        return "(empty)"
    return "\n".join(u.text for u in units)


def _page_range_label(pages: list[int]) -> str:
    if not pages:
        return ""
    unique = sorted(set(pages))
    if len(unique) == 1:
        return str(unique[0])
    return f"{unique[0]}-{unique[-1]}"


def _ledger_label(ledger_roles: set[str]) -> str:
    if not ledger_roles:
        return ""
    if len(ledger_roles) == 1:
        return next(iter(ledger_roles))
    return " | ".join(sorted(ledger_roles))


def messages_char_count(messages: list[dict[str, str]]) -> int:
    """Total character count across all message contents."""
    return sum(len(m.get("content", "")) for m in messages)


def _prompt_fixed_parts(
    pair_id: str,
    reference_profile: dict | None,
    type_labels: dict | None,
    *,
    chunk: ContextChunk | None = None,
    chunk_index: int = 1,
    chunk_total: int = 1,
) -> tuple[str, str, str]:
    """Return (system_content, user_prefix_before_raw_context, user_suffix)."""
    schema_text = json.dumps(_schema_skeleton(), indent=2)
    labels_text = json.dumps(type_labels or {}, indent=2)
    reference_text = (
        json.dumps(_summarize_reference_profile(reference_profile), indent=2)
        if reference_profile
        else "(no reference profile provided)"
    )

    system = SYSTEM_INSTRUCTIONS
    if chunk_total > 1:
        system += CHUNK_SYSTEM_SUFFIX

    chunk_header = ""
    if chunk is not None and chunk_total > 1:
        chunk_header = (
            f"CHUNK: {chunk.chunk_id} ({chunk_index} of {chunk_total})\n"
            f"chunk_source_ledger: {chunk.source_ledger}\n"
            f"chunk_page_range: {chunk.page_range}\n"
            f"chunk_source_files: {', '.join(chunk.source_files)}\n\n"
        )

    user_prefix = (
        f"pair_id: {pair_id}\n\n"
        f"{chunk_header}"
        "TYPE LABEL DICTIONARY (canonical_type -> aliases):\n"
        f"{labels_text}\n\n"
        "REFERENCE WORKBOOK STRUCTURE (for layout context only, do not copy values):\n"
        f"{reference_text}\n\n"
        "TARGET JSON SCHEMA (return exactly this shape; arrays may have many items):\n"
        f"{schema_text}\n\n"
        "RAW EXTRACTION CONTEXT (verbatim from source PDFs):\n"
    )
    user_suffix = "\n\nReturn the standardized JSON object now. JSON only."
    return system, user_prefix, user_suffix


def _split_oversized_unit(unit: ContextUnit, max_text_chars: int) -> list[ContextUnit]:
    """Split a single unit when its text alone exceeds the context budget."""
    if len(unit.text) <= max_text_chars:
        return [unit]
    parts: list[ContextUnit] = []
    text = unit.text
    part_idx = 0
    while text:
        slice_len = max(1, max_text_chars - 80)
        chunk_text = text[:slice_len]
        text = text[slice_len:]
        part_idx += 1
        suffix = f" [part {part_idx}]" if text else ""
        parts.append(
            ContextUnit(
                ledger_role=unit.ledger_role,
                source_file_name=unit.source_file_name,
                page_number=unit.page_number,
                sheet_name=unit.sheet_name,
                row_order=unit.row_order,
                line_order=unit.line_order + part_idx - 1,
                text=chunk_text + suffix,
            )
        )
    return parts


def plan_context_chunks(
    context: dict[str, pd.DataFrame],
    pair_id: str,
    reference_profile: dict | None = None,
    type_labels: dict | None = None,
    max_input_chars: int | None = None,
) -> list[ContextChunk]:
    """Split raw context into chunks that fit within the per-request char cap."""
    max_input_chars = max_input_chars or settings.ai_max_input_chars_per_request
    units = _iter_context_units(context)
    if not units:
        chunk = ContextChunk(
            chunk_id="chunk_001",
            source_ledger="",
            page_range="",
            context_text="(empty)",
            unit_count=0,
        )
        return [chunk]

    system, user_prefix, user_suffix = _prompt_fixed_parts(
        pair_id,
        reference_profile,
        type_labels,
        chunk=ContextChunk(
            chunk_id="chunk_000",
            source_ledger="org_ledger | party_ledger",
            page_range="1-9999",
            source_files=["placeholder.pdf"],
            context_text="",
        ),
        chunk_index=1,
        chunk_total=99,
    )
    overhead = len(system) + len(user_prefix) + len(user_suffix)
    context_budget = max(500, max_input_chars - overhead)
    if overhead >= max_input_chars:
        raise ValueError(
            f"Fixed prompt overhead ({overhead} chars) exceeds "
            f"AI_MAX_INPUT_CHARS_PER_REQUEST={max_input_chars}. "
            "Increase the cap or reduce type-label/schema context."
        )

    expanded_units: list[ContextUnit] = []
    for unit in units:
        expanded_units.extend(_split_oversized_unit(unit, context_budget))

    chunks: list[ContextChunk] = []
    current: list[ContextUnit] = []
    current_len = 0

    def flush_chunk() -> None:
        nonlocal current, current_len
        if not current:
            return
        chunk_index = len(chunks) + 1
        pages = [u.page_number for u in current if u.page_number]
        source_files = sorted({u.source_file_name for u in current if u.source_file_name})
        ledger_roles = {u.ledger_role for u in current if u.ledger_role}
        context_text = _units_to_context_text(current)
        chunks.append(
            ContextChunk(
                chunk_id=f"chunk_{chunk_index:03d}",
                source_ledger=_ledger_label(ledger_roles),
                page_range=_page_range_label(pages),
                source_files=source_files,
                context_text=context_text,
                unit_count=len(current),
            )
        )
        current = []
        current_len = 0

    for unit in expanded_units:
        unit_len = len(unit.text) + 1
        if current and current_len + unit_len > context_budget:
            flush_chunk()
        current.append(unit)
        current_len += unit_len

    flush_chunk()

    renumbered: list[ContextChunk] = []
    for idx, chunk in enumerate(chunks, start=1):
        renumbered.append(
            ContextChunk(
                chunk_id=f"chunk_{idx:03d}",
                source_ledger=chunk.source_ledger,
                page_range=chunk.page_range,
                source_files=chunk.source_files,
                context_text=chunk.context_text,
                unit_count=chunk.unit_count,
            )
        )

    total = len(renumbered)
    for idx, chunk in enumerate(renumbered, start=1):
        messages = build_messages_for_chunk(
            pair_id=pair_id,
            chunk=chunk,
            chunk_index=idx,
            chunk_total=total,
            reference_profile=reference_profile,
            type_labels=type_labels,
            max_input_chars=max_input_chars,
        )
        if messages_char_count(messages) > max_input_chars:
            raise ValueError(
                f"Chunk {chunk.chunk_id} exceeds AI_MAX_INPUT_CHARS_PER_REQUEST "
                f"after planning. Reduce the cap or split manually."
            )
    return renumbered


def _iter_context_units_from_text(
    context_text: str, chunk: ContextChunk
) -> list[ContextUnit]:
    """Rebuild minimal units from rendered chunk text for emergency re-split."""
    lines = context_text.split("\n")
    units: list[ContextUnit] = []
    buf: list[str] = []
    meta = ContextUnit("", "", 0, "", 0, 0, "")
    for line in lines:
        if line.startswith("[") and "] ledger_role=" in line:
            if buf:
                units.append(
                    ContextUnit(
                        ledger_role=meta.ledger_role,
                        source_file_name=meta.source_file_name,
                        page_number=meta.page_number,
                        sheet_name=meta.sheet_name,
                        row_order=len(units),
                        line_order=len(units),
                        text="\n".join(buf),
                    )
                )
                buf = []
            meta = _parse_unit_header(line)
            buf.append(line)
        else:
            buf.append(line)
    if buf:
        units.append(
            ContextUnit(
                ledger_role=meta.ledger_role,
                source_file_name=meta.source_file_name,
                page_number=meta.page_number,
                sheet_name=meta.sheet_name,
                row_order=len(units),
                line_order=len(units),
                text="\n".join(buf),
            )
        )
    if not units:
        units.append(
            ContextUnit(
                ledger_role=chunk.source_ledger,
                source_file_name=chunk.source_files[0] if chunk.source_files else "",
                page_number=0,
                sheet_name="",
                row_order=0,
                line_order=0,
                text=context_text,
            )
        )
    return units


def _parse_unit_header(line: str) -> ContextUnit:
    """Parse a unit header line back into metadata (best effort)."""
    sheet_name = ""
    ledger_role = ""
    source_file = ""
    page_number = 0
    if line.startswith("[") and "]" in line:
        sheet_name = line[1 : line.index("]")]
    for token in line.split():
        if token.startswith("ledger_role="):
            ledger_role = token.split("=", 1)[1]
        elif token.startswith("source_file="):
            source_file = token.split("=", 1)[1]
        elif token.startswith("page="):
            try:
                page_number = int(token.split("=", 1)[1])
            except ValueError:
                page_number = 0
    return ContextUnit(
        ledger_role=ledger_role,
        source_file_name=source_file,
        page_number=page_number,
        sheet_name=sheet_name,
        row_order=0,
        line_order=0,
        text="",
    )


def build_messages_for_chunk(
    pair_id: str,
    chunk: ContextChunk,
    chunk_index: int,
    chunk_total: int,
    reference_profile: dict | None = None,
    type_labels: dict | None = None,
    max_input_chars: int | None = None,
) -> list[dict[str, str]]:
    """Build chat messages for one context chunk."""
    max_input_chars = max_input_chars or settings.ai_max_input_chars_per_request
    system, user_prefix, user_suffix = _prompt_fixed_parts(
        pair_id,
        reference_profile,
        type_labels,
        chunk=chunk,
        chunk_index=chunk_index,
        chunk_total=chunk_total,
    )
    user_content = user_prefix + chunk.context_text + user_suffix
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    total = messages_char_count(messages)
    if total > max_input_chars:
        raise ValueError(
            f"Prompt for {chunk.chunk_id} is {total} chars, exceeding "
            f"AI_MAX_INPUT_CHARS_PER_REQUEST={max_input_chars}."
        )
    return messages


def _sheet_to_text(df: pd.DataFrame, max_rows: int = 400) -> str:
    """Render a dataframe sheet as compact pipe-delimited text."""
    if df.empty:
        return "(empty)"
    truncated = df.head(max_rows)
    lines = [" | ".join(str(c) for c in truncated.columns)]
    for _, row in truncated.iterrows():
        lines.append(" | ".join("" if pd.isna(v) else str(v) for v in row.tolist()))
    if len(df) > max_rows:
        lines.append(f"... ({len(df) - max_rows} more rows omitted from sheet view)")
    return "\n".join(lines)


def load_raw_context(
    raw_workbook_path: Path,
    sheets: tuple[str, ...] = CONTEXT_SHEETS,
) -> dict[str, pd.DataFrame]:
    """Load the relevant context sheets from the raw extraction workbook."""
    if not raw_workbook_path.exists():
        raise FileNotFoundError(
            f"Raw extraction workbook not found: {raw_workbook_path}"
        )
    available = pd.read_excel(raw_workbook_path, sheet_name=None)
    return {name: available[name] for name in sheets if name in available}


def build_raw_context_text(context: dict[str, pd.DataFrame]) -> str:
    """Concatenate all context sheets without truncation (use chunking for limits)."""
    parts: list[str] = []
    for name in CONTEXT_SHEETS:
        df = context.get(name)
        if df is None:
            continue
        parts.append(f"=== SHEET: {name} ===")
        parts.append(_sheet_to_text(df))
    return "\n".join(parts) if parts else "(empty)"


def build_messages(
    pair_id: str,
    raw_workbook_path: Path,
    reference_profile: dict | None = None,
    type_labels: dict | None = None,
    max_input_chars: int | None = None,
) -> list[dict[str, str]]:
    """Build chat messages for the first (or only) context chunk."""
    max_input_chars = max_input_chars or settings.ai_max_input_chars_per_request
    context = load_raw_context(raw_workbook_path)
    chunks = plan_context_chunks(
        context=context,
        pair_id=pair_id,
        reference_profile=reference_profile,
        type_labels=type_labels,
        max_input_chars=max_input_chars,
    )
    return build_messages_for_chunk(
        pair_id=pair_id,
        chunk=chunks[0],
        chunk_index=1,
        chunk_total=len(chunks),
        reference_profile=reference_profile,
        type_labels=type_labels,
        max_input_chars=max_input_chars,
    )


def _summarize_reference_profile(profile: dict | None) -> dict:
    """Reduce a full reference profile to a compact structural summary."""
    if not profile:
        return {}
    sheets = profile.get("sheets", [])
    summary = []
    for sheet in sheets:
        summary.append(
            {
                "sheet_name": sheet.get("sheet_name"),
                "headers": sheet.get("headers"),
                "is_ledger_like": sheet.get("is_ledger_like"),
                "is_summary_like": sheet.get("is_summary_like"),
            }
        )
    return {
        "workbook_name": profile.get("workbook_name"),
        "sheet_count": profile.get("sheet_count"),
        "sheets": summary,
    }
