"""Tiny, bounded AI availability probe for manual diagnostics.

Safety contract:
- Never sends any financial document, ledger row, amount, or reference.
- Sends a single fixed, content-free prompt asking for strict JSON {"ok": true}.
- Never logs, prints, or returns the API key.
- Never raises: any failure degrades to ``available=False``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from src.config import Settings, settings

logger = logging.getLogger("reconciliation.ai_availability")

# Content-free probe. Contains no ledger data of any kind.
_PROBE_SYSTEM = "You are a connectivity probe. Reply with strict minified JSON only."
_PROBE_USER = 'Return exactly this JSON object and nothing else: {"ok": true}'


@dataclass
class AIAvailability:
    """Result of the bounded availability probe."""

    enabled: bool
    approved: bool
    attempted: bool
    available: bool
    mode: str
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "approved": self.approved,
            "attempted": self.attempted,
            "available": self.available,
            "mode": self.mode,
            "detail": self.detail,
        }


def check_ai_availability(config: Settings | None = None) -> AIAvailability:
    """Probe AI availability without sending any financial data.

    Returns a structured result. The workbook builder does not need this probe;
    arbitration calls are made lazily only when unresolved packets exist.
    """
    config = config or settings
    enabled = bool(config.ai_enabled)
    hosted = config.is_hosted_provider()
    approved = (not hosted) or config.hosted_approved()
    mode = "diagnostic_probe_only"

    if not enabled:
        return AIAvailability(enabled, approved, False, False, mode,
                              "AI disabled (AI_ENABLED=false); deterministic path used.")
    if hosted and not approved:
        return AIAvailability(enabled, approved, False, False, mode,
                              "Hosted AI not approved (AI_DATA_APPROVAL); deterministic path used.")

    try:
        from src.providers.ai_client import build_ai_client

        client = build_ai_client(config)
        raw = client.complete_chat(
            [
                {"role": "system", "content": _PROBE_SYSTEM},
                {"role": "user", "content": _PROBE_USER},
            ],
            request_json_object=True,
        )
        ok = _parse_ok(raw)
        if ok:
            return AIAvailability(enabled, approved, True, True, mode,
                                  "Probe succeeded; AI reachable (assistive use only).")
        return AIAvailability(enabled, approved, True, False, mode,
                              "Probe returned unexpected payload; treated as unavailable.")
    except Exception as exc:  # noqa: BLE001 - never let a probe break the build
        # Report only the exception type; never include secrets or payloads.
        return AIAvailability(enabled, approved, True, False, mode,
                              f"Probe failed ({type(exc).__name__}); deterministic path used.")


def _parse_ok(raw: str) -> bool:
    text = (raw or "").strip()
    # Tolerate code fences or stray prose around the JSON object.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("ok") is True
