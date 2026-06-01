"""Reference-number normalization (NO matching/reconciliation).

This layer only produces a normalized form of an invoice/voucher reference so
that a later reconciliation stage can compare references reliably. It makes no
match decisions here.

Normalization rules:
- uppercase
- remove spaces, slashes, hyphens, dots, and other non-essential separators
- preserve the meaningful alphanumeric invoice body and ordering
- never raise: a missing/empty reference normalizes to an empty string

Examples (input -> output):
    SA/PB/2425/017            -> SAPB2425017
    SA/PB/2425/017SC          -> SAPB2425017SC
    SA/PB/25-26/001           -> SAPB2526001
    64858/SA/PB/2425/017SC    -> 64858SAPB2425017SC
    DL/25-26/191              -> DL2526191
    dl/25-26/191              -> DL2526191
    cn/dl/25-26/063           -> CNDL2526063
"""

from __future__ import annotations

import re
from typing import Optional

# Anything that is not a letter or digit is treated as a separator/noise and
# removed. This deliberately keeps the alphanumeric body intact and ordered.
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")


def normalize_reference(value: Optional[str]) -> str:
    """Return the normalized reference string (uppercase, separators removed)."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    stripped = _NON_ALNUM_RE.sub("", text)
    return stripped.upper()
