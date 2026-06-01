"""Deterministic, layout-aware PDF ledger extraction (no AI, no OCR).

Strategy (token-efficient; the LLM is never the bulk extractor):
1. Use PyMuPDF word boxes and cluster them into visual rows by y-coordinate.
2. Detect the header row generically and derive column anchors from it.
3. Assign amount tokens to Debit/Credit/Balance by nearest amount-column centre;
   assign text tokens (date/type/particulars/ref/voucher) by left-edge against
   gap-midpoint boundaries. This separation reliably handles wide free-text
   particulars that abut the reference column.
4. Detect the ledger identity (the account whose ledger this is) from the header
   block.

Every emitted row keeps full source evidence (file, page, source row number,
raw text, extraction method). Non-financial title/footer lines are skipped and
counted as metadata in the audit, never as dropped financial rows. Any row that
carries an amount is always emitted (routed to review if its structure is
unclear) so no financial row can vanish silently.

pdfplumber table extraction is attempted as a layout-confidence signal only; the
ledgers in scope export as positioned text, so the word-layout path is primary.
"""

from __future__ import annotations

import logging
import re
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
import pdfplumber

from src.config import settings
from src.formalization.ai_layout_profiler import profile_layout_result
from src.formalization.ledger_cleaning import (
    clean_text,
    detect_record_kind,
    looks_like_amount,
    looks_like_date,
    parse_amount_with_confidence,
    parse_balance_with_confidence,
    parse_date_with_confidence,
)
from src.formalization.ledger_models import (
    ROLE_ORG,
    ROLE_PARTY,
    SPECIAL_RECORD_KINDS,
    ExtractionAuditRow,
    LedgerIdentity,
    PageAnalysis,
    RecordKind,
)

logger = logging.getLogger("formalization.pdf_ledger_extractor")

EXTRACTION_METHOD = "pymupdf_words_layout"
STRATEGY_A = "pdfplumber_table"
STRATEGY_B = "pymupdf_coordinate"
STRATEGY_C = "line_reconstruction"
STRATEGY_D = "ai_layout_profiler"
STRATEGY_E = "ocr_extension_point"

Y_TOLERANCE = 3.5
# Tokens belonging to one multi-word header label (e.g. "Vch Type") sit within
# a few px of each other; distinct columns are separated by a larger gap (the
# smallest observed inter-column gap is ~14px). Keep this strictly between the
# two so multi-word labels merge but adjacent columns never do.
HEADER_MERGE_GAP = 10.0

# Canonical column keys recovered from header labels.
COL_DATE = "date"
COL_TYPE = "raw_type"
COL_PARTICULARS = "particulars"
COL_ACCOUNT = "account"
COL_VOUCHER = "voucher_no"
COL_REFERENCE = "reference_no"
COL_DEBIT = "debit"
COL_CREDIT = "credit"
COL_BALANCE = "balance"

AMOUNT_COLS = frozenset({COL_DEBIT, COL_CREDIT, COL_BALANCE})

LEDGER_ROLE_DIRS = (ROLE_ORG, ROLE_PARTY)


# ---------------------------------------------------------------------------
# Data carriers
# ---------------------------------------------------------------------------
@dataclass
class _Column:
    key: str
    x0: float
    x1: float
    confidence: float = 0.0
    detected_label: str = ""

    @property
    def center(self) -> float:
        return (self.x0 + self.x1) / 2.0


@dataclass
class ExtractedRow:
    """A geometrically parsed row before type/perspective enrichment."""

    page_number: int
    source_row_number: int
    raw_text: str
    record_kind: str
    record_kind_reason: str
    extraction_strategy_used: str = STRATEGY_B
    row_confidence: float = 0.0
    layout_confidence: float = 0.0
    date_raw: str = ""
    date_iso: str = ""
    date_confidence: float = 0.0
    date_carried: bool = False
    raw_type: str = ""
    particulars: str = ""
    account: str = ""
    voucher_no: str = ""
    reference_no: str = ""
    debit_raw: str = ""
    credit_raw: str = ""
    balance_raw: str = ""
    debit_source: Optional[float] = None
    credit_source: Optional[float] = None
    balance_source: Optional[float] = None
    balance_side_source: str = ""
    amount_confidence: float = 0.0
    column_confidence: dict[str, float] = field(default_factory=dict)


