"""Formal reconciliation issue taxonomy (primary reason codes + secondary tags).

Every unmatched, partially matched, AI-assisted, ambiguous, or exception row must
carry exactly one primary reason code. Secondary tags are optional.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable

from src.reconciliation.recon_models import MatchRecord, ReconRow

_MISSING_REF = frozenset({"", "none", "nan", "null", "-", "--"})


def _ref_missing(value: object) -> bool:
    return str(value if value is not None else "").strip().lower() in _MISSING_REF


class PrimaryIssueCode(str, Enum):
    EXACT_MATCH = "ExactMatch"
    TIMING_DIFFERENCE = "TimingDifference"
    MISSING_TRANSACTION = "MissingTransaction"
    DUPLICATE_TRANSACTION = "DuplicateTransaction"
    PARTIAL_SETTLEMENT = "PartialSettlement"
    OVERPAYMENT = "Overpayment"
    UNDERPAYMENT = "Underpayment"
    TAX_DIFFERENCE = "TaxDifference"
    CURRENCY_DIFFERENCE = "CurrencyDifference"
    REFERENCE_MISMATCH = "ReferenceMismatch"
    ACCOUNT_MAPPING_ERROR = "AccountMappingError"
    CREDIT_NOTE_ISSUE = "CreditNoteIssue"
    DEBIT_NOTE_ISSUE = "DebitNoteIssue"
    BANK_CHARGE_DIFFERENCE = "BankChargeDifference"
    DISCOUNT_DIFFERENCE = "DiscountDifference"
    PERIOD_CUTOFF_ISSUE = "PeriodCutOffIssue"
    DATA_QUALITY_ISSUE = "DataQualityIssue"
    MASTER_DATA_ISSUE = "MasterDataIssue"
    INTEGRATION_FAILURE = "IntegrationFailure"
    FRAUD_RISK = "FraudRisk"
    AMBIGUOUS_MATCH = "AmbiguousMatch"
    MANUAL_REVIEW_REQUIRED = "ManualReviewRequired"
    UNKNOWN_EXCEPTION = "UnknownException"


ALL_PRIMARY_CODES: frozenset[str] = frozenset(c.value for c in PrimaryIssueCode)

# AI arbitration may emit compact slugs; map to canonical primary codes.
AI_REASON_ALIASES: dict[str, str] = {
    "amount_date_agree": PrimaryIssueCode.EXACT_MATCH.value,
    "exact_reference": PrimaryIssueCode.EXACT_MATCH.value,
    "timing_difference": PrimaryIssueCode.TIMING_DIFFERENCE.value,
    "date_shift": PrimaryIssueCode.TIMING_DIFFERENCE.value,
    "rounding": PrimaryIssueCode.EXACT_MATCH.value,
    "grouped_amount": PrimaryIssueCode.PARTIAL_SETTLEMENT.value,
    "partial_settlement": PrimaryIssueCode.PARTIAL_SETTLEMENT.value,
    "unmatched": PrimaryIssueCode.MISSING_TRANSACTION.value,
    "review": PrimaryIssueCode.MANUAL_REVIEW_REQUIRED.value,
    "contextual": PrimaryIssueCode.AMBIGUOUS_MATCH.value,
    "voucher_typo": PrimaryIssueCode.REFERENCE_MISMATCH.value,
    "side_mirror": PrimaryIssueCode.EXACT_MATCH.value,
    "tax_difference": PrimaryIssueCode.TAX_DIFFERENCE.value,
    "tds": PrimaryIssueCode.TAX_DIFFERENCE.value,
    "bank_charge": PrimaryIssueCode.BANK_CHARGE_DIFFERENCE.value,
    "discount": PrimaryIssueCode.DISCOUNT_DIFFERENCE.value,
}


def normalize_ai_reason_code(code: str) -> str:
    """Map AI reason slugs to canonical primary codes; empty if unmappable."""
    raw = (code or "").strip()
    if not raw:
        return ""
    if is_valid_primary_code(raw):
        return raw
    return AI_REASON_ALIASES.get(raw, "")


def is_valid_primary_code(code: str) -> bool:
    return (code or "").strip() in ALL_PRIMARY_CODES


def classify_match_record(record: MatchRecord) -> tuple[str, str]:
    """Assign primary issue code and JSON-serializable secondary tags for a match row."""
    tags: list[str] = []
    status = record.match_status
    rule = (record.match_rule or "").lower()

    if status in {"matched_strong", "matched_supported", "matched_ai"}:
        if "timing" in rule or "date_tolerance" in rule:
            return PrimaryIssueCode.TIMING_DIFFERENCE.value, ""
        if "rounding" in rule:
            return PrimaryIssueCode.EXACT_MATCH.value, "rounding_residual"
        if "credit" in rule or "debit_note" in rule:
            return PrimaryIssueCode.EXACT_MATCH.value, "note_netting"
        if "allocation" in rule or "one_to_many" in rule or "many_to_one" in rule:
            return PrimaryIssueCode.PARTIAL_SETTLEMENT.value, "allocated"
        return PrimaryIssueCode.EXACT_MATCH.value, ""

    if status == "candidate_review":
        if "ambiguous" in rule or "multiple" in (record.review_reason or "").lower():
            return PrimaryIssueCode.AMBIGUOUS_MATCH.value, ""
        if "fuzzy" in rule:
            return PrimaryIssueCode.REFERENCE_MISMATCH.value, "fuzzy_candidate"
        if _ref_missing(record.org_normalized_reference) or _ref_missing(
            record.party_normalized_reference
        ):
            return PrimaryIssueCode.DATA_QUALITY_ISSUE.value, "missing_reference"
        if record.amount_difference and record.amount_difference > 0.01:
            return PrimaryIssueCode.REFERENCE_MISMATCH.value, "amount_date_candidate"
        return PrimaryIssueCode.MANUAL_REVIEW_REQUIRED.value, ""

    if status == "unmatched_org":
        if "duplicate" in rule:
            return PrimaryIssueCode.DUPLICATE_TRANSACTION.value, "org_side"
        return PrimaryIssueCode.MISSING_TRANSACTION.value, "org_unmatched"

    if status == "unmatched_party":
        if "duplicate" in rule:
            return PrimaryIssueCode.DUPLICATE_TRANSACTION.value, "party_side"
        return PrimaryIssueCode.MISSING_TRANSACTION.value, "party_unmatched"

    if record.validation_status == "REJECTED":
        return PrimaryIssueCode.MANUAL_REVIEW_REQUIRED.value, "ai_rejected"

    return PrimaryIssueCode.UNKNOWN_EXCEPTION.value, ""


def classify_unmatched_row(row: ReconRow, *, side: str) -> str:
    """Classify an unmatched ledger row conservatively."""
    if row.type_label in {"CreditNote"}:
        return PrimaryIssueCode.CREDIT_NOTE_ISSUE.value
    if row.type_label in {"DebitNote"}:
        return PrimaryIssueCode.DEBIT_NOTE_ISSUE.value
    if row.type_label in {"TDS"}:
        return PrimaryIssueCode.TAX_DIFFERENCE.value
    if row.review_flag or (row.confidence_score and row.confidence_score < 80):
        return PrimaryIssueCode.DATA_QUALITY_ISSUE.value
    if row.type_label in {"Unknown", "UnknownNeedsReview"}:
        return PrimaryIssueCode.MANUAL_REVIEW_REQUIRED.value
    return PrimaryIssueCode.MISSING_TRANSACTION.value


def classify_amount_residual(
    org_amount: float,
    party_amount: float,
    *,
    residual: float,
    org_label: str,
    party_label: str,
    write_off_threshold: float,
    bank_charge_threshold: float,
    discount_threshold: float,
) -> str:
    """Classify a signed amount residual between matched groups."""
    if org_label == "TDS" or party_label == "TDS":
        return PrimaryIssueCode.TAX_DIFFERENCE.value
    if residual <= write_off_threshold:
        return PrimaryIssueCode.EXACT_MATCH.value
    if org_label in {"Payment", "Receipt"} or party_label in {"Payment", "Receipt"}:
        if residual <= bank_charge_threshold:
            return PrimaryIssueCode.BANK_CHARGE_DIFFERENCE.value
    if "discount" in org_label.lower() or "discount" in party_label.lower():
        if residual <= discount_threshold:
            return PrimaryIssueCode.DISCOUNT_DIFFERENCE.value
    if abs(org_amount) > abs(party_amount):
        return PrimaryIssueCode.OVERPAYMENT.value
    return PrimaryIssueCode.UNDERPAYMENT.value


def ensure_codes_on_records(records: Iterable[MatchRecord]) -> None:
    """Populate primary_issue_code on records when absent (in-place)."""
    for record in records:
        if not getattr(record, "primary_issue_code", ""):
            code, tags = classify_match_record(record)
            record.primary_issue_code = code
            record.secondary_issue_tags = tags
