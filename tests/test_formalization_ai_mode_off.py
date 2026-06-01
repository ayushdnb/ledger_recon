"""Tests for AI-off behaviour and the no-bulk-context guardrails.

These verify that with the default configuration the formalization stage never
calls a model, and that activating AI still requires the global safety gate.
Also asserts the formalization source never logs the API key.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import src.formalization.ai_layout_profiler as profiler


def _fresh_config():
    import src.config as config

    return importlib.reload(config)


def test_default_mode_is_off(monkeypatch) -> None:
    monkeypatch.delenv("AI_FORMALIZATION_MODE", raising=False)
    config = _fresh_config()
    assert config.settings.ai_formalization_mode == "off"
    assert config.settings.formalization_ai_enabled() is False


def test_ensure_formalization_ai_allowed_raises_when_off(monkeypatch) -> None:
    monkeypatch.setenv("AI_FORMALIZATION_MODE", "off")
    config = _fresh_config()
    with pytest.raises(RuntimeError, match="Formalization AI is off"):
        config.settings.ensure_formalization_ai_allowed()


def test_active_mode_still_requires_global_gate(monkeypatch) -> None:
    # Active formalization mode must NOT bypass the AI enable/approval gate.
    monkeypatch.setenv("AI_FORMALIZATION_MODE", "layout_only")
    monkeypatch.setenv("AI_ENABLED", "false")
    config = _fresh_config()
    with pytest.raises(RuntimeError, match="AI is disabled"):
        config.settings.ensure_formalization_ai_allowed()


def test_active_hosted_mode_requires_approval(monkeypatch) -> None:
    monkeypatch.setenv("AI_FORMALIZATION_MODE", "repair_failed_rows")
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("AI_PROVIDER", "hosted_openai_compatible")
    monkeypatch.setenv("AI_DATA_APPROVAL", "local_only")
    monkeypatch.setenv("AI_API_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("AI_MODEL_NAME", "test-model")
    config = _fresh_config()
    with pytest.raises(RuntimeError, match="hosted_approved"):
        config.settings.ensure_formalization_ai_allowed()


def test_profile_layout_returns_none_when_off(monkeypatch) -> None:
    # With AI off, profiling is a no-op and never touches the network.
    monkeypatch.setattr(profiler.settings, "ai_formalization_mode", "off")
    stats = {
        "ledger_role": "org_ledger",
        "page_count": 1,
        "detected_columns": ["date", "debit", "credit"],
        "header_texts": ["Date Type Debit Credit"],
        "sample_lines": ["22-04-2025 Sale 100.00"],
    }
    assert profiler.profile_layout(stats) is None


def test_layout_evidence_is_bounded() -> None:
    # The evidence payload must never contain the whole ledger: it is capped.
    stats = {
        "ledger_role": "party_ledger",
        "page_count": 9,
        "detected_columns": ["date", "particulars", "debit", "credit"],
        "header_texts": ["h1", "h2", "h3", "h4", "h5"],
        "sample_lines": [f"line {i}" for i in range(500)],
    }
    evidence = profiler.build_layout_evidence(stats, max_sample_lines=10)
    assert evidence["sample_line_count"] == 10
    assert len(evidence["sample_lines"]) == 10
    assert len(evidence["header_texts"]) <= 4


def test_formalization_source_does_not_log_api_key() -> None:
    root = Path(__file__).resolve().parent.parent / "src" / "formalization"
    offenders: list[str] = []
    for py_file in root.rglob("*.py"):
        for line in py_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            lowered = stripped.lower()
            if ("print(" in lowered or "logger." in lowered) and "api_key" in lowered:
                offenders.append(f"{py_file.name}: {stripped}")
    assert not offenders, f"Potential API key logging: {offenders}"


def teardown_module(module) -> None:  # noqa: ARG001 - reset shared singleton
    import src.config

    importlib.reload(src.config)
