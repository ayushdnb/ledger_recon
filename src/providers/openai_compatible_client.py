"""OpenAI-compatible chat-completions client built on ``requests``.

Talks to any service exposing ``POST {base_url}/chat/completions`` with a Bearer
token. No provider SDK is used. The client:

- returns only the raw assistant text content,
- performs timeout and bounded retry handling,
- raises clear errors that never contain the API key or Authorization header.

It deliberately contains no ledger logic and does not interpret response JSON
beyond locating the assistant message text.
"""

from __future__ import annotations

import json
import logging
import time

import requests

from src.config import Settings, estimate_tokens
from src.providers.ai_client import AIClient, AIClientError

logger = logging.getLogger("openai_compatible_client")

# HTTP statuses that may succeed on retry (transient server / rate-limit pressure).
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class OpenAICompatibleClient(AIClient):
    """Minimal OpenAI-compatible chat-completions client."""

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._base_url = config.ai_api_base_url.rstrip("/")
        self._model = config.ai_model_name
        self._temperature = config.ai_temperature
        self._timeout = config.ai_timeout_seconds
        self._max_retries = max(0, config.ai_max_retries)
        self._max_output_tokens = max(1, config.ai_max_output_tokens)
        self._max_input_tokens = max(1, config.ai_max_input_tokens_per_request)
        # Stored privately; never logged or included in error messages.
        self.__api_key = config.ai_api_key.get_secret_value()

    @property
    def _endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        # Built per-request and never logged.
        return {
            "Authorization": f"Bearer {self.__api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _client_error_message(status_code: int) -> str:
        if status_code == 413:
            return (
                "AI request too large. Reduce AI_MAX_INPUT_CHARS_PER_REQUEST "
                "or enable chunking."
            )
        if status_code in (401, 403):
            return (
                f"AI provider returned HTTP {status_code}: API key or "
                "authentication issue. Check AI_API_KEY in .env."
            )
        if status_code == 429:
            return (
                "AI provider returned HTTP 429: rate limit or free-tier quota "
                "exceeded. Wait and retry, or upgrade the provider plan."
            )
        return f"AI provider returned client error status {status_code}."

    def complete_chat(
        self,
        messages: list[dict[str, str]],
        *,
        request_json_object: bool = True,
    ) -> str:
        if not self.__api_key:
            raise AIClientError(
                "AI_API_KEY is empty. Set it in .env before calling the AI provider."
            )

        # Estimate input tokens from the serialized messages and reject oversized
        # requests deterministically (no network call, no retry). This is a hard
        # token budget layered on top of the per-call character caps.
        messages_text = json.dumps(messages, ensure_ascii=False)
        estimated_input_tokens = estimate_tokens(messages_text)
        if estimated_input_tokens > self._max_input_tokens:
            raise AIClientError(
                f"AI request input is ~{estimated_input_tokens} tokens, over the "
                f"budget of {self._max_input_tokens} "
                "(AI_MAX_INPUT_TOKENS_PER_REQUEST). Split the payload into smaller "
                "chunks or lower the row/sample limits."
            )

        payload: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            # Output-token cap so limited/free-tier usage stays bounded.
            "max_tokens": self._max_output_tokens,
        }
        if request_json_object:
            # Optional hint; servers that do not support it should ignore it.
            payload["response_format"] = {"type": "json_object"}

        # Audit (secret-free): only counts and the output cap are logged.
        logger.info(
            "AI chat request: est_input_tokens=%s, output_token_cap=%s, messages=%s.",
            estimated_input_tokens,
            self._max_output_tokens,
            len(messages),
        )

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = requests.post(
                    self._endpoint,
                    headers=self._headers(),
                    json=payload,
                    timeout=self._timeout,
                )
            except requests.Timeout as exc:
                last_error = exc
                logger.warning(
                    "AI request timed out (attempt %s/%s).",
                    attempt + 1,
                    self._max_retries + 1,
                )
            except requests.RequestException as exc:
                # Avoid leaking secrets: report only the exception type.
                last_error = exc
                logger.warning(
                    "AI request failed (attempt %s/%s): %s",
                    attempt + 1,
                    self._max_retries + 1,
                    type(exc).__name__,
                )
            else:
                if response.status_code == 200:
                    return self._extract_text(response.json())
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    last_error = AIClientError(
                        self._client_error_message(response.status_code)
                    )
                    logger.warning(
                        "AI retryable status %s (attempt %s/%s).",
                        response.status_code,
                        attempt + 1,
                        self._max_retries + 1,
                    )
                else:
                    raise AIClientError(
                        self._client_error_message(response.status_code)
                    )

            if attempt < self._max_retries:
                time.sleep(min(2**attempt, 8))

        if isinstance(last_error, AIClientError):
            raise last_error
        raise AIClientError(
            "AI request failed after "
            f"{self._max_retries + 1} attempt(s): {type(last_error).__name__}."
        )

    @staticmethod
    def _extract_text(data: dict) -> str:
        """Pull the assistant text out of an OpenAI-compatible response."""
        try:
            choices = data["choices"]
            message = choices[0]["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIClientError(
                "AI response did not contain expected choices/message/content."
            ) from exc
        if content is None:
            raise AIClientError("AI response message content was null.")
        return str(content)
