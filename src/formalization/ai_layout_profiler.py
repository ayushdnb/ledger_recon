"""Optional, token-controlled AI layout profiling / failed-row repair.

The deterministic extractor is always the bulk extractor. This module is an
*optional* ambiguity resolver that is OFF by default. Hard guarantees:

- The AI never receives whole ledgers. It only ever sees a tiny, bounded
  *evidence* payload: detected headers, a few representative sample lines, and
  (in repair mode) only the specific failed rows - all capped by config.
- Nothing is called unless ``AI_FORMALIZATION_MODE`` is active AND the global AI
  safety gate passes (``settings.ensure_formalization_ai_allowed``).
- Responses are cached deterministically by content hash so repeated runs never
  re-spend tokens.
- API keys are never logged; only payload sizes and cache keys are logged.

This makes it structurally impossible to reproduce the old "send 300+ raw
context chunks" behaviour from the formalization stage.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.config import FORMALIZATION_AI_LAYOUT, settings
from src.formalization.ledger_models import LayoutProfile

logger = logging.getLogger("formalization.ai_layout_profiler")


@dataclass
class AIProfileResult:
    profile: Optional[LayoutProfile]
    used_ai: bool = False
    cache_hit: bool = False
    cache_miss: bool = False


def build_layout_evidence(layout_stats: dict, max_sample_lines: int) -> dict:
    """Assemble the compact evidence payload sent to the AI (never full ledger).

    Only structural hints are included, capped to ``max_sample_lines`` lines.
    """
    sample = list(layout_stats.get("sample_lines", []))[:max_sample_lines]
    return {
        "ledger_role": layout_stats.get("ledger_role", ""),
        "page_count": layout_stats.get("page_count", 0),
        "detected_columns": layout_stats.get("detected_columns", []),
        "header_texts": layout_stats.get("header_texts", [])[:4],
        "sample_lines": sample,
        "sample_line_count": len(sample),
    }


def _cache_key(kind: str, payload: dict) -> str:
    basis = kind + "|" + json.dumps(payload, sort_keys=True, ensure_ascii=False)
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
        logger.warning("Ignoring unreadable formalization AI cache: %s", path.name)
        return None


def _write_cache(cache_key: str, value: dict) -> None:
    if not settings.ai_formalization_cache_enabled:
        return
    path = _cache_path(cache_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _enforce_payload_budget(payload: dict) -> str:
    """Serialize the payload and assert it is within the per-request char cap."""
    text = json.dumps(payload, ensure_ascii=False)
    cap = settings.ai_max_input_chars_per_request
    if len(text) > cap:
        raise RuntimeError(
            f"Formalization AI payload {len(text)} chars exceeds cap {cap}. "
            "Refusing to send; reduce AI_MAX_LAYOUT_SAMPLE_LINES."
        )
    return text


def profile_layout_result(layout_stats: dict) -> AIProfileResult:
    """Return an AI-derived :class:`LayoutProfile`, or ``None`` when AI is off.

    With ``AI_FORMALIZATION_MODE=off`` (default) this returns ``None`` immediately
    and never touches the network.
    """
    if settings.ai_formalization_mode != FORMALIZATION_AI_LAYOUT:
        return AIProfileResult(profile=None)

    payload = build_layout_evidence(layout_stats, settings.ai_max_layout_sample_lines)
    cache_key = _cache_key("layout", payload)

    cached = _read_cache(cache_key)
    if cached is not None:
        logger.info("Using cached AI layout profile (key=%s).", cache_key[:12])
        return AIProfileResult(
            profile=_profile_from_dict(cached, source="ai_cache"),
            used_ai=True,
            cache_hit=True,
        )

    # Gate the network call. Raises clearly if not approved.
    settings.ensure_formalization_ai_allowed()

    payload_text = _enforce_payload_budget(payload)
    logger.info(
        "Requesting AI layout profile (role=%s, payload_chars=%s, key=%s).",
        payload.get("ledger_role", ""),
        len(payload_text),
        cache_key[:12],
    )

    # Imported lazily so the deterministic path has no hard AI dependency.
    from src.providers.ai_client import AIClientError, build_ai_client

    messages = [
        {
            "role": "system",
            "content": (
                "You are a ledger LAYOUT profiler. Given a tiny sample of lines "
                "and detected headers, return ONLY a compact JSON layout profile "
                "describing how to read columns. Never request or output ledger "
                "rows or totals. Keys: layout_type, date_carry_forward, columns, "
                "ignore_row_patterns, special_row_patterns."
            ),
        },
        {"role": "user", "content": payload_text},
    ]

    try:
        client = build_ai_client(settings)
        raw = client.complete_chat(messages, request_json_object=True)
        parsed = json.loads(raw)
    except (AIClientError, json.JSONDecodeError, RuntimeError) as exc:
        logger.warning("AI layout profiling failed (%s); using deterministic layout.", type(exc).__name__)
        return AIProfileResult(profile=None, used_ai=True, cache_miss=True)

    _write_cache(cache_key, parsed)
    return AIProfileResult(
        profile=_profile_from_dict(parsed, source="ai_layout"),
        used_ai=True,
        cache_miss=True,
    )


def profile_layout(layout_stats: dict) -> Optional[LayoutProfile]:
    """Backward-compatible wrapper returning only the profile (or None)."""
    return profile_layout_result(layout_stats).profile


def _profile_from_dict(data: dict, source: str) -> LayoutProfile:
    columns_raw = data.get("columns", {})
    if not isinstance(columns_raw, dict):
        logger.warning(
            "AI layout profile returned non-dict columns (%s); coercing to empty.",
            type(columns_raw).__name__,
        )
        columns_raw = {}

    ignore_raw = data.get("ignore_row_patterns", [])
    if isinstance(ignore_raw, str):
        ignore_patterns = [ignore_raw]
    elif isinstance(ignore_raw, list):
        ignore_patterns = [str(v) for v in ignore_raw if v is not None]
    else:
        logger.warning(
            "AI layout profile returned non-list ignore_row_patterns (%s); coercing.",
            type(ignore_raw).__name__,
        )
        ignore_patterns = []

    special_raw = data.get("special_row_patterns", {})
    if isinstance(special_raw, dict):
        special_patterns = {
            str(k): [str(v) for v in vals] if isinstance(vals, list) else [str(vals)]
            for k, vals in special_raw.items()
        }
    elif isinstance(special_raw, list):
        # Some providers return a flat list; keep it auditable under one key.
        special_patterns = {"generic": [str(v) for v in special_raw if v is not None]}
    else:
        logger.warning(
            "AI layout profile returned non-dict special_row_patterns (%s); coercing.",
            type(special_raw).__name__,
        )
        special_patterns = {}

    return LayoutProfile(
        layout_type=str(data.get("layout_type", "")),
        date_carry_forward=bool(data.get("date_carry_forward", False)),
        columns={str(k): str(v) for k, v in columns_raw.items()},
        ignore_row_patterns=ignore_patterns,
        special_row_patterns=special_patterns,
        source=source,
    )
