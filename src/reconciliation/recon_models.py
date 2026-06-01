"""Typed models for deterministic-first reconciliation and AI arbitration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FinalMatchDecisionSource(str, Enum):
    """Authority responsible for the final workbook placement."""

    DETERMINISTIC = "deterministic"
    AI = "AI"
    HUMAN_REVIEW_REQUIRED = "human_review_required"


@dataclass
class ReconRow:
    """One loaded formalized ledger row with reconciliation helpers."""

    data: dict[str, Any]
    sheet_name: str

    @property
    def row_id(self) -> str:
        return str(self.data.get("row_id", "")).strip()

    @property
    def record_kind(self) -> str:
        return str(self.data.get("record_kind", "")).strip()

    @property
    def type_label(self) -> str:
        return str(self.data.get("type_label", "")).strip()

    @property
    def date(self) -> str:
        return str(
            self.data.get("normalized_date")
            or self.data.get("date")
            or self.data.get("raw_date")
            or ""
        ).strip()

    @property
    def normalized_reference(self) -> str:
        return str(self.data.get("normalized_reference", "")).strip()

    @property
    def reference(self) -> str:
        return str(
            self.data.get("reference_no")
            or self.data.get("voucher_no")
            or ""
        ).strip()

    @property
    def net_source(self) -> float:
        debit = float(self.data.get("debit_source") or 0.0)
        credit = float(self.data.get("credit_source") or 0.0)
        return debit - credit

    @property
    def net_org(self) -> float:
        debit = float(self.data.get("debit_org_perspective") or 0.0)
        credit = float(self.data.get("credit_org_perspective") or 0.0)
        if debit or credit:
            return debit - credit
        return float(self.data.get("amount_org_perspective") or 0.0)

    @property
    def review_flag(self) -> bool:
        value = self.data.get("review_flag", False)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "1", "yes"}

    @property
    def confidence_score(self) -> float:
        return float(self.data.get("confidence_score") or 0.0)


@dataclass
class MatchRecord:
    """One final match, ledger-only placement, or review-required output row."""

    match_group_id: str
    match_status: str
    match_confidence: float
    match_rule: str
    type_label: str
    org_row_id: str = ""
    party_row_id: str = ""
    org_date: str = ""
    party_date: str = ""
    date_delta_days: int | None = None
    org_reference: str = ""
    party_reference: str = ""
    org_normalized_reference: str = ""
    party_normalized_reference: str = ""
    reference_relation: str = "missing"
    org_amount: float | None = None
    party_amount: float | None = None
    amount_difference: float | None = None
    org_raw_type: str = ""
    party_raw_type: str = ""
    org_particulars: str = ""
    party_particulars: str = ""
    review_required: bool = False
    review_reason: str = ""
    reviewer_comment: str = ""
    manual_status: str = ""
    decision_id: str = ""
    decision_source: str = FinalMatchDecisionSource.DETERMINISTIC.value
    match_type: str = ""
    org_row_ids: list[str] = field(default_factory=list)
    party_row_ids: list[str] = field(default_factory=list)
    compact_explanation: str = ""
    validation_status: str = ""
    validation_reason: str = ""
    prompt_fingerprint: str = ""
    response_fingerprint: str = ""
    cache_key: str = ""
    primary_issue_code: str = ""
    secondary_issue_tags: str = ""
    amount_tolerance_used: float | None = None
    date_tolerance_days_used: int | None = None
    reviewer_status: str = ""
    selected_match_group_id: str = ""
    reviewer_selected_org_row_ids: str = ""
    reviewer_selected_party_row_ids: str = ""
    manual_issue_code: str = ""
    reviewed_by: str = ""
    reviewed_at: str = ""
    override_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = self.__dict__.copy()
        payload["org_row_ids"] = json.dumps(self.org_row_ids or ([self.org_row_id] if self.org_row_id else []))
        payload["party_row_ids"] = json.dumps(
            self.party_row_ids or ([self.party_row_id] if self.party_row_id else [])
        )
        return payload


@dataclass
class UnresolvedDeterministicMatchCluster:
    """A coherent set of rows left unresolved by deterministic matching."""

    cluster_id: str
    org_row_ids: list[str]
    party_row_ids: list[str]
    deterministic_reason: str


@dataclass
class AIReconciliationCandidate:
    """Minimal, source-traceable candidate evidence sent to the AI arbiter."""

    side: str
    row_id: str
    type_label: str
    raw_date: str
    normalized_date: str
    voucher_no: str
    bill_no: str
    reference_no: str
    normalized_reference: str
    particulars_ref: str
    account_ref: str
    debit_source: float
    credit_source: float
    debit_org_perspective: float
    credit_org_perspective: float
    amount_org_perspective: float
    source_file: str
    page_number: str
    source_row_number: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class AIReconciliationComparison:
    """Deterministic pairwise facts included to reduce model arithmetic errors."""

    org_row_id: str
    party_row_id: str
    amount_delta: float
    date_delta_days: int | None
    reference_relation: str
    reference_similarity: float
    type_compatible: bool
    source_side_mirror: bool

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class AIReconciliationCandidatePacket:
    """Bounded evidence packet for one semantically coherent unresolved cluster."""

    packet_id: str
    cluster_id: str
    deterministic_reason: str
    candidates: list[AIReconciliationCandidate]
    comparisons: list[AIReconciliationComparison]
    text_dictionary: dict[str, str]
    fingerprint: str
    over_candidate_limit: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "cluster_id": self.cluster_id,
            "deterministic_reason": self.deterministic_reason,
            "text_dictionary": self.text_dictionary,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "comparisons": [comparison.as_dict() for comparison in self.comparisons],
        }


@dataclass
class AIReconciliationRequest:
    """One strict JSON-only request prepared for the provider abstraction."""

    packet_id: str
    prompt_fingerprint: str
    estimated_input_tokens: int
    messages: list[dict[str, str]]


@dataclass
class AIMatchDecision:
    """Schema-validated decision returned by the AI arbiter."""

    decision_id: str
    decision_type: str
    org_row_ids: list[str]
    party_row_ids: list[str]
    match_type: str
    confidence: float
    amount_delta: float
    date_delta_days: int
    reason_code: str
    explanation: str
    review_reason: str | None


@dataclass
class AIReconciliationResponse:
    """Parsed provider response plus audit fingerprints."""

    decisions: list[AIMatchDecision]
    prompt_fingerprint: str
    response_fingerprint: str
    cache_key: str
    cache_hit: bool


@dataclass
class AIValidationResult:
    """Deterministic post-validation result for one AI decision."""

    decision_id: str
    accepted: bool
    status: str
    rejection_reason: str = ""
    actual_amount_delta: float | None = None
    actual_date_delta_days: int | None = None


@dataclass
class AIArbitrationResult:
    """Final records and metrics produced by the AI arbitration layer."""

    records: list[MatchRecord]
    packets: list[AIReconciliationCandidatePacket] = field(default_factory=list)
    validation_results: list[AIValidationResult] = field(default_factory=list)
    provider_call_count: int = 0
    cache_hit_count: int = 0
    cache_miss_count: int = 0
    accepted_decision_count: int = 0
    review_required_count: int = 0


@dataclass
class ValidationItem:
    """Validation row for reconciliation workbook."""

    check: str
    scope: str
    status: str
    count: int | float
    details: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class FormulaAuditItem:
    """Formula audit row for required workbook formulas."""

    sheet_name: str
    cell: str
    expected_formula_category: str
    formula_present: bool
    formula_text: str
    status: str
    issue: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ReconBuildResult:
    """Result bundle from reconciliation pipeline."""

    pair_id: str
    formalized_path: str
    output_path: str
    central_output_path: str
    org_sheet: str
    party_sheet: str
    labels: list[str]
    matches: list[MatchRecord] = field(default_factory=list)
    validation_items: list[ValidationItem] = field(default_factory=list)
    formula_audit: list[FormulaAuditItem] = field(default_factory=list)
