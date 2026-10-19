# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Worker settings, read from environment variables (prefix ``PAIRLY_``).

Fail closed: ``PAIRLY_REDIS_URL``, ``PAIRLY_DATABASE_URL`` and
``PAIRLY_AUDIT_SIGNING_KEY`` (the Ed25519 key that signs the nightly audit anchors,
kept apart from every database secret) have no defaults, and ``load_worker_config``
raises ``ConfigError`` naming every missing or invalid variable, so the worker (and its
health check) refuse to start instead of guessing.

The worker connects to Postgres as the non-owner application role, like the API.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pairly_core.audit_anchor.signing import SIGNING_KEY_ENV, load_signing_key
from pairly_core.config import (
    ENV_PREFIX,
    ConfigError,
    DatabasePooler,
    Environment,
    to_asyncpg_url,
)
from pairly_core.dbconn import checkpoint_url


class WorkerConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    env: Environment = Environment.PRODUCTION
    # Non-owner application role (Row Level Security applies).
    database_url: SecretStr
    db_pool_size: int = Field(default=2, ge=1, le=20)
    # Same variables and rules as the API (pairly_core.dbconn).
    database_pooler: DatabasePooler = "none"
    checkpoint_database_url: SecretStr | None = None
    # arq queue (Valkey / Redis protocol). Database 0; LangFuse uses database 1.
    redis_url: SecretStr
    log_level: str = "INFO"
    # Nightly audit chain verification, UTC.
    audit_verify_hour: int = Field(default=2, ge=0, le=23)
    audit_verify_minute: int = Field(default=30, ge=0, le=59)
    # Daily notification digest, UTC hour (same variable as the notification settings).
    digest_hour_utc: int = Field(default=7, ge=0, le=23)
    # Lifetime of the signed approval links in escalation messages (same variable and
    # bounds as the API's setting, so the API and the worker issue the same links).
    approval_link_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    # Ed25519 seed (base64url) that signs each organisation's chain head every night.
    audit_signing_key: SecretStr
    # Nightly anchoring, UTC (before the verification, so it checks the fresh anchor).
    audit_anchor_hour: int = Field(default=2, ge=0, le=23)
    audit_anchor_minute: int = Field(default=10, ge=0, le=59)
    # Seconds between arq health records; the compose health check reads them.
    worker_health_check_interval: int = Field(default=30, ge=5, le=3600)

    @field_validator("database_url")
    @classmethod
    def _db_url(cls, value: SecretStr) -> SecretStr:
        return SecretStr(to_asyncpg_url(value.get_secret_value()))

    @field_validator("checkpoint_database_url", mode="before")
    @classmethod
    def _checkpoint_url(cls, value: Any) -> Any:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, str):
            return to_asyncpg_url(value) if value.strip() else None
        return value

    @model_validator(mode="after")
    def _pooler(self) -> WorkerConfig:
        checkpoint_url(self.database_url, self.database_pooler, self.checkpoint_database_url)
        return self

    @field_validator("audit_signing_key")
    @classmethod
    def _signing_key(cls, value: SecretStr) -> SecretStr:
        try:
            load_signing_key({SIGNING_KEY_ENV: value.get_secret_value()})
        except ConfigError as exc:
            # Never echo the key.
            raise ValueError(str(exc)) from None
        return value

    @field_validator("redis_url")
    @classmethod
    def _redis_url(cls, value: SecretStr) -> SecretStr:
        parts = urlsplit(value.get_secret_value())
        if parts.scheme not in {"redis", "rediss"} or not parts.hostname:
            raise ValueError("must be a redis:// or rediss:// URL with a host")
        return value


def _env_name(loc: tuple[int | str, ...]) -> str:
    return f"{ENV_PREFIX}{str(loc[0]).upper()}" if loc else "settings"


def load_worker_config(**overrides: Any) -> WorkerConfig:
    """Build ``WorkerConfig`` from the environment, or raise ``ConfigError``."""
    try:
        return WorkerConfig(**overrides)
    except ValidationError as exc:
        missing = sorted({_env_name(e["loc"]) for e in exc.errors() if e["type"] == "missing"})
        # Never echo the input: URLs carry passwords.
        invalid = sorted(
            f"{_env_name(e['loc'])}: {e['msg']}" for e in exc.errors() if e["type"] != "missing"
        )
        lines = []
        if missing:
            lines.append("missing required settings: " + ", ".join(missing))
        if invalid:
            lines.append("invalid settings: " + "; ".join(invalid))
        raise ConfigError(". ".join(lines)) from None
