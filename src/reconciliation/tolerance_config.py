"""Central reconciliation tolerance policy with per-label overrides.

Global defaults are conservative. Per-label overrides allow business-appropriate
flexibility (e.g. bank charges, write-offs) without silently hiding material gaps.
Every tolerance applied during matching is recorded in match evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# Canonical label keys aligned with config/type_labels.json.
LABEL_KEYS = (
    "Invoice",
    "Payment",
    "Receipt",
    "CreditNote",
    "DebitNote",
    "Journal",
    "OpeningBalance",
    "ClosingBalance",
    "TDS",
    "PQV",
    "Unknown",
    "UnknownNeedsReview",
    "BankCharge",
    "Discount",
    "WriteOff",
)

# Conservative global defaults (amounts in org-perspective currency units).
DEFAULT_AMOUNT_TOLERANCE = 0.01
DEFAULT_ROUNDING_TOLERANCE = 0.05
DEFAULT_DATE_TOLERANCE_DAYS = 7
DEFAULT_FUZZY_REFERENCE_THRESHOLD = 0.85
DEFAULT_AI_MIN_CONFIDENCE = 0.75
DEFAULT_MAX_CANDIDATE_PACKET_SIZE = 12
DEFAULT_MAX_COMBINATION_SEARCH_SIZE = 8
DEFAULT_SMALL_WRITE_OFF_THRESHOLD = 1.0
DEFAULT_BANK_CHARGE_THRESHOLD = 50.0
DEFAULT_DISCOUNT_THRESHOLD = 100.0

_INTEGER_FIELDS = frozenset({"date_tolerance_days", "max_combination_search_size"})


@dataclass(frozen=True)
class LabelTolerance:
    """Effective tolerances for one canonical type label."""

    amount_tolerance: float
    rounding_tolerance: float
    date_tolerance_days: int
    fuzzy_reference_threshold: float
    small_write_off_threshold: float
    bank_charge_threshold: float
    discount_threshold: float
    max_combination_search_size: int

    def effective_amount_tolerance(self, *, for_rounding: bool = False) -> float:
        return self.rounding_tolerance if for_rounding else self.amount_tolerance


@dataclass(frozen=True)
class ReconTolerancePolicy:
    """Resolved tolerance policy for a reconciliation run."""

    amount_tolerance: float
    rounding_tolerance: float
    date_tolerance_days: int
    fuzzy_reference_threshold: float
    ai_min_confidence: float
    max_candidate_packet_size: int
    max_combination_search_size: int
    small_write_off_threshold: float
    bank_charge_threshold: float
    discount_threshold: float
    label_overrides: Mapping[str, LabelTolerance]
    config_version: str = "recon-tolerance-v1"

    def for_label(self, type_label: str) -> LabelTolerance:
        key = (type_label or "").strip()
        if key in self.label_overrides:
            return self.label_overrides[key]
        return LabelTolerance(
            amount_tolerance=self.amount_tolerance,
            rounding_tolerance=self.rounding_tolerance,
            date_tolerance_days=self.date_tolerance_days,
            fuzzy_reference_threshold=self.fuzzy_reference_threshold,
            small_write_off_threshold=self.small_write_off_threshold,
            bank_charge_threshold=self.bank_charge_threshold,
            discount_threshold=self.discount_threshold,
            max_combination_search_size=self.max_combination_search_size,
        )

    def pair_tolerance(self, org_label: str, party_label: str) -> LabelTolerance:
        """Use the stricter of the two labels' tolerances for a candidate pair."""
        org_t = self.for_label(org_label)
        party_t = self.for_label(party_label)
        return LabelTolerance(
            amount_tolerance=min(org_t.amount_tolerance, party_t.amount_tolerance),
            rounding_tolerance=min(org_t.rounding_tolerance, party_t.rounding_tolerance),
            date_tolerance_days=min(org_t.date_tolerance_days, party_t.date_tolerance_days),
            fuzzy_reference_threshold=max(
                org_t.fuzzy_reference_threshold, party_t.fuzzy_reference_threshold
            ),
            small_write_off_threshold=min(
                org_t.small_write_off_threshold, party_t.small_write_off_threshold
            ),
            bank_charge_threshold=min(
                org_t.bank_charge_threshold, party_t.bank_charge_threshold
            ),
            discount_threshold=min(org_t.discount_threshold, party_t.discount_threshold),
            max_combination_search_size=min(
                org_t.max_combination_search_size, party_t.max_combination_search_size
            ),
        )


