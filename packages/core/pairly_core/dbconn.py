# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Connection options for running behind a connection pooler (Supabase's Supavisor).

``PAIRLY_DATABASE_POOLER`` says how the application role reaches Postgres:

* ``none`` (default) or ``session``: a direct connection, or a session-mode pooler
  (Supavisor on port 5432), where one client owns one server connection for its whole
  session. Everything works as on a local Postgres. This is the recommended mode for
  Pairly's long-lived API and worker processes.
* ``transaction``: a transaction-mode pooler (Supavisor on port 6543, PgBouncer). A
  server connection is shared between clients after each transaction, so:

  - server-side prepared statements must not outlive a transaction: asyncpg's statement
    cache (``statement_cache_size=0``) and SQLAlchemy's (``prepared_statement_cache_size
    =0``) are turned off, and the statements SQLAlchemy still prepares get unique names
    (``prepared_statement_name_func``), as SQLAlchemy's PgBouncer notes advise;
  - the tenant context is unaffected: ``pairly_core.db`` sets ``app.org_id`` with
    ``set_config(..., true)``, local to the transaction;
  - the LangGraph checkpointer is NOT safe there: it sets ``app.org_id`` for the session
    of its own connection. It needs ``PAIRLY_CHECKPOINT_DATABASE_URL`` (direct or
    session mode, same application role); without it the settings are refused.

Migrations and the owner role never go through a transaction pooler
(``refuse_transaction_pooler``): Alembic needs one session for a whole migration.

Supabase facts behind this (read 2026-10-19):
https://supabase.com/docs/guides/database/connecting-to-postgres ("Transaction mode does
not support prepared statements"), https://supabase.com/docs/guides/troubleshooting/
disabling-prepared-statements-qL8lEL (asyncpg ``statement_cache_size=0``).
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import SecretStr
from sqlalchemy.engine import make_url

from pairly_core.config import DatabasePooler

SUPABASE_POOLER_SUFFIX = ".pooler.supabase.com"
SUPABASE_TRANSACTION_PORT = 6543


def _secret(value: SecretStr | str) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def is_transaction_pooler_url(url: SecretStr | str) -> bool:
    """True for a Supabase transaction-mode pooler URL (``*.pooler.supabase.com:6543``)."""
    try:
        parsed = make_url(_secret(url))
    except Exception:
        return False
    host = (parsed.host or "").lower()
    return host.endswith(SUPABASE_POOLER_SUFFIX) and parsed.port == SUPABASE_TRANSACTION_PORT


def refuse_transaction_pooler(url: SecretStr | str, what: str) -> None:
    """Raise ``ValueError`` when ``url`` is a transaction-mode pooler."""
    if is_transaction_pooler_url(url):
        raise ValueError(
            f"{what} must use a direct or session-mode connection, not the transaction "
            f"pooler (port {SUPABASE_TRANSACTION_PORT})"
        )


def _statement_name() -> str:
    return f"__asyncpg_{uuid.uuid4()}__"


def engine_kwargs(pooler: DatabasePooler) -> dict[str, Any]:
    """Extra ``create_async_engine`` arguments for the pooler mode (empty unless transaction)."""
    if pooler != "transaction":
        return {}
    return {
        "connect_args": {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": _statement_name,
        }
    }


def checkpoint_url(
    database_url: SecretStr | str,
    pooler: DatabasePooler,
    checkpoint_database_url: SecretStr | str | None,
) -> str:
    """The URL the LangGraph checkpointer connects with. Raises ``ValueError`` when the
    configuration would put its session-level tenant setting behind a transaction pooler."""
    if checkpoint_database_url is not None and _secret(checkpoint_database_url).strip():
        refuse_transaction_pooler(checkpoint_database_url, "PAIRLY_CHECKPOINT_DATABASE_URL")
        return _secret(checkpoint_database_url)
    if pooler == "transaction":
        raise ValueError(
            "PAIRLY_CHECKPOINT_DATABASE_URL (direct or session mode) is required with "
            "PAIRLY_DATABASE_POOLER=transaction"
        )
    if is_transaction_pooler_url(database_url):
        raise ValueError(
            "PAIRLY_DATABASE_URL is a transaction pooler: set PAIRLY_DATABASE_POOLER="
            "transaction and PAIRLY_CHECKPOINT_DATABASE_URL (direct or session mode)"
        )
    return _secret(database_url)
