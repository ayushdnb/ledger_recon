"""Provider-agnostic AI client interface and factory.

This layer only moves text in and out of a chat-completion provider. It performs
no ledger logic, no JSON parsing of ledger content, and never logs secrets.

Use ``build_ai_client`` to obtain a client based on the current settings. The
factory enforces the hosted-approval safety gate before returning a client that
could make a network call.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.config import Settings, settings


class AIClientError(RuntimeError):
    """Raised for AI client failures with safe, secret-free messages."""


class ChatMessage(dict):
    """A single chat message: {"role": ..., "content": ...}."""

    def __init__(self, role: str, content: str) -> None:
        super().__init__(role=role, content=content)


class AIClient(ABC):
    """Abstract chat-completion client returning raw assistant text."""

    @abstractmethod
    def complete_chat(
        self,
        messages: list[dict[str, str]],
        *,
        request_json_object: bool = True,
    ) -> str:
        """Send chat messages and return the raw assistant text content."""
        raise NotImplementedError


def build_ai_client(config: Settings | None = None) -> AIClient:
    """Construct an AI client for the configured provider.

    Enforces the AI safety gate (AI enabled + hosted approval) before returning
    any client capable of making a network call.
    """
    config = config or settings
    config.ensure_ai_call_allowed()

    provider = config.ai_provider
    if provider == "hosted_openai_compatible":
        # Imported lazily to keep the dependency optional at import time.
        from src.providers.openai_compatible_client import OpenAICompatibleClient

        return OpenAICompatibleClient(config)

    raise AIClientError(
        f"Unsupported AI_PROVIDER '{provider}'. Supported: hosted_openai_compatible."
    )
