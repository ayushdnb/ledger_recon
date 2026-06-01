"""Shared deterministic matching helpers (no allocation dependencies)."""

from __future__ import annotations

MISSING_REFERENCE_TOKENS: frozenset[str] = frozenset(
    {"", "none", "nan", "null", "-", "--"}
)


def is_missing_reference(value: object) -> bool:
    """True when a normalized reference must be treated as absent."""
    return str(value if value is not None else "").strip().lower() in MISSING_REFERENCE_TOKENS


def is_type_compatible(org_type: str, party_type: str) -> bool:
    """Conservative type compatibility rules."""
    a = (org_type or "").strip()
    b = (party_type or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    mirror = {
        ("Receipt", "Payment"),
        ("Payment", "Receipt"),
        ("CreditNote", "DebitNote"),
        ("DebitNote", "CreditNote"),
    }
    return (a, b) in mirror


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def is_mirror_polarity_plausible(org, party) -> bool:
    """True when source ledgers mirror while the org perspective agrees."""
    org_source_sign = _sign(org.net_source)
    party_source_sign = _sign(party.net_source)
    org_perspective_sign = _sign(org.net_org)
    party_perspective_sign = _sign(party.net_org)
    return (
        org_source_sign != 0
        and party_source_sign != 0
        and org_source_sign == -party_source_sign
        and org_perspective_sign != 0
        and org_perspective_sign == party_perspective_sign
    )


def amount_close(a: float, b: float, tolerance: float) -> bool:
    return abs(abs(a) - abs(b)) <= tolerance