def _validate_label_tolerance(label: str, values: Mapping[str, Any]) -> None:
    allowed = frozenset(LabelTolerance.__dataclass_fields__)
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(
            f"Unsupported reconciliation tolerance override(s) for {label!r}: "
            f"{', '.join(unknown)}."
        )
    for field_name, raw in values.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(
                f"Reconciliation tolerance override {label}.{field_name} must be numeric."
            )
        if raw < 0:
            raise ValueError(
                f"Reconciliation tolerance override {label}.{field_name} must be non-negative."
            )
        if field_name in _INTEGER_FIELDS and not isinstance(raw, int):
            raise ValueError(
                f"Reconciliation tolerance override {label}.{field_name} must be an integer."
            )
        if field_name == "fuzzy_reference_threshold" and raw > 1:
            raise ValueError(
                f"Reconciliation tolerance override {label}.{field_name} must be <= 1."
            )


def _with_label_overrides(
    policy: ReconTolerancePolicy,
    custom: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, LabelTolerance]:
    """Merge validated operator overrides over the conservative defaults."""
    merged = dict(policy.label_overrides)
    for label, values in (custom or {}).items():
        if not isinstance(label, str) or not label.strip() or not isinstance(values, Mapping):
            raise ValueError("Reconciliation label tolerance overrides must map labels to objects.")
        _validate_label_tolerance(label, values)
        current = policy.for_label(label)
        payload = {
            field_name: getattr(current, field_name)
            for field_name in LabelTolerance.__dataclass_fields__
        }
        payload.update(values)
        merged[label.strip()] = LabelTolerance(**payload)
    return merged


def _label_override(
    base: ReconTolerancePolicy,
    label: str,
    *,
    amount: float | None = None,
    rounding: float | None = None,
    date_days: int | None = None,
    fuzzy: float | None = None,
    write_off: float | None = None,
    bank_charge: float | None = None,
    discount: float | None = None,
    combo_size: int | None = None,
) -> LabelTolerance:
    lt = base.for_label(label)
    return LabelTolerance(
        amount_tolerance=amount if amount is not None else lt.amount_tolerance,
        rounding_tolerance=rounding if rounding is not None else lt.rounding_tolerance,
        date_tolerance_days=date_days if date_days is not None else lt.date_tolerance_days,
        fuzzy_reference_threshold=fuzzy if fuzzy is not None else lt.fuzzy_reference_threshold,
        small_write_off_threshold=write_off if write_off is not None else lt.small_write_off_threshold,
        bank_charge_threshold=bank_charge if bank_charge is not None else lt.bank_charge_threshold,
        discount_threshold=discount if discount is not None else lt.discount_threshold,
        max_combination_search_size=combo_size if combo_size is not None else lt.max_combination_search_size,
    )


def build_default_label_overrides(base: ReconTolerancePolicy) -> dict[str, LabelTolerance]:
    """Conservative per-label overrides; never widen amount tolerance materially."""
    return {
        "Invoice": _label_override(base, "Invoice", rounding=0.02, date_days=14),
        "Payment": _label_override(base, "Payment", date_days=14, rounding=0.02),
        "Receipt": _label_override(base, "Receipt", date_days=14, rounding=0.02),
        "CreditNote": _label_override(
            base, "CreditNote", amount=base.amount_tolerance, date_days=21, rounding=0.02
        ),
        "DebitNote": _label_override(
            base, "DebitNote", amount=base.amount_tolerance, date_days=21, rounding=0.02
        ),
        "Journal": _label_override(base, "Journal", date_days=30, rounding=0.10),
        "OpeningBalance": _label_override(
            base, "OpeningBalance", amount=0.0, date_days=0, combo_size=0
        ),
        "ClosingBalance": _label_override(
            base, "ClosingBalance", amount=0.0, date_days=0, combo_size=0
        ),
        "TDS": _label_override(base, "TDS", rounding=0.50, date_days=30),
        "BankCharge": _label_override(
            base,
            "BankCharge",
            amount=base.bank_charge_threshold,
            bank_charge=base.bank_charge_threshold,
            date_days=14,
        ),
        "Discount": _label_override(
            base,
            "Discount",
            amount=base.discount_threshold,
            discount=base.discount_threshold,
            date_days=14,
        ),
        "WriteOff": _label_override(
            base,
            "WriteOff",
            amount=base.small_write_off_threshold,
            write_off=base.small_write_off_threshold,
        ),
        "Unknown": _label_override(base, "Unknown", amount=0.0, combo_size=0),
        "UnknownNeedsReview": _label_override(
            base, "UnknownNeedsReview", amount=0.0, combo_size=0
        ),
    }


