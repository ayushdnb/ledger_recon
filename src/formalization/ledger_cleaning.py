"""Pure parsing/cleaning helpers for ledger rows.

Every function here is pure and deterministic so it can be unit tested in
isolation. None of them mutate input or hold global state.

Covered:
- Indian-format amount parsing (e.g. ``1,06,710.00``).
- Date parsing for the formats seen in the source ledgers
  (``22-04-2025``, ``1-4-2025``, ``22-Apr-25``, ``1-Apr-25``).
- Balance side parsing (``28,630.00 Cr`` -> amount + "Cr").
- Record-kind detection from row text.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from src.formalization.ledger_models import RecordKind

# An amount token: optional parens (negatives), digits with Indian/Western
# grouping commas, optional decimals. "Rs.", currency words and Cr/Dr suffixes
# are stripped before matching.
_AMOUNT_CORE = r"\(?-?\d[\d,]*(?:\.\d+)?\)?"
_AMOUNT_RE = re.compile(rf"^{_AMOUNT_CORE}$")

_CR_DR_RE = re.compile(r"\b(cr|dr)\b", re.IGNORECASE)

# Date input formats, tried in order. ``%b`` matches "Apr" style month names.
_DATE_FORMATS: tuple[str, ...] = (
    "%d-%m-%Y",
    "%d-%m-%y",
    "%d-%b-%y",
    "%d-%b-%Y",
    "%d/%m/%Y",
    "%d/%m/%y",
    "%d/%b/%y",
    "%d/%b/%Y",
    "%d.%m.%Y",
    "%d.%m.%y",
    "%B %d %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%b %d, %Y",
)

_DATE_LIKE_RE = re.compile(
    r"^\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,})[-/.]\d{2,4}$"
)


def clean_text(value: Optional[str]) -> str:
    """Collapse whitespace and strip; ``None`` becomes empty string."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_amount(value: Optional[str]) -> Optional[float]:
    """Parse an amount string to float, or ``None`` if not a clean amount.

    Handles Indian grouping (``1,06,710.00``), surrounding ``Rs.``/currency
    noise, and a trailing ``Cr``/``Dr`` suffix. Parentheses denote negatives.
    Returns ``None`` (never 0.0) when the value is absent or unparseable, so a
    missing amount is never fabricated.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    # Remove currency markers and Cr/Dr side suffix, keep the numeric body.
    text = re.sub(r"(?i)rs\.?", "", text)
    text = text.replace("\u20b9", "")  # rupee sign
    text = _CR_DR_RE.sub("", text).strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").strip()

    if not _AMOUNT_RE.match(text):
        return None

    cleaned = text.replace(",", "")
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    return -amount if negative else amount


def parse_amount_with_confidence(value: Optional[str]) -> tuple[Optional[float], float]:
    """Parse an amount and return a confidence score in [0, 100]."""
    parsed = parse_amount(value)
    if value is None or not str(value).strip():
        return None, 0.0
    if parsed is None:
        return None, 10.0
    text = str(value).strip()
    score = 95.0
    if "(" in text and ")" in text:
        score = min(score, 90.0)
    if _CR_DR_RE.search(text):
        score = min(score, 90.0)
    if "," in text:
        score = min(score, 92.0)
    return parsed, score


def looks_like_amount(value: Optional[str]) -> bool:
    """True if the token parses as an amount."""
    return parse_amount(value) is not None


def parse_balance(value: Optional[str]) -> tuple[Optional[float], str]:
    """Parse a balance cell into (amount, side) where side is 'Cr'/'Dr'/''."""
    if value is None:
        return None, ""
    text = str(value).strip()
    side = ""
    match = _CR_DR_RE.search(text)
    if match:
        side = match.group(1).title()  # 'Cr' or 'Dr'
    return parse_amount(text), side


def parse_balance_with_confidence(value: Optional[str]) -> tuple[Optional[float], str, float]:
    """Parse balance and report a confidence score."""
    amount, side = parse_balance(value)
    if value is None or not str(value).strip():
        return amount, side, 0.0
    if amount is None:
        return amount, side, 10.0
    score = 94.0 if side else 90.0
    return amount, side, score


def looks_like_date(value: Optional[str]) -> bool:
    """True if the token has a date-like shape (not necessarily parseable)."""
    if value is None:
        return False
    return bool(_DATE_LIKE_RE.match(str(value).strip()))


def parse_date(value: Optional[str]) -> Optional[str]:
    """Parse a date to ISO ``YYYY-MM-DD``, or ``None`` if unparseable.

    Two-digit years are interpreted via ``%y`` (Python: 00-68 -> 2000-2068).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.date().isoformat()
    return None


def parse_date_with_confidence(value: Optional[str]) -> tuple[Optional[str], float]:
    """Parse a date to ISO with an explicit confidence score."""
    if value is None:
        return None, 0.0
    text = str(value).strip()
    if not text:
        return None, 0.0
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        score = 96.0
        if "%y" in fmt:
            score = 88.0
        if "%B" in fmt or "%b" in fmt:
            score = min(score, 92.0)
        return parsed.date().isoformat(), score
    return None, 15.0


