# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Notification settings (environment, prefix ``PAIRLY_``).

Email: ``PAIRLY_EMAIL_TRANSPORT`` is ``smtp`` (Mailpit locally, any relay in production,
same ``PAIRLY_SMTP_*`` variables as the web app), ``resend`` (HTTP API, needs
``PAIRLY_RESEND_API_KEY``), ``fake`` (tests and demos, sends nothing) or ``outbox``
(native development and tests only: each message is written as a file to
``PAIRLY_OUTBOX_DIR``, see ``pairly_core.notifications.outbox``).

Fail closed:

* Outside development and test, SMTP to a non-local host must use TLS
  (``PAIRLY_SMTP_SECURE`` or ``PAIRLY_SMTP_REQUIRE_TLS``), public URLs must be https,
  and ``PAIRLY_SIGNED_LINK_SECRET`` (unsubscribe links) is required.
* Without ``PAIRLY_SLACK_SIGNING_SECRET`` every Slack endpoint answers 503: no request
  is ever accepted unsigned.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pairly_core.config import ENV_PREFIX, ConfigError, Environment

_LOCAL = frozenset({"localhost", "127.0.0.1", "::1", "mailpit"})


class NotificationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, case_sensitive=False, extra="ignore", frozen=True
    )

    env: Environment = Environment.PRODUCTION

    # Where links in messages point: the web app (approval confirmation page, inbox) and
    # the API (one-click unsubscribe).
    public_web_url: str | None = None
    public_api_url: str | None = None

    email_transport: Literal["smtp", "resend", "fake", "outbox"] = "smtp"
    # Development outbox folder (email_transport "outbox" only).
    outbox_dir: str | None = None
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_secure: bool = False  # implicit TLS (port 465)
    smtp_require_tls: bool = False  # STARTTLS, refuse to send without it
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str = "Pairly <no-reply@pairly.localhost>"
    smtp_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    resend_api_key: SecretStr | None = None
    resend_base_url: str = "https://api.resend.com"

    # HMAC key for one-click unsubscribe links.
    signed_link_secret: SecretStr | None = None

    slack_signing_secret: SecretStr | None = None
    slack_api_base_url: str = "https://slack.com/api"
    # Seconds a Slack request timestamp may differ from our clock.
    slack_timestamp_window_seconds: int = Field(default=300, ge=30, le=900)

    # Delivery: attempts before giving up, first retry delay (doubles each time).
    delivery_max_attempts: int = Field(default=6, ge=1, le=20)
    delivery_backoff_seconds: int = Field(default=30, ge=1, le=3600)
    # Daily digest, UTC hour.
    digest_hour_utc: int = Field(default=7, ge=0, le=23)
    # Receipt replies to inbound email (backscatter limits).
    receipt_limit_per_sender_per_day: int = Field(default=5, ge=0, le=1000)
    receipt_limit_per_org_per_hour: int = Field(default=200, ge=0, le=100_000)
    # Server-sent events: database poll interval and the longest a stream stays open.
    sse_poll_seconds: float = Field(default=5.0, ge=0.05, le=60)
    sse_max_seconds: int = Field(default=300, ge=1, le=3600)

    @property
    def relaxed(self) -> bool:
        return self.env in {Environment.DEVELOPMENT, Environment.TEST}

    @model_validator(mode="after")
    def _checks(self) -> NotificationSettings:
        problems: list[str] = []
        defaults = {
            "public_web_url": "http://localhost:3100",
            "public_api_url": "http://localhost:8000",
        }
        for name, fallback in defaults.items():
            value = getattr(self, name)
            if value is None:
                if not self.relaxed:
                    problems.append(f"{name} is required")
                    continue
                value = fallback
            parts = urlsplit(value)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.query:
                problems.append(f"{name} must be an absolute http(s) URL")
            elif parts.scheme != "https" and not self.relaxed:
                problems.append(f"{name} must use https outside development and test")
            object.__setattr__(self, name, value.rstrip("/"))
        if self.email_transport == "smtp":
            if not self.smtp_host:
                if not self.relaxed:
                    problems.append("smtp_host is required for the smtp transport")
            elif (
                not self.relaxed
                and self.smtp_host not in _LOCAL
                and not (self.smtp_secure or self.smtp_require_tls)
            ):
                problems.append("SMTP must use TLS outside development and test")
        if self.email_transport == "resend" and self.resend_api_key is None:
            problems.append("resend_api_key is required for the resend transport")
        if self.email_transport == "fake" and self.env is Environment.PRODUCTION:
            problems.append("the fake email transport is not allowed in production")
        if self.email_transport == "outbox":
            if not self.relaxed:
                problems.append("the outbox email transport is for development and test only")
            if not (self.outbox_dir or "").strip():
                problems.append("outbox_dir is required for the outbox transport")
        if self.signed_link_secret is None and self.env is not Environment.TEST:
            problems.append("signed_link_secret is required")
        elif (
            self.signed_link_secret is not None
            and len(self.signed_link_secret.get_secret_value()) < 32
        ):
            problems.append("signed_link_secret must be at least 32 characters")
        for name in ("resend_base_url", "slack_api_base_url"):
            parts = urlsplit(getattr(self, name))
            if parts.scheme != "https" and not self.relaxed:
                problems.append(f"{name} must use https")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def web_url(self) -> str:
        assert self.public_web_url is not None  # noqa: S101 (set by the validator)
        return self.public_web_url

    @property
    def api_url(self) -> str:
        assert self.public_api_url is not None  # noqa: S101 (set by the validator)
        return self.public_api_url


def _env_name(loc: tuple[int | str, ...]) -> str:
    return f"{ENV_PREFIX}{str(loc[0]).upper()}" if loc else "settings"


def load_notification_settings(**overrides: Any) -> NotificationSettings:
    """Build ``NotificationSettings`` from the environment, or raise ``ConfigError``."""
    try:
        return NotificationSettings(**overrides)
    except ValidationError as exc:
        missing = sorted({_env_name(e["loc"]) for e in exc.errors() if e["type"] == "missing"})
        # Never echo input values: they can hold credentials.
        invalid = sorted(
            f"{_env_name(e['loc'])}: {e['msg']}" for e in exc.errors() if e["type"] != "missing"
        )
        lines = []
        if missing:
            lines.append("missing required settings: " + ", ".join(missing))
        if invalid:
            lines.append("invalid settings: " + "; ".join(invalid))
        raise ConfigError(". ".join(lines)) from None
