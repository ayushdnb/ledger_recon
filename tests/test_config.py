"""Tests for configuration loading and the AI safety gate."""

from __future__ import annotations

import importlib

import pytest


def _fresh_config():
    import src.config as config

    return importlib.reload(config)


def test_config_loads_without_env(monkeypatch) -> None:
    # Override .env values re-applied by load_dotenv on reload.
    monkeypatch.setenv("AI_ENABLED", "false")
    monkeypatch.setenv("AI_PROVIDER", "hosted_openai_compatible")
    monkeypatch.setenv("AI_DATA_APPROVAL", "local_only")
    monkeypatch.delenv("AI_API_BASE_URL", raising=False)
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("AI_MODEL_NAME", raising=False)
    config = _fresh_config()
    settings = config.settings
    assert settings.ai_enabled is False
    assert settings.ai_provider == "hosted_openai_compatible"
    assert settings.ai_data_approval == "local_only"
    assert settings.ai_temperature == 0.0
    assert settings.type_labels_path.name == "type_labels.json"


def test_ai_gate_blocks_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("AI_ENABLED", "false")
    config = _fresh_config()
    with pytest.raises(RuntimeError, match="AI is disabled"):
        config.settings.ensure_ai_call_allowed()


def test_ai_gate_blocks_hosted_without_approval(monkeypatch) -> None:
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("AI_PROVIDER", "hosted_openai_compatible")
    monkeypatch.setenv("AI_DATA_APPROVAL", "local_only")
    monkeypatch.setenv("AI_API_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("AI_MODEL_NAME", "test-model")
    config = _fresh_config()
    with pytest.raises(RuntimeError, match="hosted_approved"):
        config.settings.ensure_ai_call_allowed()


def test_ai_gate_allows_when_approved(monkeypatch) -> None:
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("AI_PROVIDER", "hosted_openai_compatible")
    monkeypatch.setenv("AI_DATA_APPROVAL", "hosted_approved")
    monkeypatch.setenv("AI_API_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("AI_MODEL_NAME", "test-model")
    config = _fresh_config()
    # Should not raise.
    config.settings.ensure_ai_call_allowed()


def test_reconciliation_ai_defaults_off(monkeypatch) -> None:
    monkeypatch.setenv("AI_RECONCILIATION_ENABLED", "false")
    monkeypatch.setenv("AI_RECONCILIATION_MODE", "off")
    config = _fresh_config()
    assert config.settings.ai_reconciliation_active() is False


def test_reconciliation_ai_gate_requires_reconciliation_mode(monkeypatch) -> None:
    monkeypatch.setenv("AI_RECONCILIATION_ENABLED", "false")
    monkeypatch.setenv("AI_RECONCILIATION_MODE", "off")
    config = _fresh_config()
    with pytest.raises(RuntimeError, match="Reconciliation AI is off"):
        config.settings.ensure_reconciliation_ai_allowed()


def test_api_key_is_not_exposed_in_repr(monkeypatch) -> None:
    monkeypatch.setenv("AI_API_KEY", "super-secret-key-value")
    config = _fresh_config()
    rendered = repr(config.settings) + str(config.settings)
    assert "super-secret-key-value" not in rendered
    # The secret is still retrievable explicitly when needed.
    assert config.settings.ai_api_key.get_secret_value() == "super-secret-key-value"


def test_reconciliation_label_tolerance_override_loads_from_env(monkeypatch) -> None:
    monkeypatch.setenv(
        "RECON_LABEL_TOLERANCE_OVERRIDES_JSON",
        '{"Payment":{"date_tolerance_days":21,"rounding_tolerance":0.03}}',
    )
    config = _fresh_config()
    payment = config.settings.reconciliation_tolerance_policy().for_label("Payment")
    assert payment.date_tolerance_days == 21
    assert payment.rounding_tolerance == 0.03


def test_reconciliation_label_tolerance_override_rejects_unknown_field(monkeypatch) -> None:
    monkeypatch.setenv(
        "RECON_LABEL_TOLERANCE_OVERRIDES_JSON",
        '{"Payment":{"invented_tolerance":12}}',
    )
    config = _fresh_config()
    with pytest.raises(ValueError, match="Unsupported reconciliation tolerance override"):
        config.settings.reconciliation_tolerance_policy()


def teardown_module(module) -> None:  # noqa: ARG001 - reset shared singleton
    import src.config

    importlib.reload(src.config)
