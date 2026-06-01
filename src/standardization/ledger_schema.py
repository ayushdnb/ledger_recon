"""Strict pydantic schema for AI-assisted ledger standardization output.

This schema validates the JSON returned by the AI model. It enforces:

- Missing values are ``null`` (None), never invented.
- ``canonical_type`` is restricted to the approved label set (plus ``Unknown``).
- Opening/closing balances are modelled separately from transaction rows.
- Malformed JSON is rejected via :func:`parse_ai_result`.

No reconciliation, matching, or final-balance logic lives here. This is purely a
structural / type contract for standardized data.
"""

from __future__ import annotations

import json
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from src.standardization.type_labeler import ALLOWED_CANONICAL_TYPES

BalanceKind = Literal["OpeningBalance", "ClosingBalance"]


class SchemaError(ValueError):
    """Raised when AI output cannot be parsed/validated against the schema."""


class OpeningClosingBalance(BaseModel):
    """An opening or closing balance treated as metadata, not a transaction."""

    model_config = ConfigDict(extra="forbid")

    balance_kind: BalanceKind
    ledger_role: str
    source_file_name: Optional[str] = None
    amount: Optional[float] = None
    balance_side: Optional[str] = None
    date_or_period: Optional[str] = None
    source_page: Optional[int] = None
    source_text: Optional[str] = None
    found_location: Optional[str] = None
    confidence: Optional[float] = None
    review_required: bool = False
    review_reason: Optional[str] = None


class StandardizedLedgerRow(BaseModel):
    """A single standardized ledger transaction row."""

    model_config = ConfigDict(extra="forbid")

    source_file: Optional[str] = None
    source_page: Optional[int] = None
    source_row_number: Optional[int] = None
    ledger_role: str
    party_name: Optional[str] = None
    ledger_period_start: Optional[str] = None
    ledger_period_end: Optional[str] = None
    transaction_date: Optional[str] = None
    raw_type: Optional[str] = None
    canonical_type: str
    type_match_method: Optional[str] = None
    type_match_score: Optional[float] = None
    voucher_or_bill_no: Optional[str] = None
    reference_no: Optional[str] = None
    account_or_particulars: Optional[str] = None
    narration: Optional[str] = None
    debit: Optional[float] = None
    credit: Optional[float] = None
    net_amount: Optional[float] = None
    balance: Optional[float] = None
    balance_side: Optional[str] = None
    extraction_confidence: Optional[float] = None
    review_required: bool = False
    review_reason: Optional[str] = None
    raw_text: Optional[str] = None
    extraction_method: Optional[str] = None

    @field_validator("canonical_type")
    @classmethod
    def _validate_canonical_type(cls, value: str) -> str:
        if value not in ALLOWED_CANONICAL_TYPES:
            raise ValueError(
                f"canonical_type '{value}' is not an approved label. "
                f"Allowed: {', '.join(ALLOWED_CANONICAL_TYPES)}."
            )
        return value


class AIStandardizationResult(BaseModel):
    """Top-level standardization payload returned by the AI model."""

    model_config = ConfigDict(extra="forbid")

    pair_id: str
    detected_org_name: Optional[str] = None
    detected_party_name: Optional[str] = None
    detected_account_name: Optional[str] = None
    detected_period_start: Optional[str] = None
    detected_period_end: Optional[str] = None
    opening_closing_balances: list[OpeningClosingBalance] = []
    org_ledger_rows: list[StandardizedLedgerRow] = []
    party_ledger_rows: list[StandardizedLedgerRow] = []
    uncertain_fields: list[str] = []
    extraction_warnings: list[str] = []


def parse_ai_result(raw_text: str) -> AIStandardizationResult:
    """Parse and validate raw AI text into an :class:`AIStandardizationResult`.

    Rejects malformed JSON and schema violations with a :class:`SchemaError`.
    """
    text = (raw_text or "").strip()
    if not text:
        raise SchemaError("AI returned empty output.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"AI output was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaError("AI output JSON must be an object at the top level.")
    try:
        return AIStandardizationResult.model_validate(data)
    except ValidationError as exc:
        raise SchemaError(f"AI output failed schema validation: {exc}") from exc