@dataclass
class LedgerExtraction:
    """Full result of extracting one ledger PDF."""

    ledger_role: str
    source_file: str
    identity: LedgerIdentity
    rows: list[ExtractedRow]
    audit: list[ExtractionAuditRow]
    page_analysis: list[PageAnalysis]
    page_count: int
    layout_stats: dict = field(default_factory=dict)


@dataclass
class _PageStrategyResult:
    strategy_name: str
    rows: list[ExtractedRow] = field(default_factory=list)
    columns: list[_Column] = field(default_factory=list)
    detected_headers: list[str] = field(default_factory=list)
    confidence_score: float = 0.0
    detail: str = ""


# ---------------------------------------------------------------------------
# PDF discovery
# ---------------------------------------------------------------------------
def discover_pair_pdfs(pair_root: Path) -> dict[str, list[Path]]:
    """Discover org-ledger and party-ledger PDFs under ``pair_root/input/<role>``.

    Fails clearly if a role folder is missing or empty. Does not mutate sources.
    """
    if not pair_root.exists():
        raise FileNotFoundError(f"Pair workspace not found: {pair_root}")

    discovered: dict[str, list[Path]] = {}
    for role in LEDGER_ROLE_DIRS:
        role_dir = pair_root / "input" / role
        if not role_dir.exists():
            raise FileNotFoundError(f"Missing input folder for role '{role}': {role_dir}")
        pdfs = sorted(p for p in role_dir.glob("*.pdf") if p.is_file())
        if not pdfs:
            raise FileNotFoundError(f"No PDF found for role '{role}' in: {role_dir}")
        discovered[role] = pdfs
    return discovered


