"""Tests for OpenAI-compatible client error handling."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.config import Settings
from src.providers.ai_client import AIClientError
from src.providers.openai_compatible_client import OpenAICompatibleClient


def _settings(**overrides) -> Settings:
    base = {
        "app_env": "test",
        "project_root": ".",
        "default_pair_id": "pair_test",
        "input_pair_root": "data/02_work_pairs",
        "raw_context_output_dir": "data/04_outputs/raw_context_workbooks",
        "project_context_dump_dir": "data/04_outputs/project_context_dumps",
        "log_level": "INFO",
        "ai_enabled": True,
        "ai_provider": "hosted_openai_compatible",
        "ai_api_base_url": "https://example.invalid/v1",
        "ai_api_key": "test-key-not-real",
        "ai_model_name": "test-model",
        "ai_temperature": 0.0,
        "ai_timeout_seconds": 30,
        "ai_max_retries": 0,
        "ai_max_input_chars_per_request": 12000,
        "ai_max_input_tokens_per_request": 3000,
        "ai_max_output_tokens": 1024,
        "ai_data_approval": "hosted_approved",
        "type_labels_path": "config/type_labels.json",
        "standardized_output_dir": "data/04_outputs/standardized_workbooks",
        "reference_profile_output_dir": "data/04_outputs/reference_profiles",
        "formalized_output_dir": "data/04_outputs/formalized_workbooks",
        "ai_formalization_mode": "off",
        "ai_max_layout_sample_lines": 40,
        "ai_max_failed_rows_per_request": 25,
        "ai_max_ai_pages_per_ledger": 3,
        "ai_formalization_cache_enabled": True,
        "ai_formalization_cache_dir": "data/04_outputs/formalization_ai_cache",
    }
    base.update(overrides)
    from pydantic import SecretStr

    base["ai_api_key"] = SecretStr(base["ai_api_key"])
    return Settings(**base)


def _mock_response(status_code: int) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    return response


@pytest.mark.parametrize(
    ("status_code", "expected_fragment"),
    [
        (413, "AI request too large"),
        (401, "authentication issue"),
        (403, "authentication issue"),
        (429, "rate limit"),
    ],
)
def test_client_errors_are_clear_and_secret_free(
    status_code: int, expected_fragment: str
) -> None:
    client = OpenAICompatibleClient(_settings())
    with patch("src.providers.openai_compatible_client.requests.post") as mock_post:
        mock_post.return_value = _mock_response(status_code)
        with pytest.raises(AIClientError) as exc_info:
            client.complete_chat([{"role": "user", "content": "hi"}])
    message = str(exc_info.value).lower()
    assert expected_fragment.lower() in message
    assert "test-key-not-real" not in message
    assert "authorization" not in message


def test_413_does_not_retry() -> None:
    client = OpenAICompatibleClient(_settings(ai_max_retries=3))
    with patch("src.providers.openai_compatible_client.requests.post") as mock_post:
        mock_post.return_value = _mock_response(413)
        with pytest.raises(AIClientError, match="too large"):
            client.complete_chat([{"role": "user", "content": "hi"}])
        assert mock_post.call_count == 1


def test_500_retries_then_fails() -> None:
    client = OpenAICompatibleClient(_settings(ai_max_retries=2))
    with patch("src.providers.openai_compatible_client.requests.post") as mock_post:
        mock_post.return_value = _mock_response(500)
        with pytest.raises(AIClientError):
            client.complete_chat([{"role": "user", "content": "hi"}])
        assert mock_post.call_count == 3


def _ok_response() -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": "{}"}}]
    }
    return response


def test_payload_includes_output_token_cap() -> None:
    client = OpenAICompatibleClient(_settings(ai_max_output_tokens=256))
    with patch("src.providers.openai_compatible_client.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        client.complete_chat([{"role": "user", "content": "hi"}])
        payload = mock_post.call_args.kwargs["json"]
    assert payload["max_tokens"] == 256


def test_oversized_input_is_rejected_without_network_call() -> None:
    # Tiny token budget so even a small message is over budget.
    client = OpenAICompatibleClient(_settings(ai_max_input_tokens_per_request=1))
    with patch("src.providers.openai_compatible_client.requests.post") as mock_post:
        with pytest.raises(AIClientError, match="tokens"):
            client.complete_chat(
                [{"role": "user", "content": "this content is too large for the budget"}]
            )
        assert mock_post.call_count == 0


def test_api_key_never_appears_in_payload_or_logs(caplog) -> None:
    secret = "test-key-not-real"
    client = OpenAICompatibleClient(_settings(ai_api_key=secret))
    with caplog.at_level("INFO"):
        with patch("src.providers.openai_compatible_client.requests.post") as mock_post:
            mock_post.return_value = _ok_response()
            client.complete_chat([{"role": "user", "content": "hi"}])
            payload = mock_post.call_args.kwargs["json"]
            headers = mock_post.call_args.kwargs["headers"]
    # The key lives only in the Authorization header, never in the JSON payload.
    assert secret not in str(payload)
    assert secret in headers["Authorization"]
    # And it must never be logged.
    assert secret not in caplog.text
