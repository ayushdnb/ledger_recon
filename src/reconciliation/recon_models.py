"""Typed models for deterministic reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    """One deterministic match/candidate/unmatched output row."""

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

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


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

