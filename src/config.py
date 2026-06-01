"""Project settings loaded from environment / .env.

Covers every stage of the pipeline: raw extraction, deterministic formalization,
optional token-limited AI assistance (layout profiling, failed-row repair,
unknown-label grouping), and deterministic reconciliation workbook generation.

Safety notes:
- AI_API_KEY is never logged or printed by this module (or anywhere else).
- Hosted AI providers are gated behind AI_DATA_APPROVAL=hosted_approved so that
  financial documents are never sent to a hosted service without explicit opt-in.
- AI usage is input-limited (character cap + estimated-token budget) and
  output-limited (max_tokens) so free/limited tiers stay bounded.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, SecretStr

# Conservative characters-per-token ratio used to estimate token budgets without
# adding a tokenizer dependency. English/JSON payloads average ~4 chars/token;
# a low divisor over-estimates tokens, which keeps us safely under provider caps.
CHARS_PER_TOKEN_ESTIMATE = 4


def estimate_tokens(text: str) -> int:
    """Conservatively estimate the token count of ``text``.

    Uses a fixed characters-per-token divisor (no tokenizer dependency). The
    estimate intentionally rounds up so a request is never under-counted when
    checked against an input-token budget.
    """
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN_ESTIMATE)

# Project root is this file's parent's parent (the repo root).
_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parent

# Load .env from the repo root if present. Never raises if the file is absent.
load_dotenv(_REPO_ROOT / ".env")

HOSTED_PROVIDERS = {"hosted_openai_compatible"}
APPROVAL_HOSTED = "hosted_approved"

# Formalization AI modes. ``off`` is the safe default and the only value that
# guarantees no model is ever called from the formalization stage.
FORMALIZATION_AI_OFF = "off"
FORMALIZATION_AI_LAYOUT = "layout_only"
FORMALIZATION_AI_REPAIR = "repair_failed_rows"
FORMALIZATION_AI_GROUP = "group_unknown_labels"
FORMALIZATION_AI_MODES = (
    FORMALIZATION_AI_OFF,
    FORMALIZATION_AI_LAYOUT,
    FORMALIZATION_AI_REPAIR,
    FORMALIZATION_AI_GROUP,
)
FORMALIZATION_AI_MODES_ACTIVE = {
    FORMALIZATION_AI_LAYOUT,
    FORMALIZATION_AI_REPAIR,
    FORMALIZATION_AI_GROUP,
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    """Return an env value restricted to ``allowed``; unknown values fall back.

    Falling back to a safe default (rather than raising) prevents a typo in the
    AI mode from leaving the gate in an undefined state.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    return value if value in allowed else default


class Settings(BaseModel):
    """Runtime settings for extraction and AI-assisted standardization."""

    app_env: str
    project_root: Path
    default_pair_id: str
    input_pair_root: Path
    raw_context_output_dir: Path
    project_context_dump_dir: Path
    log_level: str

    # AI / provider configuration.
    ai_enabled: bool
    ai_provider: str
    ai_api_base_url: str
    # SecretStr keeps the key out of repr/str/logs; read via get_secret_value().
    ai_api_key: SecretStr
    ai_model_name: str
    ai_temperature: float
    ai_timeout_seconds: int
    ai_max_retries: int
    ai_max_input_chars_per_request: int
    # Token budgeting (layered on top of the character cap). Input tokens are
    # estimated with a conservative divisor; output tokens are passed to the
    # provider as ``max_tokens`` so free/limited tiers stay bounded.
    ai_max_input_tokens_per_request: int
    ai_max_output_tokens: int
    ai_data_approval: str

    # Standardization paths.
    type_labels_path: Path
    standardized_output_dir: Path
    reference_profile_output_dir: Path

    # Formalization stage (deterministic two-ledger formalization).
    formalized_output_dir: Path
    ai_formalization_mode: str
    ai_max_layout_sample_lines: int
    ai_max_failed_rows_per_request: int
    ai_max_ai_pages_per_ledger: int
    ai_formalization_cache_enabled: bool
    ai_formalization_cache_dir: Path
    formalization_layout_fingerprint_dir: Path = Path(
        "data/04_outputs/formalization_layout_fingerprints"
    )
    formalization_min_page_confidence_for_ai: float = 70.0

    def resolved(self, path: Path) -> Path:
        """Resolve a (possibly relative) path against the project root."""
        return path if path.is_absolute() else (self.project_root / path)

    def is_hosted_provider(self) -> bool:
        """True if the configured provider sends data to a hosted service."""
        return self.ai_provider in HOSTED_PROVIDERS

    def hosted_approved(self) -> bool:
        """True only when hosted data egress has been explicitly approved."""
        return self.ai_data_approval == APPROVAL_HOSTED

    def formalization_ai_enabled(self) -> bool:
        """True when the formalization stage is configured to use AI at all."""
        return self.ai_formalization_mode in FORMALIZATION_AI_MODES_ACTIVE

    def ensure_formalization_ai_allowed(self) -> None:
        """Validate that a formalization AI call is permitted, else raise.

        Layered on top of :meth:`ensure_ai_call_allowed`: the formalization mode
        must be active (not ``off``) AND the global AI safety gate must pass.
        With ``AI_FORMALIZATION_MODE=off`` (the default) this always raises, so
        the deterministic path can never accidentally call a model.
        """
        if not self.formalization_ai_enabled():
            raise RuntimeError(
                "Formalization AI is off (AI_FORMALIZATION_MODE=off). "
                "Set it to 'layout_only' or 'repair_failed_rows' to enable."
            )
        self.ensure_ai_call_allowed()

    def ensure_ai_call_allowed(self) -> None:
        """Validate that an AI call is permitted, else raise a clear error.

        Must be called before any network request to an AI provider. For hosted
        providers this enforces AI_DATA_APPROVAL=hosted_approved so financial
        documents are never sent to a hosted service without explicit opt-in.
        The API key is never included in any error message.
        """
        if not self.ai_enabled:
            raise RuntimeError(
                "AI is disabled (AI_ENABLED=false). Enable it in .env to run "
                "AI-assisted standardization."
            )
        if self.is_hosted_provider() and not self.hosted_approved():
            raise RuntimeError(
                "Hosted AI provider "
                f"'{self.ai_provider}' requires AI_DATA_APPROVAL='{APPROVAL_HOSTED}' "
                f"before any API call. Current value: '{self.ai_data_approval}'. "
                "Financial documents must not be sent to a hosted service without "
                "explicit approval."
            )
        if not self.ai_api_base_url:
            raise RuntimeError(
                "AI_API_BASE_URL is empty. Set it in .env before running AI "
                "standardization."
            )
        if not self.ai_model_name:
            raise RuntimeError(
                "AI_MODEL_NAME is empty. Set it in .env before running AI "
                "standardization."
            )


