# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Settings fail closed: missing or unsafe values stop startup with a clear message."""

from __future__ import annotations

import pytest

from pairly_core.config import ConfigError, Environment, load_settings

REQUIRED = {
    "PAIRLY_DATABASE_URL": "postgresql://app:pw@db:5432/pairly",
    "PAIRLY_AUTH_JWKS_URL": "https://auth.example/api/auth/jwks",
    "PAIRLY_AUTH_ISSUER": "https://auth.example",
    "PAIRLY_AUTH_AUDIENCE": "https://api.example",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key in [*REQUIRED, "PAIRLY_ENV", "PAIRLY_CORS_ORIGINS"]:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _set_all(env: pytest.MonkeyPatch, **extra: str) -> None:
    for key, value in {**REQUIRED, **extra}.items():
        env.setenv(key, value)


def test_missing_settings_are_all_listed(env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError) as info:
        load_settings()
    message = str(info.value)
    for key in REQUIRED:
        assert key in message


def test_valid_settings(env: pytest.MonkeyPatch) -> None:
    _set_all(env, PAIRLY_CORS_ORIGINS="https://app.example, http://localhost:3000")
    settings = load_settings()
    assert settings.env is Environment.PRODUCTION
    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")
    assert settings.cors_origins == ["https://app.example", "http://localhost:3000"]
    assert "pw" not in repr(settings)


@pytest.mark.parametrize(
    ("extra", "fragment"),
    [
        ({"PAIRLY_CORS_ORIGINS": "*"}, "PAIRLY_CORS_ORIGINS"),
        ({"PAIRLY_CORS_ORIGINS": "https://*.example"}, "PAIRLY_CORS_ORIGINS"),
        ({"PAIRLY_AUTH_ALGORITHMS": "HS256"}, "PAIRLY_AUTH_ALGORITHMS"),
        ({"PAIRLY_AUTH_ALGORITHMS": "none"}, "PAIRLY_AUTH_ALGORITHMS"),
        ({"PAIRLY_DATABASE_URL": "mysql://x"}, "PAIRLY_DATABASE_URL"),
        ({"PAIRLY_AUTH_JWKS_URL": "http://auth.example/jwks"}, "https"),
    ],
)
def test_unsafe_settings_rejected(
    env: pytest.MonkeyPatch, extra: dict[str, str], fragment: str
) -> None:
    _set_all(env, **extra)
    with pytest.raises(ConfigError, match=fragment):
        load_settings()


def test_plain_http_jwks_allowed_in_development(env: pytest.MonkeyPatch) -> None:
    _set_all(env, PAIRLY_ENV="development", PAIRLY_AUTH_JWKS_URL="http://web:3000/api/auth/jwks")
    assert load_settings().auth_jwks_url.startswith("http://")
