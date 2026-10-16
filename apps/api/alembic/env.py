# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Alembic environment. Runs migrations as the owner role (PAIRLY_DATABASE_URL_OWNER).

Workflow plugins are discovered (``pairly.workflows`` entry points) before the metadata
is used, so the tables their modules declare on ``Base.metadata`` are known to
autogenerate and ``alembic check`` like core tables.

Version locations: core's linear chain is ``alembic/versions``; each plugin ships its
own branch (``migrations_dir``, see ``pairly_core.plugins``), added here before Alembic
reads any revision. So there are several heads: always ``upgrade heads``. Commands that
do not run this file (``alembic heads``, ``history``) only see core's chain.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import pairly_core.models  # noqa: F401  (registers core tables on Base.metadata)
from pairly_core.config import to_asyncpg_url
from pairly_core.db import Base
from pairly_core.plugins import discover_plugins, plugin_migration_locations

# Importing a plugin imports its table definitions (they register on Base.metadata).
_PLUGINS = discover_plugins()

OWNER_URL_ENV = "PAIRLY_DATABASE_URL_OWNER"

config = context.config
target_metadata = Base.metadata


def _add_plugin_versions() -> None:
    """Core's versions directory plus every plugin's, before revisions are loaded."""
    script = context.script
    script.version_locations = [
        str(Path(script.dir) / "versions"),
        *plugin_migration_locations(_PLUGINS.values()),
    ]
    # Forget any locations computed from the config alone (memoized by Alembic).
    for name in ("_version_locations", "versions"):
        script.__dict__.pop(name, None)


_add_plugin_versions()


def _owner_url() -> str:
    url = config.attributes.get("database_url") or os.environ.get(OWNER_URL_ENV)
    if not url:
        raise SystemExit(f"missing required setting: {OWNER_URL_ENV} (migration owner role URL)")
    return to_asyncpg_url(url)


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_owner_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported; run against a database")

asyncio.run(_run_async())
