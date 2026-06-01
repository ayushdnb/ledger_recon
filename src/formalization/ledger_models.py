"""Typed data models and canonical schema for the formalization stage.

These models describe a single, stable canonical ledger row while preserving the
original source evidence (file, page, raw text, extraction method). Nothing here
performs reconciliation or matching; this is structure only.

Design rules:
- Source debit/credit/balance values are preserved verbatim in ``*_source`` fields.
- Organisation-perspective values are derived separately and reversibly; the
  source fields are never overwritten.
- ``row_id`` is deterministic so repeated runs produce identical identifiers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, fields
from typing import Optional

# ---------------------------------------------------------------------------
# Ledger roles
# ---------------------------------------------------------------------------
ROLE_ORG = "org_ledger"
ROLE_PARTY = "party_ledger"
LEDGER_ROLES: tuple[str, ...] = (ROLE_ORG, ROLE_PARTY)


# ---------------------------------------------------------------------------
# Record kinds: every row is explicitly classified. Non-transaction rows must
# never be silently mixed into transaction calculations.
# ---------------------------------------------------------------------------
class RecordKind:
    TRANSACTION = "Transaction"
    OPENING_BALANCE = "OpeningBalance"
    CLOSING_BALANCE = "ClosingBalance"
    TOTAL = "Total"
    GRAND_TOTAL = "GrandTotal"
    CARRIED_FORWARD = "CarriedForward"
    BROUGHT_FORWARD = "BroughtForward"
    BALANCE_FORWARD = "BalanceForward"
    HEADER = "Header"
    FOOTER = "Footer"
    UNKNOWN = "Unknown"
    # Clearly non-transaction structural rows that cannot be classified exactly.
    UNKNOWN_SPECIAL = "UnknownSpecial"


ALL_RECORD_KINDS: tuple[str, ...] = (
    RecordKind.TRANSACTION,
    RecordKind.OPENING_BALANCE,
    RecordKind.CLOSING_BALANCE,
    RecordKind.TOTAL,
    RecordKind.GRAND_TOTAL,
    RecordKind.CARRIED_FORWARD,
    RecordKind.BROUGHT_FORWARD,
    RecordKind.BALANCE_FORWARD,
    RecordKind.HEADER,
    RecordKind.FOOTER,
    RecordKind.UNKNOWN,
    RecordKind.UNKNOWN_SPECIAL,
)

# Record kinds that participate in transaction-level reasoning. Everything else
# is structural and must be kept separate (but still auditable in the sheet).
TRANSACTION_KINDS: frozenset[str] = frozenset({RecordKind.TRANSACTION})

# Structural / special record kinds that must never enter transaction matching
# but must still be emitted and auditable.
SPECIAL_RECORD_KINDS: frozenset[str] = frozenset(
    {
        RecordKind.OPENING_BALANCE,
        RecordKind.CLOSING_BALANCE,
        RecordKind.TOTAL,
        RecordKind.GRAND_TOTAL,
        RecordKind.CARRIED_FORWARD,
        RecordKind.BROUGHT_FORWARD,
        RecordKind.BALANCE_FORWARD,
        RecordKind.UNKNOWN_SPECIAL,
    }
)


@dataclass
class LedgerRow:
    """One canonical, audit-ready ledger row.

    All amount fields are optional floats; ``None`` means "absent in source",
    which is never fabricated into a number.
    """

    # Identity / traceability
    row_id: str
    record_kind: str
    ledger_role: str
    detected_ledger_name: str
    source_file: str
    page_number: int
    source_row_number: int

    # Parsed transaction content
    date: str = ""
    raw_date: str = ""
    normalized_date: str = ""
    date_parse_confidence: float = 0.0
    raw_type: str = ""
    type_label: str = ""
    particulars: str = ""
    account: str = ""
    voucher_no: str = ""
    reference_no: str = ""
    normalized_reference: str = ""

    # Source-preserved monetary values (verbatim interpretation of the source)
    debit_source: Optional[float] = None
    credit_source: Optional[float] = None
    balance_source: Optional[float] = None
    balance_side_source: str = ""
    debit_raw_source: str = ""
    credit_raw_source: str = ""
    balance_raw_source: str = ""
    amount_parse_confidence: float = 0.0

    # Organisation-perspective values (reversible, derived from source)
    debit_org_perspective: Optional[float] = None
    credit_org_perspective: Optional[float] = None
    amount_org_perspective: Optional[float] = None

    # Balance markers (populated only on the relevant special rows)
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None

    # Evidence + quality
    raw_text: str = ""
    extraction_method: str = ""
    confidence_score: float = 0.0
    layout_confidence: float = 0.0
    extraction_strategy_used: str = ""
    column_confidence_json: str = ""
    review_flag: bool = False
    review_reason: str = ""

    # Optional AI date repair audit (source fields above are never overwritten).
    ai_repair_applied: bool = False
    ai_repair_normalized_date: str = ""
    ai_repair_date_confidence: float = 0.0
    ai_repair_reason: str = ""
    ai_repair_explanation: str = ""
    ai_repair_rejected: bool = False
    ai_repair_rejection_reason: str = ""

    # Optional AI unknown-label grouping audit. The model may only suggest one of
    # the predefined canonical labels (config/type_labels.json) or the sentinel
    # ``UnknownNeedsReview``; it can never create a new canonical label. A
    # suggestion is applied to ``type_label`` only when it is a predefined label
    # above the acceptance threshold. ``raw_type`` is never altered.
    ai_label_suggested: str = ""
    ai_label_confidence: float = 0.0
    ai_label_reason: str = ""
    ai_label_applied: bool = False
    ai_label_rejected: bool = False

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# Canonical column order for the two ledger sheets. Derived directly from the
# dataclass field order so the schema and the model can never drift apart.
LEDGER_SHEET_COLUMNS: list[str] = [f.name for f in fields(LedgerRow)]


@dataclass
class LedgerIdentity:
    """Detected identity of a ledger PDF (the account whose ledger this is)."""

    ledger_role: str
    source_file: str
    detected_title: str
    owner_company: str = ""
    period_text: str = ""
    detection_method: str = ""
    confidence: float = 0.0


@dataclass
class ExtractionAuditRow:
    """One per-page extraction audit record. No financial row vanishes silently."""

    ledger_role: str
    source_file: str
    page_number: int
    extraction_method: str
    status: str
    rows_emitted: int
    rows_dropped: int
    detail: str = ""

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class ValidationItem:
    """One validation check result written into Validation_Report."""

    check: str
    scope: str
    status: str  # PASS | REVIEW | FAIL | INFO
    count: int
    details: str = ""

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class LayoutProfile:
    """Compact layout description, optionally produced by the AI profiler.

    This is intentionally small: it never carries full ledger content, only a
    structural description of how to read columns.
    """

    layout_type: str = ""
    date_carry_forward: bool = False
    columns: dict[str, str] = field(default_factory=dict)
    ignore_row_patterns: list[str] = field(default_factory=list)
    special_row_patterns: dict[str, list[str]] = field(default_factory=dict)
    source: str = "deterministic"  # deterministic | ai_layout | ai_cache


@dataclass
class PageAnalysis:
    """Per-page extraction strategy and confidence summary."""

    page_number: int
    extraction_strategy_used: str
    confidence_score: float
    detected_headers: list[str] = field(default_factory=list)
    detected_columns: list[str] = field(default_factory=list)
    column_confidence: dict[str, float] = field(default_factory=dict)
    transaction_row_count: int = 0
    special_row_count: int = 0
    unknown_row_count: int = 0
    review_row_count: int = 0

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class FormalizedLedger:
    """A fully assembled ledger ready to be written and validated."""

    ledger_role: str
    identity: LedgerIdentity
    sheet_name: str
    rows: list[LedgerRow] = field(default_factory=list)
    audit: list[ExtractionAuditRow] = field(default_factory=list)
    page_analysis: list[PageAnalysis] = field(default_factory=list)
    layout_stats: dict = field(default_factory=dict)
    layout_source: str = "deterministic"

    def transaction_rows(self) -> list[LedgerRow]:
        return [r for r in self.rows if r.record_kind in TRANSACTION_KINDS]

    def rows_of_kind(self, kind: str) -> list[LedgerRow]:
        return [r for r in self.rows if r.record_kind == kind]


def make_row_id(
    ledger_role: str,
    source_file: str,
    page_number: int,
    source_row_number: int,
    raw_text: str,
) -> str:
    """Return a short, deterministic row identifier.

    The same source row always yields the same id across runs, which keeps the
    output diffable and auditable.
    """
    basis = f"{ledger_role}|{source_file}|{page_number}|{source_row_number}|{raw_text}"
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()
    return digest[:12]