def _build_settings() -> Settings:
    project_root_raw = os.getenv("PROJECT_ROOT", ".")
    project_root = Path(project_root_raw)
    if not project_root.is_absolute():
        # Resolve relative PROJECT_ROOT against the repo root for stability,
        # regardless of the current working directory.
        project_root = (_REPO_ROOT / project_root).resolve()

    return Settings(
        app_env=os.getenv("APP_ENV", "development"),
        project_root=project_root,
        default_pair_id=os.getenv("DEFAULT_PAIR_ID", "pair_001_baby_and_mom__good_luck"),
        input_pair_root=Path(os.getenv("INPUT_PAIR_ROOT", "data/02_work_pairs")),
        raw_context_output_dir=Path(
            os.getenv("RAW_CONTEXT_OUTPUT_DIR", "data/04_outputs/raw_context_workbooks")
        ),
        project_context_dump_dir=Path(
            os.getenv("PROJECT_CONTEXT_DUMP_DIR", "data/04_outputs/project_context_dumps")
        ),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        ai_enabled=_env_bool("AI_ENABLED", False),
        ai_provider=os.getenv("AI_PROVIDER", "hosted_openai_compatible"),
        ai_api_base_url=os.getenv("AI_API_BASE_URL", "").strip(),
        ai_api_key=SecretStr(os.getenv("AI_API_KEY", "")),
        ai_model_name=os.getenv("AI_MODEL_NAME", "").strip(),
        ai_temperature=_env_float("AI_TEMPERATURE", 0.0),
        ai_timeout_seconds=_env_int("AI_TIMEOUT_SECONDS", 120),
        ai_max_retries=_env_int("AI_MAX_RETRIES", 2),
        ai_max_input_chars_per_request=_env_int(
            "AI_MAX_INPUT_CHARS_PER_REQUEST", 12000
        ),
        ai_max_input_tokens_per_request=_env_int(
            "AI_MAX_INPUT_TOKENS_PER_REQUEST", 3000
        ),
        ai_max_output_tokens=_env_int("AI_MAX_OUTPUT_TOKENS", 1024),
        ai_data_approval=os.getenv("AI_DATA_APPROVAL", "local_only"),
        type_labels_path=Path(os.getenv("TYPE_LABELS_PATH", "config/type_labels.json")),
        standardized_output_dir=Path(
            os.getenv(
                "STANDARDIZED_OUTPUT_DIR", "data/04_outputs/standardized_workbooks"
            )
        ),
        reference_profile_output_dir=Path(
            os.getenv(
                "REFERENCE_PROFILE_OUTPUT_DIR", "data/04_outputs/reference_profiles"
            )
        ),
        formalized_output_dir=Path(
            os.getenv(
                "FORMALIZED_OUTPUT_DIR", "data/04_outputs/formalized_workbooks"
            )
        ),
        ai_formalization_mode=_env_choice(
            "AI_FORMALIZATION_MODE", FORMALIZATION_AI_OFF, FORMALIZATION_AI_MODES
        ),
        ai_max_layout_sample_lines=_env_int("AI_MAX_LAYOUT_SAMPLE_LINES", 40),
        ai_max_failed_rows_per_request=_env_int("AI_MAX_FAILED_ROWS_PER_REQUEST", 25),
        ai_max_ai_pages_per_ledger=_env_int("AI_MAX_AI_PAGES_PER_LEDGER", 3),
        ai_formalization_cache_enabled=_env_bool("AI_FORMALIZATION_CACHE_ENABLED", True),
        ai_formalization_cache_dir=Path(
            os.getenv(
                "AI_FORMALIZATION_CACHE_DIR", "data/04_outputs/formalization_ai_cache"
            )
        ),
        formalization_layout_fingerprint_dir=Path(
            os.getenv(
                "FORMALIZATION_LAYOUT_FINGERPRINT_DIR",
                "data/04_outputs/formalization_layout_fingerprints",
            )
        ),
        formalization_min_page_confidence_for_ai=_env_float(
            "FORMALIZATION_MIN_PAGE_CONFIDENCE_FOR_AI", 70.0
        ),
    )


settings = _build_settings()