# ---------------------------------------------------------------------------
# Record-kind detection
# ---------------------------------------------------------------------------
_OPENING_RE = re.compile(
    r"(?i)\b(?:opening\s+(?:bal\.?|balance)|balance\s+brought\s+forward|b/?f)\b"
)
_CLOSING_RE = re.compile(
    r"(?i)\b(?:closing\s+(?:bal\.?|balance)|balance\s+carried\s+forward|c/?f)\b"
)
# Balance c/o, Balance b/d and "Debit/Credit Balance" lines are Tally balance
# carry/closing markers, never transactions.
_BALANCE_FORWARD_RE = re.compile(
    r"(?i)\bbalance\s*(?:c/?[fod]|b/?[fod])\b"
)
_DR_CR_BALANCE_RE = re.compile(
    r"(?i)\b(?:debit|credit|dr\.?|cr\.?)\s+balance\b"
)
# Carried/brought markers: words ("carried over/forward/down") and the Tally
# abbreviations c/o, c/f, c/d (carried) and b/o, b/f, b/d (brought). The plural
# "Totals c/o" / "Totals b/d" forms must resolve here, before the total rule.
_CARRIED_RE = re.compile(
    r"(?i)(?:\bcarried\s+(?:over|forward|down)\b|\bc\s*/\s*[fod]\b|\bc\.f\.)"
)
_BROUGHT_RE = re.compile(
    r"(?i)(?:\bbrought\s+(?:over|forward|down)\b|\bb\s*/\s*[fod]\b|\bb\.f\.)"
)
# Grand total must win over the generic total rule.
_GRAND_TOTAL_RE = re.compile(r"(?i)\bgrand\s+totals?\b")
# Match singular and plural "total(s)" plus "debit/credit total(s)".
_TOTAL_RE = re.compile(
    r"(?i)\b(?:totals?|(?:debit|credit)\s+totals?|sub\s*-?\s*totals?)\b"
)
_HEADER_TOKENS_RE = re.compile(r"(?i)\b(date)\b")
_CONTINUED_RE = re.compile(r"(?i)\b(continued|cont\.|page\s+\d+|carried\s+over)\b")


def detect_record_kind(
    raw_text: str,
    *,
    has_amount: bool,
    has_date: bool,
    has_type: bool,
    is_header: bool = False,
) -> tuple[str, str]:
    """Classify a row into a record kind.

    Returns ``(record_kind, reason)``. ``reason`` is empty for confident
    classifications and explains low-confidence/ambiguous cases otherwise.

    Detection precedence is deliberate: explicit balance/forward/total markers
    win over the generic "looks like a transaction" rule so that opening,
    closing, total, and forward rows are never absorbed into transactions.
    """
    text = clean_text(raw_text)
    lowered = text.lower()

    if is_header:
        return RecordKind.HEADER, ""

    # Precedence is deliberate. Explicit balance/forward/total markers win over
    # the generic "looks like a transaction" rule so that opening, closing,
    # total, grand-total, and forward rows are never absorbed into transactions.
    if _OPENING_RE.search(lowered):
        return RecordKind.OPENING_BALANCE, ""
    if _CLOSING_RE.search(lowered):
        return RecordKind.CLOSING_BALANCE, ""
    # "Debit Balance" / "Credit Balance" are Tally closing-balance lines.
    if _DR_CR_BALANCE_RE.search(lowered):
        return RecordKind.CLOSING_BALANCE, "Dr/Cr balance line treated as closing balance."
    # "Balance c/o", "Balance b/d" etc. (carry markers that are not c/f or b/f).
    if _BALANCE_FORWARD_RE.search(lowered):
        return RecordKind.BALANCE_FORWARD, ""
    # Carried/brought markers (including the plural "Totals c/o" / "Totals b/d").
    if _CARRIED_RE.search(lowered):
        return RecordKind.CARRIED_FORWARD, ""
    if _BROUGHT_RE.search(lowered):
        return RecordKind.BROUGHT_FORWARD, ""
    if _GRAND_TOTAL_RE.search(lowered):
        return RecordKind.GRAND_TOTAL, ""
    if _TOTAL_RE.search(lowered):
        return RecordKind.TOTAL, ""

    # Amount-only rows (no date, no type, no narrative) are untitled subtotals
    # emitted by Tally. They are totals, not transactions, but should be flagged.
    alpha = re.sub(r"[^A-Za-z]", "", text)
    if has_amount and not has_date and not has_type and not alpha:
        return RecordKind.TOTAL, "Untitled amount-only row treated as subtotal/total."

    if has_amount and (has_date or has_type):
        return RecordKind.TRANSACTION, ""

    if has_amount:
        return RecordKind.TRANSACTION, "Transaction lacks a date or type marker."

    if not text:
        return RecordKind.UNKNOWN, "Empty row."

    return RecordKind.UNKNOWN, "Row has no amount and no recognised marker."
