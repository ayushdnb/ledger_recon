"""Optional, token-controlled AI repair for failed formalized rows.

Only runs when ``AI_FORMALIZATION_MODE=repair_failed_rows``. Sends a compact
batch of failed-row evidence (never full ledgers) and applies validated repairs
to repairable fields only (dates for this experiment).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.config import FORMALIZATION_AI_REPAIR, estimate_tokens, settings
from src.formalization.ledger_cleaning import parse_date, parse_date_with_confidence
from src.formalization.ledger_models import LedgerRow, RecordKind, ROLE_PARTY

logger = logging.getLogger("formalization.ai_failed_row_repair")

_RAW_TEXT_CAP = 400
_FORBIDDEN_REPAIR_FIELDS = frozenset(
    {
        "debit_source",
        "credit_source",
        "balance_source",
        "voucher_no",
        "reference_no",
        "particulars",
        "account",
        "type_label",
        "debit_org_perspective",
        "credit_org_perspective",
        "amount_org_perspective",
    }
)

_DATE_REVIEW_PATTERNS = (
    re.compile(r"low date confidence", re.I),
    re.compile(r"unparseable date", re.I),
    re.compile(r"missing date", re.I),
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class AIRepairBatchResult:
    applied_count: int = 0
    rejected_count: int = 0
    used_ai: bool = False
    cache_hit: bool = False
    cache_miss: bool = False
    warnings: list[str] = field(default_factory=list)


def is_repair_mode_active() -> bool:
    return settings.ai_formalization_mode == FORMALIZATION_AI_REPAIR


def is_date_ambiguous_row(row: LedgerRow) -> bool:
    """True when a transaction row has low-confidence or unresolved date parsing."""
    if row.record_kind != RecordKind.TRANSACTION:
        return False
    if row.date_parse_confidence < 70.0:
        return True
    reason = (row.review_reason or "").lower()
    if "low date confidence" in reason:
        return True
    if row.raw_date and not row.normalized_date:
        return True
    return False


def select_failed_rows(
    rows: list[LedgerRow],
    *,
    ledger_role: str,
    max_rows: int,
) -> list[LedgerRow]:
    """Return capped failed rows eligible for repair (party ledger, date ambiguity)."""
    if ledger_role != ROLE_PARTY:
        return []
    candidates = [r for r in rows if is_date_ambiguous_row(r)]
    return candidates[:max_rows]


def _neighbor_dates(rows: list[LedgerRow], target: LedgerRow) -> dict[str, str]:
    """Return immediate neighbor dates on the same page (compact context only)."""
    same_page = [r for r in rows if r.page_number == target.page_number and r.normalized_date]
    neighbors: dict[str, str] = {}
    for r in same_page:
        if r.source_row_number == target.source_row_number - 1:
            neighbors["prev"] = r.normalized_date
        elif r.source_row_number == target.source_row_number + 1:
            neighbors["next"] = r.normalized_date
    return neighbors


def build_repair_evidence(
    failed_rows: list[LedgerRow],
    all_rows: list[LedgerRow],
    header_texts: list[str],
) -> dict:
    """Assemble compact failed-row evidence for one bounded AI request."""
    row_payloads = []
    for row in failed_rows:
        row_payloads.append(
            {
                "row_id": row.row_id,
                "page_number": row.page_number,
                "source_row_number": row.source_row_number,
                "raw_text": (row.raw_text or "")[:_RAW_TEXT_CAP],
                "raw_date": row.raw_date,
                "normalized_date": row.normalized_date,
                "date_parse_confidence": row.date_parse_confidence,
                "neighbor_dates": _neighbor_dates(all_rows, row),
            }
        )
    return {
        "ledger_role": failed_rows[0].ledger_role if failed_rows else "",
        "repairable_fields": ["normalized_date", "date_parse_confidence", "explanation"],
        "header_texts": list(header_texts)[:4],
        "failed_rows": row_payloads,
        "expected_response_schema": {
            "repairs": [
                {
                    "row_id": "string",
                    "normalized_date": "YYYY-MM-DD",
                    "date_parse_confidence": "number 0-100",
                    "explanation": "string",
                }
            ]
        },
    }


def _cache_key(payload: dict) -> str:
    basis = "repair|" + json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _cache_path(cache_key: str) -> Path:
    cache_dir = settings.resolved(settings.ai_formalization_cache_dir)
    return cache_dir / f"{cache_key}.json"


def _read_cache(cache_key: str) -> Optional[dict]:
    if not settings.ai_formalization_cache_enabled:
        return None
    path = _cache_path(cache_key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring unreadable repair AI cache: %s", path.name)
        return None


def _write_cache(cache_key: str, value: dict) -> None:
    if not settings.ai_formalization_cache_enabled:
        return
    path = _cache_path(cache_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _enforce_payload_budget(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    cap = settings.ai_max_input_chars_per_request
    if len(text) > cap:
        raise RuntimeError(
            f"Repair AI payload {len(text)} chars exceeds cap {cap}. "
            "Reduce AI_MAX_FAILED_ROWS_PER_REQUEST."
        )
    return text


def _normalize_iso_date(value: str) -> Optional[str]:
    """Accept ISO dates or ledger-style dates; return ISO or None."""
    text = value.strip()
    if _ISO_DATE_RE.match(text):
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return text
        except ValueError:
            return None
    return parse_date(text)


def _validate_repair_candidate(repair: dict) -> tuple[bool, str, str, float]:
    """Validate one AI repair proposal. Returns (ok, iso_date, explanation, confidence)."""
    row_id = str(repair.get("row_id", "")).strip()
    if not row_id:
        return False, "", "missing row_id", 0.0

    proposed_date = str(repair.get("normalized_date", "")).strip()
    if not proposed_date:
        return False, "", "missing normalized_date", 0.0

    iso_date = _normalize_iso_date(proposed_date)
    if not iso_date:
        return False, "", f"unparseable normalized_date '{proposed_date}'", 0.0

    try:
        confidence = float(repair.get("date_parse_confidence", 0.0))
    except (TypeError, ValueError):
        return False, "", "invalid date_parse_confidence", 0.0

    confidence = max(0.0, min(100.0, confidence))
    if confidence < 70.0:
        return False, "", f"date_parse_confidence {confidence} below acceptance threshold", 0.0

    explanation = str(repair.get("explanation", "")).strip()
    return True, iso_date, explanation, confidence


def _strip_date_review_reasons(reason: str) -> list[str]:
    parts = [p.strip() for p in (reason or "").split(";") if p.strip()]
    kept: list[str] = []
    for part in parts:
        if any(p.search(part) for p in _DATE_REVIEW_PATTERNS):
            continue
        kept.append(part)
    return kept


def _apply_validated_repair(row: LedgerRow, iso_date: str, confidence: float, explanation: str) -> None:
    """Apply an accepted date repair while preserving original source evidence."""
    row.ai_repair_applied = True
    row.ai_repair_normalized_date = iso_date
    row.ai_repair_date_confidence = confidence
    row.ai_repair_explanation = explanation
    row.ai_repair_reason = "AI date repair accepted after validation"

    row.date = iso_date
    row.normalized_date = iso_date
    row.date_parse_confidence = confidence

    remaining = _strip_date_review_reasons(row.review_reason)
    row.review_reason = "; ".join(remaining)
    row.review_flag = bool(remaining)
    if row.review_flag:
        row.ai_repair_reason = "AI date repair applied; other review reasons remain"
    else:
        row.ai_repair_reason = "AI date repair applied; row cleared from review"


def _reject_repair(row: LedgerRow, reason: str) -> None:
    row.ai_repair_rejected = True
    row.ai_repair_rejection_reason = reason


def _request_ai_repairs(payload: dict, cache_key: str) -> tuple[Optional[dict], AIRepairBatchResult]:
    """Call AI or read cache. Returns parsed response and usage metadata."""
    meta = AIRepairBatchResult()

    cached = _read_cache(cache_key)
    if cached is not None:
        logger.info("Using cached AI row repair (key=%s).", cache_key[:12])
        meta.used_ai = True
        meta.cache_hit = True
        return cached, meta

    if not is_repair_mode_active():
        return None, meta

    settings.ensure_formalization_ai_allowed()
    payload_text = _enforce_payload_budget(payload)
    logger.info(
        "AI audit: purpose=repair_failed_rows, stage=formalization, role=%s, "
        "rows=%s, payload_chars=%s, est_input_tokens=%s, output_token_cap=%s, "
        "cache=miss, key=%s.",
        payload.get("ledger_role", ""),
        len(payload.get("failed_rows", [])),
        len(payload_text),
        estimate_tokens(payload_text),
        settings.ai_max_output_tokens,
        cache_key[:12],
    )

    from src.providers.ai_client import AIClientError, build_ai_client

    messages = [
        {
            "role": "system",
            "content": (
                "You repair ONLY failed ledger row dates from compact evidence. "
                "Return JSON: {\"repairs\": [{\"row_id\", \"normalized_date\" (ISO), "
                "\"date_parse_confidence\" (0-100), \"explanation\"}]}. "
                "Do NOT modify amounts, references, vouchers, particulars, or accounts. "
                "Do NOT invent financial data. Only propose dates supported by raw_text."
            ),
        },
        {"role": "user", "content": payload_text},
    ]

    try:
        client = build_ai_client(settings)
        raw = client.complete_chat(messages, request_json_object=True)
        parsed = json.loads(raw)
    except (AIClientError, json.JSONDecodeError, RuntimeError) as exc:
        logger.warning("AI row repair failed (%s); keeping deterministic rows.", type(exc).__name__)
        meta.used_ai = True
        meta.cache_miss = True
        return None, meta

    if not isinstance(parsed, dict):
        meta.used_ai = True
        meta.cache_miss = True
        meta.warnings.append("AI response was not a JSON object")
        return None, meta

    _write_cache(cache_key, parsed)
    meta.used_ai = True
    meta.cache_miss = True
    return parsed, meta


def repair_failed_rows_batch(
    rows: list[LedgerRow],
    *,
    ledger_role: str,
    header_texts: list[str] | None = None,
) -> AIRepairBatchResult:
    """Repair a bounded batch of date-ambiguous party-ledger rows via AI."""
    result = AIRepairBatchResult()
    if not is_repair_mode_active():
        return result
    if ledger_role != ROLE_PARTY:
        return result

    cap = settings.ai_max_failed_rows_per_request
    failed = select_failed_rows(rows, ledger_role=ledger_role, max_rows=cap)
    if not failed:
        return result

    payload = build_repair_evidence(failed, rows, header_texts or [])
    cache_key = _cache_key(payload)
    parsed, meta = _request_ai_repairs(payload, cache_key)
    result.used_ai = meta.used_ai
    result.cache_hit = meta.cache_hit
    result.cache_miss = meta.cache_miss
    result.warnings.extend(meta.warnings)

    if parsed is None:
        for row in failed:
            _reject_repair(row, "AI repair request failed or returned no data")
            result.rejected_count += 1
        return result

    repairs_raw = parsed.get("repairs", [])
    if isinstance(repairs_raw, dict):
        repairs_raw = [repairs_raw]
    if not isinstance(repairs_raw, list):
        result.warnings.append("AI repairs payload was not a list")
        for row in failed:
            _reject_repair(row, "invalid AI repairs structure")
            result.rejected_count += 1
        return result

    by_id = {str(r.get("row_id", "")).strip(): r for r in repairs_raw if isinstance(r, dict)}
    for forbidden in _FORBIDDEN_REPAIR_FIELDS:
        for repair in by_id.values():
            if forbidden in repair:
                result.warnings.append(f"ignored forbidden field '{forbidden}' in AI response")

    failed_ids = {r.row_id for r in failed}
    for row in failed:
        repair = by_id.get(row.row_id)
        if repair is None:
            _reject_repair(row, "no AI repair returned for row_id")
            result.rejected_count += 1
            continue

        ok, iso_date, explanation_or_reason, confidence = _validate_repair_candidate(repair)
        if not ok:
            _reject_repair(row, explanation_or_reason)
            result.rejected_count += 1
            continue

        # Cross-check proposed date against raw_text when raw_date present.
        if row.raw_date:
            parsed_from_raw, raw_conf = parse_date_with_confidence(row.raw_date)
            if parsed_from_raw and parsed_from_raw != iso_date and raw_conf >= 70.0:
                _reject_repair(row, "AI date conflicts with high-confidence raw_date parse")
                result.rejected_count += 1
                continue

        _apply_validated_repair(row, iso_date, confidence, explanation_or_reason)
        result.applied_count += 1

    # Rows in batch without proposals are already rejected above.
    _ = failed_ids
    return result
