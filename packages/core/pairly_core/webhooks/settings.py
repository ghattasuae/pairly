# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Webhook settings (prefix ``PAIRLY_``), shared by the API and the worker.

* ``PAIRLY_WEBHOOK_TIMEOUT_SECONDS`` (default 10): total time for one delivery.
* ``PAIRLY_WEBHOOK_MAX_CONSECUTIVE_FAILURES`` (default 25): failed attempts in a row
  after which an endpoint is disabled.
* ``PAIRLY_WEBHOOK_ALLOW_HTTP`` and ``PAIRLY_WEBHOOK_ALLOW_PRIVATE_TARGETS`` (default
  false): local development only, refused in production and staging (fail closed).
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pairly_core.config import ENV_PREFIX, ConfigError, Environment


class WebhookSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, case_sensitive=False, extra="ignore", frozen=True
    )

    env: Environment = Environment.PRODUCTION
    webhook_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
    webhook_connect_timeout_seconds: float = Field(default=5.0, ge=0.5, le=30.0)
    webhook_max_response_bytes: int = Field(default=64 * 1024, ge=1024, le=1024 * 1024)
    webhook_max_consecutive_failures: int = Field(default=25, ge=1, le=10000)
    webhook_batch_size: int = Field(default=50, ge=1, le=1000)
    webhook_concurrency: int = Field(default=8, ge=1, le=64)
    webhook_allow_http: bool = False
    webhook_allow_private_targets: bool = False

    @model_validator(mode="after")
    def _dev_only(self) -> WebhookSettings:
        relaxed = self.webhook_allow_http or self.webhook_allow_private_targets
        if relaxed and self.env not in {Environment.DEVELOPMENT, Environment.TEST}:
            raise ValueError("http and private webhook targets are only allowed in development")
        return self


def load_webhook_settings(**overrides: Any) -> WebhookSettings:
    try:
        return WebhookSettings(**overrides)
    except ValidationError as exc:
        problems = sorted(
            f"{ENV_PREFIX}{str(e['loc'][0]).upper() if e['loc'] else 'WEBHOOK'}: {e['msg']}"
            for e in exc.errors()
        )
        raise ConfigError("invalid settings: " + "; ".join(problems)) from None
