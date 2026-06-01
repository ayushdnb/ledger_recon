"""Optional, token-controlled AI grouping of unknown transaction labels.

Only runs when ``AI_FORMALIZATION_MODE=group_unknown_labels``. The deterministic
classifier (:mod:`src.formalization.type_classification`) always runs first; this
module is a fallback that looks ONLY at rows the deterministic classifier could
not place (``type_label == "Unknown"``).

Discipline (mirrors the failed-row repair module):

- never sends whole ledgers; only minimal, deduplicated context for the distinct
  unknown raw-type values (capped count, capped char/token budget),
- caches responses by content hash,
- logs a secret-free audit line (purpose, stage, counts, est tokens, output cap),
- the model may only pick one of the predefined canonical labels
  (``config/type_labels.json``) or return the sentinel ``UnknownNeedsReview``; it
  can NEVER invent a new canonical label,
- a suggestion is applied to ``type_label`` only when it is a predefined label
  above the acceptance threshold; ``raw_type`` and monetary fields are never
  touched.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.config import FORMALIZATION_AI_GROUP, estimate_tokens, settings
from src.formalization.ledger_models import LedgerRow, RecordKind
from src.standardization.type_labeler import UNKNOWN_TYPE, load_type_labels

logger = logging.getLogger("formalization.ai_label_grouping")

# Sentinel the model must return when no predefined label is justified.
UNKNOWN_NEEDS_REVIEW = "UnknownNeedsReview"

# Acceptance threshold for applying an AI label suggestion (0-100).
_ACCEPT_MIN_CONFIDENCE = 70.0

# Compact context caps.
_PARTICULARS_CAP = 160
_RAW_TEXT_CAP = 200
_SAMPLE_ROWS_PER_GROUP = 2


@dataclass
class AILabelGroupingResult:
    applied_count: int = 0
    rejected_count: int = 0
    group_count: int = 0
    used_ai: bool = False
    cache_hit: bool = False
    cache_miss: bool = False
    warnings: list[str] = field(default_factory=list)


def is_label_grouping_mode_active() -> bool:
    return settings.ai_formalization_mode == FORMALIZATION_AI_GROUP


def predefined_labels() -> list[str]:
    """Predefined canonical labels (config is the only source of truth)."""
    return list(load_type_labels().keys())


def select_unknown_rows(rows: list[LedgerRow]) -> list[LedgerRow]:
    """Return transaction rows the deterministic classifier left as Unknown."""
    return [
        r
        for r in rows
        if r.record_kind == RecordKind.TRANSACTION and r.type_label == UNKNOWN_TYPE
    ]


def _group_unknown_rows(rows: list[LedgerRow]) -> dict[str, list[LedgerRow]]:
    """Group unknown rows by their (stripped) raw type string."""
    groups: dict[str, list[LedgerRow]] = {}
    for row in rows:
        key = (row.raw_type or "").strip()
        groups.setdefault(key, []).append(row)
    return groups


def build_grouping_evidence(
    groups: dict[str, list[LedgerRow]], labels: list[str]
) -> dict:
    """Assemble compact, deduplicated evidence for one bounded AI request."""
    group_payloads = []
    for raw_type, rows in groups.items():
        samples = []
        for row in rows[:_SAMPLE_ROWS_PER_GROUP]:
            samples.append(
                {
                    "ledger_role": row.ledger_role,
                    "page_number": row.page_number,
                    "particulars": (row.particulars or "")[:_PARTICULARS_CAP],
                    "voucher_no": row.voucher_no,
                    "reference_no": row.reference_no,
                    "debit": row.debit_source,
                    "credit": row.credit_source,
                    "raw_text": (row.raw_text or "")[:_RAW_TEXT_CAP],
                }
            )
        group_payloads.append(
            {
                "raw_type": raw_type,
                "affected_row_count": len(rows),
                "samples": samples,
            }
        )
    return {
        "task": "group_unknown_transaction_labels",
        "predefined_labels": labels,
        "unknown_groups": group_payloads,
        "rules": [
            "Choose exactly one predefined label only if the context clearly "
            "justifies it.",
            f"If no predefined label is justified, return '{UNKNOWN_NEEDS_REVIEW}'.",
            "Never invent a new label. Never modify amounts or references.",
        ],
        "expected_response_schema": {
            "groups": [
                {
                    "raw_type": "string",
                    "suggested_label": f"one predefined label or '{UNKNOWN_NEEDS_REVIEW}'",
                    "confidence": "number 0-100",
                    "reason": "string",
                }
            ]
        },
    }


def _cache_key(payload: dict) -> str:
    basis = "labelgroup|" + json.dumps(payload, sort_keys=True, ensure_ascii=False)
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
        logger.warning("Ignoring unreadable label-grouping AI cache: %s", path.name)
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
            f"Label-grouping AI payload {len(text)} chars exceeds cap {cap}. "
            "Reduce AI_MAX_FAILED_ROWS_PER_REQUEST (group cap)."
        )
    return text


def _request_ai_grouping(
    payload: dict, cache_key: str
) -> tuple[Optional[dict], AILabelGroupingResult]:
    """Call AI or read cache. Returns parsed response and usage metadata."""
    meta = AILabelGroupingResult()

    cached = _read_cache(cache_key)
    if cached is not None:
        logger.info("Using cached AI label grouping (key=%s).", cache_key[:12])
        meta.used_ai = True
        meta.cache_hit = True
        return cached, meta

    if not is_label_grouping_mode_active():
        return None, meta

    settings.ensure_formalization_ai_allowed()
    payload_text = _enforce_payload_budget(payload)
    logger.info(
        "AI audit: purpose=group_unknown_labels, stage=formalization, groups=%s, "
        "payload_chars=%s, est_input_tokens=%s, output_token_cap=%s, cache=miss, "
        "key=%s.",
        len(payload.get("unknown_groups", [])),
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
                "You group unclassified ledger transaction types into a fixed set "
                "of predefined canonical labels. Return JSON: {\"groups\": "
                "[{\"raw_type\", \"suggested_label\", \"confidence\" (0-100), "
                "\"reason\"}]}. Only use a label from predefined_labels, or "
                f"'{UNKNOWN_NEEDS_REVIEW}' when unsure. Never invent labels. Never "
                "modify or infer amounts, references, or dates."
            ),
        },
        {"role": "user", "content": payload_text},
    ]

    try:
        client = build_ai_client(settings)
        raw = client.complete_chat(messages, request_json_object=True)
        parsed = json.loads(raw)
    except (AIClientError, json.JSONDecodeError, RuntimeError) as exc:
        logger.warning(
            "AI label grouping failed (%s); keeping deterministic Unknown rows.",
            type(exc).__name__,
        )
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


def _validate_suggestion(
    suggestion: dict, allowed: set[str]
) -> tuple[str, float, str]:
    """Return (label_or_sentinel, confidence, reason) from one AI suggestion."""
    label = str(suggestion.get("suggested_label", "")).strip()
    try:
        confidence = float(suggestion.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(100.0, confidence))
    reason = str(suggestion.get("reason", "")).strip()

    if label in allowed and confidence >= _ACCEPT_MIN_CONFIDENCE:
        return label, confidence, reason
    # Anything not a confidently-justified predefined label stays for review.
    return UNKNOWN_NEEDS_REVIEW, confidence, reason or "No predefined label justified."


def _apply_suggestion(row: LedgerRow, label: str, confidence: float, reason: str) -> None:
    """Apply an accepted predefined label; never touches raw_type or amounts."""
    row.ai_label_suggested = label
    row.ai_label_confidence = confidence
    row.ai_label_reason = reason
    row.ai_label_applied = True
    row.ai_label_rejected = False
    row.type_label = label
    # Clear the "unmatched type" review reason that the deterministic step added,
    # but preserve any other review reasons (amount/date/layout).
    parts = [
        p.strip()
        for p in (row.review_reason or "").split(";")
        if p.strip() and "unmatched" not in p.lower() and "could not be classified" not in p.lower()
    ]
    row.review_reason = "; ".join(parts)
    row.review_flag = bool(parts)


def _keep_for_review(row: LedgerRow, confidence: float, reason: str) -> None:
    """Record an AI suggestion that was NOT applied; row stays Unknown + review."""
    row.ai_label_suggested = UNKNOWN_NEEDS_REVIEW
    row.ai_label_confidence = confidence
    row.ai_label_reason = reason
    row.ai_label_applied = False
    row.ai_label_rejected = True
    row.review_flag = True


def group_unknown_labels_batch(rows: list[LedgerRow]) -> AILabelGroupingResult:
    """Group a bounded batch of Unknown transaction rows via AI (if active)."""
    result = AILabelGroupingResult()
    if not is_label_grouping_mode_active():
        return result

    unknown_rows = select_unknown_rows(rows)
    if not unknown_rows:
        return result

    groups = _group_unknown_rows(unknown_rows)
    # Cap the number of distinct groups per request (reuse the failed-row cap).
    cap = settings.ai_max_failed_rows_per_request
    capped_keys = list(groups.keys())[:cap]
    capped_groups = {k: groups[k] for k in capped_keys}
    result.group_count = len(capped_groups)

    labels = predefined_labels()
    payload = build_grouping_evidence(capped_groups, labels)
    cache_key = _cache_key(payload)
    parsed, meta = _request_ai_grouping(payload, cache_key)
    result.used_ai = meta.used_ai
    result.cache_hit = meta.cache_hit
    result.cache_miss = meta.cache_miss
    result.warnings.extend(meta.warnings)

    if parsed is None:
        return result

    groups_raw = parsed.get("groups", [])
    if isinstance(groups_raw, dict):
        groups_raw = [groups_raw]
    if not isinstance(groups_raw, list):
        result.warnings.append("AI groups payload was not a list")
        return result

    allowed = set(labels)
    by_raw_type = {
        str(g.get("raw_type", "")).strip(): g for g in groups_raw if isinstance(g, dict)
    }

    for raw_type, group_rows in capped_groups.items():
        suggestion = by_raw_type.get(raw_type)
        if suggestion is None:
            continue
        label, confidence, reason = _validate_suggestion(suggestion, allowed)
        for row in group_rows:
            if label == UNKNOWN_NEEDS_REVIEW:
                _keep_for_review(row, confidence, reason)
                result.rejected_count += 1
            else:
                _apply_suggestion(row, label, confidence, reason)
                result.applied_count += 1

    return result