def build_tolerance_policy(
    *,
    amount_tolerance: float = DEFAULT_AMOUNT_TOLERANCE,
    rounding_tolerance: float = DEFAULT_ROUNDING_TOLERANCE,
    date_tolerance_days: int = DEFAULT_DATE_TOLERANCE_DAYS,
    fuzzy_reference_threshold: float = DEFAULT_FUZZY_REFERENCE_THRESHOLD,
    ai_min_confidence: float = DEFAULT_AI_MIN_CONFIDENCE,
    max_candidate_packet_size: int = DEFAULT_MAX_CANDIDATE_PACKET_SIZE,
    max_combination_search_size: int = DEFAULT_MAX_COMBINATION_SEARCH_SIZE,
    small_write_off_threshold: float = DEFAULT_SMALL_WRITE_OFF_THRESHOLD,
    bank_charge_threshold: float = DEFAULT_BANK_CHARGE_THRESHOLD,
    discount_threshold: float = DEFAULT_DISCOUNT_THRESHOLD,
    label_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> ReconTolerancePolicy:
    """Build the authoritative tolerance policy for deterministic reconciliation."""
    _validate_label_tolerance(
        "global",
        {
            "amount_tolerance": amount_tolerance,
            "rounding_tolerance": rounding_tolerance,
            "date_tolerance_days": date_tolerance_days,
            "fuzzy_reference_threshold": fuzzy_reference_threshold,
            "small_write_off_threshold": small_write_off_threshold,
            "bank_charge_threshold": bank_charge_threshold,
            "discount_threshold": discount_threshold,
            "max_combination_search_size": max_combination_search_size,
        },
    )
    if not 0 <= ai_min_confidence <= 1:
        raise ValueError("AI reconciliation minimum confidence must be between 0 and 1.")
    if max_candidate_packet_size < 1:
        raise ValueError("AI reconciliation candidate packet size must be positive.")
    base = ReconTolerancePolicy(
        amount_tolerance=amount_tolerance,
        rounding_tolerance=rounding_tolerance,
        date_tolerance_days=date_tolerance_days,
        fuzzy_reference_threshold=fuzzy_reference_threshold,
        ai_min_confidence=ai_min_confidence,
        max_candidate_packet_size=max_candidate_packet_size,
        max_combination_search_size=max_combination_search_size,
        small_write_off_threshold=small_write_off_threshold,
        bank_charge_threshold=bank_charge_threshold,
        discount_threshold=discount_threshold,
        label_overrides={},
    )
    overrides = build_default_label_overrides(base)
    defaults = ReconTolerancePolicy(
        amount_tolerance=amount_tolerance,
        rounding_tolerance=rounding_tolerance,
        date_tolerance_days=date_tolerance_days,
        fuzzy_reference_threshold=fuzzy_reference_threshold,
        ai_min_confidence=ai_min_confidence,
        max_candidate_packet_size=max_candidate_packet_size,
        max_combination_search_size=max_combination_search_size,
        small_write_off_threshold=small_write_off_threshold,
        bank_charge_threshold=bank_charge_threshold,
        discount_threshold=discount_threshold,
        label_overrides=overrides,
    )
    overrides = _with_label_overrides(defaults, label_overrides)
    return ReconTolerancePolicy(
        amount_tolerance=amount_tolerance,
        rounding_tolerance=rounding_tolerance,
        date_tolerance_days=date_tolerance_days,
        fuzzy_reference_threshold=fuzzy_reference_threshold,
        ai_min_confidence=ai_min_confidence,
        max_candidate_packet_size=max_candidate_packet_size,
        max_combination_search_size=max_combination_search_size,
        small_write_off_threshold=small_write_off_threshold,
        bank_charge_threshold=bank_charge_threshold,
        discount_threshold=discount_threshold,
        label_overrides=overrides,
    )