def _fingerprint_store_path() -> Path:
    root = settings.resolved(settings.formalization_layout_fingerprint_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / "layout_fingerprints.json"


def _load_layout_fingerprints() -> dict[str, dict]:
    path = _fingerprint_store_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring unreadable layout fingerprint cache: %s", path)
        return {}


def _save_layout_fingerprints(fingerprints: dict[str, dict]) -> None:
    path = _fingerprint_store_path()
    path.write_text(
        json.dumps(fingerprints, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _fingerprint_key(headers: list[str], columns: list[_Column]) -> str:
    normalized_headers = "|".join(clean_text(h).lower() for h in headers if h)
    col_sig = "|".join(c.key for c in sorted(columns, key=lambda x: x.x0))
    return f"{normalized_headers}::{col_sig}"


# ---------------------------------------------------------------------------
# Word clustering and column detection
# ---------------------------------------------------------------------------
def _cluster_rows(words: list[tuple], y_tol: float = Y_TOLERANCE) -> list[list[tuple]]:
    """Cluster word boxes into visual rows by their top y-coordinate.

    Each returned row is a list of ``(x0, x1, text)`` sorted left-to-right.
    """
    rows: list[dict] = []
    for w in sorted(words, key=lambda x: (round(x[1], 1), x[0])):
        x0, y0, x1, _y1, txt = w[0], w[1], w[2], w[3], w[4]
        if txt is None or str(txt).strip() == "":
            continue
        placed = False
        for row in rows:
            if abs(row["y"] - y0) <= y_tol:
                row["cells"].append((x0, x1, str(txt)))
                placed = True
                break
        if not placed:
            rows.append({"y": y0, "cells": [(x0, x1, str(txt))]})
    out: list[list[tuple]] = []
    for row in sorted(rows, key=lambda r: r["y"]):
        out.append(sorted(row["cells"], key=lambda c: c[0]))
    return out


def _row_text(cells: list[tuple]) -> str:
    return clean_text(" ".join(c[2] for c in cells))


def _classify_header_label(text: str) -> Optional[str]:
    """Map a (merged) header label to a canonical column key, or None."""
    lowered = text.lower()
    if "date" in lowered:
        return COL_DATE
    if "particular" in lowered:
        return COL_PARTICULARS
    if "ref" in lowered:
        return COL_REFERENCE
    if "type" in lowered:
        return COL_TYPE
    if "account" in lowered:
        return COL_ACCOUNT
    if "balance" in lowered:
        return COL_BALANCE
    if "debit" in lowered:
        return COL_DEBIT
    if "credit" in lowered:
        return COL_CREDIT
    if any(tok in lowered for tok in ("vch", "voucher", "bill", "no")):
        return COL_VOUCHER
    return None


def _column_label_confidence(text: str, key: str) -> float:
    lowered = text.lower()
    if key == COL_DATE and "date" in lowered:
        return 98.0
    if key == COL_PARTICULARS and "particular" in lowered:
        return 98.0
    if key == COL_REFERENCE and "ref" in lowered:
        return 96.0
    if key == COL_TYPE and "type" in lowered:
        return 95.0
    if key == COL_ACCOUNT and "account" in lowered:
        return 95.0
    if key == COL_BALANCE and "balance" in lowered:
        return 98.0
    if key == COL_DEBIT and "debit" in lowered:
        return 98.0
    if key == COL_CREDIT and "credit" in lowered:
        return 98.0
    if key == COL_VOUCHER and any(tok in lowered for tok in ("vch", "voucher", "bill", "no")):
        return 90.0
    return 70.0


def _is_header_row(cells: list[tuple]) -> bool:
    lowered = _row_text(cells).lower()
    has_debit_credit = "debit" in lowered and "credit" in lowered
    has_date_amount = "date" in lowered and ("amount" in lowered or "balance" in lowered)
    return has_debit_credit or has_date_amount


def _detect_columns(cells: list[tuple]) -> list[_Column]:
    """Merge adjacent header tokens into columns and name them."""
    merged: list[list[tuple]] = []
    for x0, x1, txt in cells:
        if merged and x0 - merged[-1][-1][1] <= HEADER_MERGE_GAP:
            merged[-1].append((x0, x1, txt))
        else:
            merged.append([(x0, x1, txt)])

    columns: list[_Column] = []
    for group in merged:
        text = clean_text(" ".join(c[2] for c in group))
        key = _classify_header_label(text)
        if key is None:
            continue
        x0 = min(c[0] for c in group)
        x1 = max(c[1] for c in group)
        # Avoid duplicate keys (keep the first occurrence; later ones ignored).
        if any(col.key == key for col in columns):
            continue
        columns.append(
            _Column(
                key=key,
                x0=x0,
                x1=x1,
                confidence=_column_label_confidence(text, key),
                detected_label=text,
            )
        )
    columns.sort(key=lambda c: c.x0)
    return columns


def _detect_columns_from_labels(labels: list[str]) -> list[_Column]:
    """Detect columns from header labels when coordinates are unavailable."""
    columns: list[_Column] = []
    for idx, label in enumerate(labels):
        text = clean_text(label)
        key = _classify_header_label(text)
        if key is None:
            continue
        if any(c.key == key for c in columns):
            continue
        x0 = float(idx * 100)
        x1 = float((idx + 1) * 100)
        columns.append(
            _Column(
                key=key,
                x0=x0,
                x1=x1,
                confidence=_column_label_confidence(text, key),
                detected_label=text,
            )
        )
    columns.sort(key=lambda c: c.x0)
    return columns


def _amount_region_threshold(columns: list[_Column]) -> float:
    """X-centre below which a token is treated as text, not an amount."""
    amount_centers = [c.center for c in columns if c.key in AMOUNT_COLS]
    text_centers = [c.center for c in columns if c.key not in AMOUNT_COLS]
    if not amount_centers:
        return float("inf")
    first_amount = min(amount_centers)
    left_text = [c for c in text_centers if c < first_amount]
    if not left_text:
        return first_amount - 30.0
    return (first_amount + max(left_text)) / 2.0


def _text_boundaries(text_cols: list[_Column]) -> list[float]:
    """Gap-midpoint boundaries between consecutive text columns."""
    boundaries: list[float] = []
    for i in range(len(text_cols) - 1):
        boundaries.append((text_cols[i].x1 + text_cols[i + 1].x0) / 2.0)
    return boundaries


def _assign_text_column(x0: float, text_cols: list[_Column], boundaries: list[float]) -> str:
    idx = 0
    for b in boundaries:
        if x0 >= b:
            idx += 1
    idx = min(idx, len(text_cols) - 1)
    return text_cols[idx].key


def _assign_amount_column(center: float, amount_cols: list[_Column]) -> str:
    best = amount_cols[0]
    best_dist = abs(center - best.center)
    for col in amount_cols[1:]:
        dist = abs(center - col.center)
        if dist < best_dist:
            best, best_dist = col, dist
    return best.key


# ---------------------------------------------------------------------------
# Identity detection
# ---------------------------------------------------------------------------
_ACCOUNT_LABEL_RE = re.compile(r"(?i)^account\s*[:\-]\s*(.+)$")
_PERIOD_RE = re.compile(r"(?i)(from\s+.+\s+to\s+.+|\d.+\s+to\s+\d.+)")


def _detect_identity(
    clustered: list[list[tuple]], ledger_role: str, source_file: str
) -> LedgerIdentity:
    """Detect the ledger account identity from the top-of-document block."""
    texts = [_row_text(r) for r in clustered[:12]]
    owner_company = texts[0] if texts else ""
    period_text = ""
    for t in texts:
        if _PERIOD_RE.search(t):
            period_text = t
            break

    # Pattern A: "Account : GOOD LUCK" (Tally Account Ledger / org format).
    for t in texts:
        m = _ACCOUNT_LABEL_RE.match(t)
        if m:
            title = clean_text(m.group(1))
            # Trim a trailing period clause if it got merged onto the same line.
            title = re.split(r"(?i)\s+from\s+", title)[0].strip()
            return LedgerIdentity(
                ledger_role=ledger_role,
                source_file=source_file,
                detected_title=title,
                owner_company=owner_company,
                period_text=period_text,
                detection_method="account_label",
                confidence=0.95,
            )

    # Pattern B: the account name sits on the line above "Ledger Account"
    # (Tally Ledger Account / party format).
    for idx, t in enumerate(texts):
        if t.strip().lower() == "ledger account":
            for back in range(idx - 1, -1, -1):
                candidate = texts[back]
                if candidate:
                    return LedgerIdentity(
                        ledger_role=ledger_role,
                        source_file=source_file,
                        detected_title=candidate,
                        owner_company=owner_company,
                        period_text=period_text,
                        detection_method="above_ledger_account",
                        confidence=0.9,
                    )

    # Fallback: first non-empty title line; low confidence.
    fallback = next((t for t in texts[1:] if t), owner_company)
    return LedgerIdentity(
        ledger_role=ledger_role,
        source_file=source_file,
        detected_title=fallback,
        owner_company=owner_company,
        period_text=period_text,
        detection_method="fallback_first_line",
        confidence=0.4,
    )


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------
def _build_row(
    cells: list[tuple],
    columns: list[_Column],
    page_number: int,
    source_row_number: int,
    *,
    strategy_name: str,
    layout_confidence: float,
) -> ExtractedRow:
    threshold = _amount_region_threshold(columns)
    text_cols = [c for c in columns if c.key not in AMOUNT_COLS]
    amount_cols = [c for c in columns if c.key in AMOUNT_COLS]
    boundaries = _text_boundaries(text_cols)

    text_buckets: dict[str, list[tuple]] = {c.key: [] for c in text_cols}
    amount_buckets: dict[str, list[tuple]] = {c.key: [] for c in amount_cols}

    for x0, x1, txt in cells:
        center = (x0 + x1) / 2.0
        is_amount = amount_cols and center >= threshold and looks_like_amount(txt)
        # Cr/Dr side markers belong with their amount column.
        is_side = amount_cols and center >= threshold and txt.strip().lower() in {"cr", "dr"}
        if is_amount or is_side:
            key = _assign_amount_column(center, amount_cols)
            amount_buckets[key].append((x0, txt))
        elif text_cols:
            key = _assign_text_column(x0, text_cols, boundaries)
            text_buckets[key].append((x0, txt))

    def joined(bucket: dict[str, list[tuple]], key: str) -> str:
        items = bucket.get(key) or []
        return clean_text(" ".join(t for _x, t in sorted(items, key=lambda i: i[0])))

    date_raw = joined(text_buckets, COL_DATE)
    raw_type = joined(text_buckets, COL_TYPE)
    particulars = joined(text_buckets, COL_PARTICULARS)
    account = joined(text_buckets, COL_ACCOUNT)
    voucher_col = joined(text_buckets, COL_VOUCHER)
    reference_col = joined(text_buckets, COL_REFERENCE)

    debit_raw = joined(amount_buckets, COL_DEBIT)
    credit_raw = joined(amount_buckets, COL_CREDIT)
    balance_raw = joined(amount_buckets, COL_BALANCE)

    debit_source, debit_conf = parse_amount_with_confidence(debit_raw)
    credit_source, credit_conf = parse_amount_with_confidence(credit_raw)
    balance_amount, balance_side, balance_conf = parse_balance_with_confidence(balance_raw)

    # Reference/voucher mapping: org ledgers carry a single "Vch/Bill No" that
    # doubles as the reference; party ledgers separate Ref.No and Vch No.
    reference_no = reference_col or voucher_col
    voucher_no = voucher_col or reference_col

    raw_text = _row_text(cells)
    has_amount = any(v is not None for v in (debit_source, credit_source, balance_amount))
    has_date = looks_like_date(date_raw)
    has_type = bool(raw_type)
    record_kind, reason = detect_record_kind(
        raw_text, has_amount=has_amount, has_date=has_date, has_type=has_type
    )

    date_iso, date_conf = parse_date_with_confidence(date_raw)
    amount_conf = max(debit_conf, credit_conf, balance_conf)
    row_conf = max(
        0.0,
        min(
            100.0,
            (layout_confidence * 0.45)
            + (date_conf * 0.2)
            + (amount_conf * 0.25)
            + (10.0 if record_kind != RecordKind.UNKNOWN else 0.0),
        ),
    )

    return ExtractedRow(
        page_number=page_number,
        source_row_number=source_row_number,
        raw_text=raw_text,
        record_kind=record_kind,
        record_kind_reason=reason,
        extraction_strategy_used=strategy_name,
        row_confidence=round(row_conf, 1),
        layout_confidence=round(layout_confidence, 1),
        date_raw=date_raw,
        date_iso=date_iso or "",
        date_confidence=round(date_conf, 1),
        raw_type=raw_type,
        particulars=particulars,
        account=account,
        voucher_no=voucher_no,
        reference_no=reference_no,
        debit_raw=debit_raw,
        credit_raw=credit_raw,
        balance_raw=balance_raw,
        debit_source=debit_source,
        credit_source=credit_source,
        balance_source=balance_amount,
        balance_side_source=balance_side,
        amount_confidence=round(amount_conf, 1),
        column_confidence={c.key: round(c.confidence, 1) for c in columns},
    )


def _should_emit(row: ExtractedRow) -> bool:
    """Emit transactions, recognised special rows, and any amount-bearing row.

    Non-financial metadata (title/footer lines with no amount and no marker) is
    not emitted; such rows are counted as metadata in the audit, not dropped
    financial rows.
    """
    if row.record_kind in (frozenset({RecordKind.TRANSACTION}) | SPECIAL_RECORD_KINDS):
        return True
    has_amount = any(
        v is not None
        for v in (row.debit_source, row.credit_source, row.balance_source)
    )
    return has_amount


def _score_strategy_result(rows: list[ExtractedRow], columns: list[_Column]) -> float:
    if not rows:
        return 0.0
    unknown = sum(1 for r in rows if r.record_kind == RecordKind.UNKNOWN)
    tx = sum(1 for r in rows if r.record_kind == RecordKind.TRANSACTION)
    dated_tx = sum(1 for r in rows if r.record_kind == RecordKind.TRANSACTION and r.date_iso)
    amount_tx = sum(
        1
        for r in rows
        if r.record_kind == RecordKind.TRANSACTION
        and (r.debit_source is not None or r.credit_source is not None)
    )
    base = 30.0
    col_score = min(30.0, len(columns) * 4.0)
    tx_score = min(20.0, tx * 1.5)
    unknown_penalty = min(25.0, unknown * 2.5)
    date_score = 0.0 if tx == 0 else (10.0 * dated_tx / tx)
    amount_score = 0.0 if tx == 0 else (10.0 * amount_tx / tx)
    conf = base + col_score + tx_score + date_score + amount_score - unknown_penalty
    return round(max(0.0, min(100.0, conf)), 1)


def _build_page_analysis(
    page_number: int,
    strategy_name: str,
    confidence_score: float,
    headers: list[str],
    columns: list[_Column],
    rows: list[ExtractedRow],
) -> PageAnalysis:
    tx = sum(1 for r in rows if r.record_kind == RecordKind.TRANSACTION)
    special = sum(1 for r in rows if r.record_kind in SPECIAL_RECORD_KINDS)
    unknown = sum(1 for r in rows if r.record_kind == RecordKind.UNKNOWN)
    review = sum(
        1
        for r in rows
        if (
            r.record_kind == RecordKind.UNKNOWN
            or (r.record_kind == RecordKind.TRANSACTION and r.date_confidence < 70.0)
            or (r.record_kind == RecordKind.TRANSACTION and r.amount_confidence < 70.0)
        )
    )
    return PageAnalysis(
        page_number=page_number,
        extraction_strategy_used=strategy_name,
        confidence_score=confidence_score,
        detected_headers=headers,
        detected_columns=[c.key for c in columns],
        column_confidence={c.key: round(c.confidence, 1) for c in columns},
        transaction_row_count=tx,
        special_row_count=special,
        unknown_row_count=unknown,
        review_row_count=review,
    )


def _extract_with_strategy_b(
    clustered: list[list[tuple]],
    page_number: int,
    last_columns: Optional[list[_Column]],
    source_row_number: int,
) -> _PageStrategyResult:
    header_idx: Optional[int] = None
    columns = last_columns or []
    headers: list[str] = []
    for ridx, cells in enumerate(clustered):
        if _is_header_row(cells):
            detected = _detect_columns(cells)
            if detected:
                columns = detected
                header_idx = ridx
                headers.append(_row_text(cells))
            break
    if not columns:
        return _PageStrategyResult(
            strategy_name=STRATEGY_B,
            rows=[],
            columns=[],
            detected_headers=headers,
            confidence_score=0.0,
            detail="no columns detected",
        )

    tentative_rows: list[ExtractedRow] = []
    row_no = source_row_number
    layout_conf = min(
        100.0, round(sum(c.confidence for c in columns) / max(1, len(columns)), 1)
    )
    last_date_iso = ""
    for ridx, cells in enumerate(clustered):
        if header_idx is not None and ridx == header_idx:
            continue
        pre_header = header_idx is not None and ridx < header_idx
        row = _build_row(
            cells,
            columns,
            page_number,
            row_no + 1,
            strategy_name=STRATEGY_B,
            layout_confidence=layout_conf,
        )
        if pre_header and row.record_kind not in SPECIAL_RECORD_KINDS:
            continue
        if not _should_emit(row):
            continue
        if row.record_kind == RecordKind.TRANSACTION:
            if row.date_iso:
                last_date_iso = row.date_iso
            elif not row.date_raw and last_date_iso:
                row.date_iso = last_date_iso
                row.date_carried = True
                row.date_confidence = min(row.date_confidence, 80.0)
        elif row.date_iso:
            last_date_iso = row.date_iso
        row_no += 1
        row.source_row_number = row_no
        tentative_rows.append(row)

    return _PageStrategyResult(
        strategy_name=STRATEGY_B,
        rows=tentative_rows,
        columns=columns,
        detected_headers=headers,
        confidence_score=_score_strategy_result(tentative_rows, columns),
    )


def _extract_with_strategy_a(
    page_tables: list[list[list[str]]],
    page_number: int,
    source_row_number: int,
) -> _PageStrategyResult:
    if not page_tables:
        return _PageStrategyResult(strategy_name=STRATEGY_A, detail="no tables")
    best_rows: list[ExtractedRow] = []
    best_columns: list[_Column] = []
    headers: list[str] = []
    for table in page_tables:
        if not table:
            continue
        header_idx: Optional[int] = None
        columns: list[_Column] = []
        for ridx, raw_row in enumerate(table):
            labels = [clean_text(c) for c in (raw_row or []) if clean_text(c)]
            if not labels:
                continue
            joined = " ".join(labels)
            if _is_header_row([(0.0, 0.0, joined)]):
                header_idx = ridx
                columns = _detect_columns_from_labels(labels)
                headers.append(joined)
                break
        if not columns:
            continue
        table_rows: list[ExtractedRow] = []
        row_no = source_row_number
        layout_conf = min(
            95.0, round(sum(c.confidence for c in columns) / max(1, len(columns)), 1)
        )
        for ridx, raw_row in enumerate(table):
            if header_idx is not None and ridx <= header_idx:
                continue
            cells = []
            for cidx, cell in enumerate(raw_row or []):
                text = clean_text(cell)
                if not text:
                    continue
                x0 = float(cidx * 100)
                x1 = float((cidx + 1) * 100)
                cells.append((x0, x1, text))
            if not cells:
                continue
            row = _build_row(
                cells,
                columns,
                page_number,
                row_no + 1,
                strategy_name=STRATEGY_A,
                layout_confidence=layout_conf,
            )
            if not _should_emit(row):
                continue
            row_no += 1
            row.source_row_number = row_no
            table_rows.append(row)
        if len(table_rows) > len(best_rows):
            best_rows = table_rows
            best_columns = columns
    return _PageStrategyResult(
        strategy_name=STRATEGY_A,
        rows=best_rows,
        columns=best_columns,
        detected_headers=headers,
        confidence_score=_score_strategy_result(best_rows, best_columns),
    )


def _extract_with_strategy_c(
    page_text: str,
    page_number: int,
    source_row_number: int,
) -> _PageStrategyResult:
    lines = [clean_text(l) for l in (page_text or "").splitlines()]
    lines = [l for l in lines if l]
    if not lines:
        return _PageStrategyResult(strategy_name=STRATEGY_C, detail="empty page text")
    header_line = next((l for l in lines if "debit" in l.lower() and "credit" in l.lower()), "")
    labels = re.split(r"\s{2,}", header_line) if header_line else []
    columns = _detect_columns_from_labels(labels) if labels else []
    if not columns:
        columns = _detect_columns_from_labels(
            ["Date", "Type", "Particulars", "Voucher", "Debit", "Credit", "Balance"]
        )
        for col in columns:
            col.confidence = min(col.confidence, 60.0)
    rows: list[ExtractedRow] = []
    row_no = source_row_number
    layout_conf = min(75.0, round(sum(c.confidence for c in columns) / max(1, len(columns)), 1))
    amount_re = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?(?:\s*(?:cr|dr))?$", re.IGNORECASE)
    for line in lines:
        if line == header_line or "page " in line.lower():
            continue
        parts = re.split(r"\s{2,}", line)
        if len(parts) == 1:
            parts = line.split()
        if len(parts) < 2:
            continue
        cells: list[tuple] = []
        cursor = 0.0
        for p in parts:
            token = clean_text(p)
            if not token:
                continue
            width = max(20.0, float(len(token) * 6))
            cells.append((cursor, cursor + width, token))
            cursor += width + 10.0
        if not any(amount_re.search(c[2]) for c in cells):
            continue
        row = _build_row(
            cells,
            columns,
            page_number,
            row_no + 1,
            strategy_name=STRATEGY_C,
            layout_confidence=layout_conf,
        )
        if not _should_emit(row):
            continue
        row_no += 1
        row.source_row_number = row_no
        rows.append(row)
    return _PageStrategyResult(
        strategy_name=STRATEGY_C,
        rows=rows,
        columns=columns,
        detected_headers=[header_line] if header_line else [],
        confidence_score=_score_strategy_result(rows, columns),
    )


def extract_ledger(pdf_path: Path, ledger_role: str) -> LedgerExtraction:
    """Extract one ledger PDF into geometrically parsed rows + audit."""
    source_file = pdf_path.name
    rows: list[ExtractedRow] = []
    audit: list[ExtractionAuditRow] = []
    page_analysis: list[PageAnalysis] = []
    identity: Optional[LedgerIdentity] = None
    last_columns: Optional[list[_Column]] = None
    source_row_number = 0
    sample_lines: list[str] = []
    header_texts: list[str] = []
    strategy_counts: dict[str, int] = {
        STRATEGY_A: 0,
        STRATEGY_B: 0,
        STRATEGY_C: 0,
        STRATEGY_D: 0,
        STRATEGY_E: 0,
    }
    ai_usage_count = 0
    ai_cache_hit_count = 0
    ai_cache_miss_count = 0
    fingerprints = _load_layout_fingerprints()

    with fitz.open(pdf_path) as doc, pdfplumber.open(pdf_path) as plumber_doc:
        page_count = doc.page_count
        for page_index in range(page_count):
            page_number = page_index + 1
            try:
                page = doc.load_page(page_index)
                words = page.get_text("words") or []
                page_text = page.get_text("text") or ""
                page_tables = (
                    plumber_doc.pages[page_index].extract_tables()
                    if page_index < len(plumber_doc.pages)
                    else []
                ) or []
            except Exception as exc:  # noqa: BLE001 - record and continue
                logger.warning("Failed to read %s page %s: %s", source_file, page_number, exc)
                audit.append(
                    ExtractionAuditRow(
                        ledger_role=ledger_role,
                        source_file=source_file,
                        page_number=page_number,
                        extraction_method=EXTRACTION_METHOD,
                        status="error",
                        rows_emitted=0,
                        rows_dropped=0,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            clustered = _cluster_rows(words)
            if identity is None and clustered:
                identity = _detect_identity(clustered, ledger_role, source_file)

            strategy_a = _extract_with_strategy_a(page_tables, page_number, source_row_number)
            strategy_b = _extract_with_strategy_b(
                clustered, page_number, last_columns, source_row_number
            )
            strategy_c = _extract_with_strategy_c(page_text, page_number, source_row_number)
            strategy_e = _PageStrategyResult(
                strategy_name=STRATEGY_E,
                rows=[],
                columns=[],
                confidence_score=0.0,
                detail="ocr extension not implemented",
            )

            candidates = [strategy_a, strategy_b, strategy_c, strategy_e]
            best = max(candidates, key=lambda s: s.confidence_score)

            if best.confidence_score < settings.formalization_min_page_confidence_for_ai:
                ai_evidence = {
                    "ledger_role": ledger_role,
                    "page_count": 1,
                    "detected_columns": [c.key for c in best.columns],
                    "header_texts": best.detected_headers,
                    "sample_lines": [_row_text(r) for r in clustered[:30]],
                }
                ai_result = profile_layout_result(ai_evidence)
                if ai_result.used_ai:
                    ai_usage_count += 1
                if ai_result.cache_hit:
                    ai_cache_hit_count += 1
                if ai_result.cache_miss:
                    ai_cache_miss_count += 1
                if ai_result.profile is not None:
                    strategy_counts[STRATEGY_D] += 1
                    # AI only contributes layout structure; it never parses rows.
                    prof_cols = _detect_columns_from_labels(list(ai_result.profile.columns.keys()))
                    if prof_cols:
                        for col in prof_cols:
                            col.confidence = max(col.confidence, 75.0)
                        ai_layout = _PageStrategyResult(
                            strategy_name=STRATEGY_D,
                            rows=best.rows,
                            columns=prof_cols,
                            detected_headers=best.detected_headers,
                            confidence_score=min(100.0, best.confidence_score + 8.0),
                            detail=f"ai_layout_source={ai_result.profile.source}",
                        )
                        if ai_layout.confidence_score > best.confidence_score:
                            best = ai_layout

            if not best.columns:
                # Reuse persisted fingerprints as a fallback.
                fp = next(iter(fingerprints.values()), None)
                if fp:
                    best.columns = _detect_columns_from_labels(
                        fp.get("detected_columns", [])
                    )
                    for col in best.columns:
                        col.confidence = max(col.confidence, 72.0)
                    best.confidence_score = max(best.confidence_score, 60.0)
                    best.detail = "columns from persisted fingerprint"

            if best.columns:
                last_columns = best.columns
                if best.detected_headers:
                    header_texts.extend(best.detected_headers[:2])
                key = _fingerprint_key(best.detected_headers, best.columns)
                if key:
                    current = fingerprints.get(key, {})
                    fingerprints[key] = {
                        "detected_headers": best.detected_headers,
                        "detected_columns": [c.key for c in best.columns],
                        "usage_count": int(current.get("usage_count", 0)) + 1,
                        "confidence_score": best.confidence_score,
                    }

            emitted = len(best.rows)
            for row in best.rows:
                source_row_number += 1
                row.source_row_number = source_row_number
                rows.append(row)
                if len(sample_lines) < 60:
                    sample_lines.append(row.raw_text)

            strategy_counts[best.strategy_name] += 1
            pa = _build_page_analysis(
                page_number,
                best.strategy_name,
                best.confidence_score,
                best.detected_headers,
                best.columns,
                best.rows,
            )
            page_analysis.append(pa)

            audit.append(
                ExtractionAuditRow(
                    ledger_role=ledger_role,
                    source_file=source_file,
                    page_number=page_number,
                    extraction_method=best.strategy_name,
                    status="ok" if emitted > 0 else "review",
                    rows_emitted=emitted,
                    rows_dropped=0,
                    detail=(
                        f"confidence={best.confidence_score}; "
                        f"columns={[c.key for c in best.columns]}; "
                        f"unknown_rows={pa.unknown_row_count}; "
                        f"review_rows={pa.review_row_count}; "
                        f"detail={best.detail}"
                    ),
                )
            )

    _save_layout_fingerprints(fingerprints)

    if identity is None:
        identity = LedgerIdentity(
            ledger_role=ledger_role,
            source_file=source_file,
            detected_title=pdf_path.stem,
            detection_method="none",
            confidence=0.0,
        )

    layout_stats = {
        "ledger_role": ledger_role,
        "source_file": source_file,
        "page_count": page_count,
        "detected_columns": [c.key for c in last_columns] if last_columns else [],
        "header_texts": header_texts,
        "sample_lines": sample_lines,
        "row_count": len(rows),
        "page_analysis": [p.as_dict() for p in page_analysis],
        "strategy_counts": strategy_counts,
        "ai_usage_count": ai_usage_count,
        "ai_cache_hit_count": ai_cache_hit_count,
        "ai_cache_miss_count": ai_cache_miss_count,
    }

    return LedgerExtraction(
        ledger_role=ledger_role,
        source_file=source_file,
        identity=identity,
        rows=rows,
        audit=audit,
        page_analysis=page_analysis,
        page_count=page_count,
        layout_stats=layout_stats,
    )
