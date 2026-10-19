# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pooler settings (``PAIRLY_DATABASE_POOLER``), the checkpointer URL rule, and the
``sslmode`` translation between libpq and asyncpg URLs (Supabase hosting)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from pairly_core.config import ConfigError, load_settings, to_asyncpg_url
from pairly_core.dbconn import (
    checkpoint_url,
    engine_kwargs,
    is_transaction_pooler_url,
    refuse_transaction_pooler,
)
from pairly_core.workflow.checkpoint import psycopg_conninfo

SESSION = "postgresql://pairly_app.abcd:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
TRANSACTION = "postgresql://pairly_app.abcd:pw@aws-0-eu-central-1.pooler.supabase.com:6543/postgres"
DIRECT = "postgresql://pairly_app:pw@db.abcd.supabase.co:5432/postgres"

REQUIRED = {
    "PAIRLY_AUTH_JWKS_URL": "https://auth.example/api/auth/jwks",
    "PAIRLY_AUTH_ISSUER": "https://auth.example",
    "PAIRLY_AUTH_AUDIENCE": "https://api.example",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key in (
        "PAIRLY_DATABASE_URL",
        "PAIRLY_DATABASE_POOLER",
        "PAIRLY_CHECKPOINT_DATABASE_URL",
        "PAIRLY_ENV",
        *REQUIRED,
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_default_is_no_pooler(env: pytest.MonkeyPatch) -> None:
    env.setenv("PAIRLY_DATABASE_URL", "postgresql://app:pw@db:5432/pairly")
    settings = load_settings()
    assert settings.database_pooler == "none"
    assert settings.checkpoint_database_url is None
    assert engine_kwargs(settings.database_pooler) == {}


def test_session_pooler_needs_nothing_else(env: pytest.MonkeyPatch) -> None:
    env.setenv("PAIRLY_DATABASE_URL", SESSION + "?sslmode=require")
    env.setenv("PAIRLY_DATABASE_POOLER", "session")
    settings = load_settings()
    assert settings.database_pooler == "session"
    assert engine_kwargs("session") == {}
    url = checkpoint_url(settings.database_url, "session", None)
    assert url.endswith("?ssl=require")


def test_transaction_pooler_requires_a_checkpoint_url(env: pytest.MonkeyPatch) -> None:
    env.setenv("PAIRLY_DATABASE_URL", TRANSACTION)
    env.setenv("PAIRLY_DATABASE_POOLER", "transaction")
    with pytest.raises(ConfigError, match="PAIRLY_CHECKPOINT_DATABASE_URL"):
        load_settings()
    env.setenv("PAIRLY_CHECKPOINT_DATABASE_URL", SESSION)
    settings = load_settings()
    assert settings.checkpoint_database_url is not None
    assert checkpoint_url(
        settings.database_url, settings.database_pooler, settings.checkpoint_database_url
    ).startswith(
        "postgresql+asyncpg://pairly_app.abcd:pw@aws-0-eu-central-1.pooler.supabase.com:5432"
    )


def test_checkpoint_url_never_on_the_transaction_pooler(env: pytest.MonkeyPatch) -> None:
    env.setenv("PAIRLY_DATABASE_URL", TRANSACTION)
    env.setenv("PAIRLY_DATABASE_POOLER", "transaction")
    env.setenv("PAIRLY_CHECKPOINT_DATABASE_URL", TRANSACTION)
    with pytest.raises(ConfigError, match="not the transaction pooler"):
        load_settings()


def test_transaction_pooler_url_without_the_flag_is_refused(env: pytest.MonkeyPatch) -> None:
    env.setenv("PAIRLY_DATABASE_URL", TRANSACTION)
    with pytest.raises(ConfigError, match="PAIRLY_DATABASE_POOLER=transaction"):
        load_settings()


def test_unknown_pooler_mode_is_refused(env: pytest.MonkeyPatch) -> None:
    env.setenv("PAIRLY_DATABASE_URL", DIRECT)
    env.setenv("PAIRLY_DATABASE_POOLER", "statement")
    with pytest.raises(ConfigError, match="PAIRLY_DATABASE_POOLER"):
        load_settings()


def test_blank_checkpoint_url_is_unset(env: pytest.MonkeyPatch) -> None:
    env.setenv("PAIRLY_DATABASE_URL", DIRECT)
    env.setenv("PAIRLY_CHECKPOINT_DATABASE_URL", "  ")
    assert load_settings().checkpoint_database_url is None


def test_transaction_engine_arguments_turn_the_caches_off() -> None:
    kwargs: dict[str, Any] = engine_kwargs("transaction")
    args = kwargs["connect_args"]
    assert args["statement_cache_size"] == 0
    assert args["prepared_statement_cache_size"] == 0
    first, second = args["prepared_statement_name_func"](), args["prepared_statement_name_func"]()
    assert first != second
    assert first.startswith("__asyncpg_")
    # The arguments are accepted by SQLAlchemy's asyncpg dialect (no connection made).
    engine = create_async_engine(to_asyncpg_url(TRANSACTION), **kwargs)
    assert engine.dialect.name == "postgresql"


def test_transaction_pooler_detection() -> None:
    assert is_transaction_pooler_url(TRANSACTION)
    assert is_transaction_pooler_url(SecretStr(to_asyncpg_url(TRANSACTION)))
    assert not is_transaction_pooler_url(SESSION)
    assert not is_transaction_pooler_url(DIRECT)
    assert not is_transaction_pooler_url("not a url")
    refuse_transaction_pooler(SESSION, "owner")
    with pytest.raises(ValueError, match="owner must use a direct or session-mode"):
        refuse_transaction_pooler(TRANSACTION, "owner")


def test_checkpoint_url_function() -> None:
    assert checkpoint_url(DIRECT, "none", None) == DIRECT
    assert checkpoint_url(DIRECT, "none", "") == DIRECT
    assert checkpoint_url(TRANSACTION, "transaction", SESSION) == SESSION


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql://u:p@h/db?sslmode=require", "postgresql+asyncpg://u:p@h/db?ssl=require"),
        (
            "postgres://u:p%40x@h/db?application_name=pairly&sslmode=verify-full",
            "postgresql+asyncpg://u:p%40x@h/db?application_name=pairly&ssl=verify-full",
        ),
        # An explicit asyncpg ssl= wins; sslmode is dropped.
        (
            "postgresql://u:p@h/db?ssl=disable&sslmode=require",
            "postgresql+asyncpg://u:p@h/db?ssl=disable",
        ),
    ],
)
def test_sslmode_becomes_asyncpg_ssl(given: str, expected: str) -> None:
    assert to_asyncpg_url(given) == expected


def test_psycopg_gets_sslmode_back() -> None:
    conninfo = psycopg_conninfo(to_asyncpg_url(SESSION + "?sslmode=verify-full"))
    assert conninfo.startswith("postgresql://pairly_app.abcd:pw@")
    assert conninfo.endswith("?sslmode=verify-full")
    assert "ssl=" not in conninfo.replace("sslmode=", "")
