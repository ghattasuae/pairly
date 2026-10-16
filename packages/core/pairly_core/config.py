# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Runtime settings, read from environment variables (prefix ``PAIRLY_``).

Fail closed: required secrets and endpoints have no defaults. ``load_settings`` raises
``ConfigError`` naming every missing or invalid variable, so a misconfigured service
refuses to start instead of running with a guessed value.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENV_PREFIX = "PAIRLY_"

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ConfigError(RuntimeError):
    """Raised when settings are missing or invalid. The message lists every problem."""


class Environment(StrEnum):
    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"
    TEST = "test"


def to_asyncpg_url(url: str) -> str:
    """Normalise a Postgres URL to the SQLAlchemy asyncpg dialect."""
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    raise ValueError("must be a postgresql:// or postgresql+asyncpg:// URL")


def _split_csv(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    """Settings shared by the API and other services.

    Required (no default): ``PAIRLY_DATABASE_URL``, ``PAIRLY_AUTH_JWKS_URL``,
    ``PAIRLY_AUTH_ISSUER``, ``PAIRLY_AUTH_AUDIENCE``.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # Production is the default so that a missing value never loosens a check.
    env: Environment = Environment.PRODUCTION

    # Connection URL for the non-owner application role (RLS applies to it).
    database_url: SecretStr
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)

    # Better Auth JWT plugin: JWKS endpoint, expected issuer and audience.
    auth_jwks_url: str
    auth_issuer: str
    auth_audience: str
    auth_algorithms: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["EdDSA", "ES256", "RS256"]
    )
    auth_leeway_seconds: int = Field(default=10, ge=0, le=60)
    auth_jwks_cache_seconds: int = Field(default=300, ge=10, le=86400)
    # Minimum gap between JWKS refetches triggered by an unknown key id.
    auth_jwks_min_refresh_seconds: int = Field(default=30, ge=1, le=3600)

    # Exact browser origins allowed to call the API. Empty means no CORS at all.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    log_level: str = "INFO"

    # Signed approval links (email, Slack): lifetime in seconds. Short on purpose.
    approval_link_ttl_seconds: int = Field(default=1800, ge=60, le=86400)

    # API keys (WP1.11): default requests per minute per key (a key row may lower or
    # raise it), and the longest lifetime a new key may have.
    api_key_rate_limit_per_minute: int = Field(default=600, ge=1, le=100000)
    api_key_max_days: int = Field(default=365, ge=1, le=3650)
    # Batch API (M6): documents per minute per caller (API key or person), on top of the
    # request rate limit; a batch of 50 counts 50.
    batch_items_per_minute: int = Field(default=300, ge=1, le=100000)

    @field_validator("auth_algorithms", "cors_origins", mode="before")
    @classmethod
    def _csv_list(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("database_url")
    @classmethod
    def _db_url(cls, value: SecretStr) -> SecretStr:
        return SecretStr(to_asyncpg_url(value.get_secret_value()))

    @field_validator("auth_algorithms")
    @classmethod
    def _asymmetric_only(cls, value: list[str]) -> list[str]:
        allowed = {"EdDSA", "ES256", "ES384", "ES512", "RS256", "RS384", "RS512", "PS256"}
        bad = [alg for alg in value if alg not in allowed]
        if bad or not value:
            raise ValueError(f"only asymmetric algorithms from {sorted(allowed)} are allowed")
        return value

    @field_validator("cors_origins")
    @classmethod
    def _exact_origins(cls, value: list[str]) -> list[str]:
        for origin in value:
            parts = urlsplit(origin)
            if (
                origin == "*"
                or parts.scheme not in {"http", "https"}
                or not parts.netloc
                or parts.path not in {"", "/"}
                or parts.query
                or "*" in origin
            ):
                raise ValueError(f"invalid CORS origin {origin!r}: use exact scheme://host[:port]")
        return [o.rstrip("/") for o in value]

    @model_validator(mode="after")
    def _https_outside_dev(self) -> Settings:
        parts = urlsplit(self.auth_jwks_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("auth_jwks_url must be an absolute http(s) URL")
        insecure_ok = self.env in {Environment.DEVELOPMENT, Environment.TEST}
        if parts.scheme != "https" and not insecure_ok and parts.hostname not in _LOCAL_HOSTS:
            raise ValueError("auth_jwks_url must use https outside development and test")
        return self

    @property
    def is_production(self) -> bool:
        return self.env in {Environment.PRODUCTION, Environment.STAGING}


def _env_name(loc: tuple[int | str, ...]) -> str:
    if not loc:
        return "settings"
    return f"{ENV_PREFIX}{str(loc[0]).upper()}"


def load_settings(**overrides: Any) -> Settings:
    """Build ``Settings`` from the environment, or raise ``ConfigError`` listing every problem."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        missing = sorted({_env_name(e["loc"]) for e in exc.errors() if e["type"] == "missing"})
        invalid = sorted(
            f"{_env_name(e['loc'])}: {e['msg']}" for e in exc.errors() if e["type"] != "missing"
        )
        lines = []
        if missing:
            lines.append("missing required settings: " + ", ".join(missing))
        if invalid:
            lines.append("invalid settings: " + "; ".join(invalid))
        raise ConfigError(". ".join(lines)) from None
