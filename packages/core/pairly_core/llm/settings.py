# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Model router settings, read from the environment.

Most names use the ``PAIRLY_`` prefix. Provider keys and LangFuse use the names their
own tools use (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``LANGFUSE_*``), which is
what ``.env.example`` and the compose file already pass.

Defaults are the safe ones: ``recorded`` mode (no live calls, no spend) and no keys.
A provider without a key is simply unavailable; the router never lets an SDK pick up
a key from somewhere else.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from pydantic import AliasChoices, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from pairly_core.config import ENV_PREFIX, ConfigError


class ModelMode(StrEnum):
    """``live`` calls providers and stores every response for replay. ``record`` does the
    same and marks the rows as a recording session (for bundle export). ``recorded``
    serves only stored responses and fails closed when one is missing."""

    LIVE = "live"
    RECORD = "record"
    RECORDED = "recorded"


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    model_mode: ModelMode = ModelMode.RECORDED

    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("PAIRLY_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )
    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("PAIRLY_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )

    # Default monthly budget per organisation in USD (per-org overrides live in the
    # ``org_llm_budgets`` table). 0 blocks every paid call.
    llm_monthly_budget_usd: Decimal = Field(default=Decimal("20"), ge=0, le=1_000_000)
    # Seconds a reservation is held before it stops counting (covers a crashed caller).
    llm_reservation_ttl_seconds: int = Field(default=900, ge=60, le=86_400)

    llm_request_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    # Retries after the first request, on retryable errors only.
    llm_max_retries: int = Field(default=2, ge=0, le=6)
    llm_retry_base_delay_seconds: float = Field(default=0.5, ge=0, le=30)
    llm_retry_max_delay_seconds: float = Field(default=8.0, ge=0, le=120)
    llm_breaker_failure_threshold: int = Field(default=5, ge=1, le=100)
    llm_breaker_reset_seconds: float = Field(default=30.0, gt=0, le=3600)

    # Live mode: a request whose hash this organisation already had answered (same
    # prompt, model, inputs, schema and settings) reuses that answer instead of calling
    # the model again, so identical input gives identical output and costs nothing.
    # The row says which call it reused (``model_calls.reused_from``). Off with
    # ``false``; answers older than the TTL (days) are not reused.
    llm_reuse_responses: bool = True
    llm_reuse_ttl_days: int = Field(default=30, ge=1, le=3650)

    # Caps on what one call may store (request inputs and model output, UTF-8 bytes).
    llm_max_request_bytes: int = Field(default=1_048_576, ge=1024, le=16_777_216)
    llm_max_response_bytes: int = Field(default=262_144, ge=1024, le=4_194_304)

    # Override the bundled catalog and price table (versioned YAML files).
    llm_models_file: Path | None = None
    llm_prices_file: Path | None = None
    # Provider preference, first is primary. Empty means the catalog's order.
    llm_provider_order: Annotated[list[str], NoDecode] = Field(default_factory=list)

    langfuse_host: str | None = Field(
        default=None, validation_alias=AliasChoices("LANGFUSE_HOST", "LANGFUSE_BASE_URL")
    )
    langfuse_public_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("LANGFUSE_PUBLIC_KEY")
    )
    langfuse_secret_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("LANGFUSE_SECRET_KEY")
    )

    @field_validator("llm_provider_order", mode="before")
    @classmethod
    def _csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator(
        "anthropic_api_key",
        "openai_api_key",
        "langfuse_public_key",
        "langfuse_secret_key",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        # Compose passes ``ANTHROPIC_API_KEY=`` when unset: that means "no key".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_host and self.langfuse_public_key and self.langfuse_secret_key)


def load_llm_settings(**overrides: Any) -> LLMSettings:
    """Build ``LLMSettings`` from the environment, or raise ``ConfigError``."""
    try:
        return LLMSettings(**overrides)
    except ValidationError as exc:
        # Never echo the input: keys are secrets.
        problems = sorted(
            f"{'.'.join(str(p) for p in e['loc']) or 'settings'}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError("invalid model router settings: " + "; ".join(problems)) from None
