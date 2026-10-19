# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Tenant-bound LangGraph checkpointer (``langgraph-checkpoint-postgres``).

The stock ``AsyncPostgresSaver`` tables have no organisation column and the saver runs
its own SQL on a psycopg connection, so on their own they would let any code that holds
a thread id read or overwrite another tenant's run state (graph state can hold personal
data). Tenant separation is enforced in four layers:

1. Dedicated schema. The checkpoint tables live in ``lg_checkpoints`` (migration 0006),
   created by Alembic from the package's own DDL. The saver's ``setup()`` is never run
   at runtime and the application role cannot create tables. The version of the
   package's DDL is checked at startup (``check_schema``), so an upgrade that needs a
   new checkpoint migration fails closed instead of running against an old schema.
2. Row Level Security. Each table gets ``org_id uuid NOT NULL DEFAULT
   app_current_org_id()``, RLS forced, and the standard tenant policy. A check
   constraint ties the thread id to that column: ``split_part(thread_id, ':', 1) =
   org_id::text``. The saver's INSERTs do not name ``org_id``, so it takes the
   connection's organisation; reads only ever see that organisation's rows.
3. One connection per organisation and invocation. ``TenantCheckpointer.bind(org)``
   opens a dedicated psycopg connection as the application role (no BYPASSRLS), sets
   ``app.org_id`` for the whole session on it, hands it to the saver and closes it
   afterwards. It never returns to a pool, so the setting cannot leak to another tenant.
4. Thread ids embed the organisation (``"<org_id>:<run_id>"``, see
   ``pairly_core.workflow.models``) and ``OrgBoundSaver`` refuses any call whose thread
   id belongs to another organisation before any SQL runs. The runtime also loads the
   run under normal RLS and checks its thread id before every start or resume.

Checkpoint data is deserialised in LangGraph's strict mode (safe types only).

Deployment note: session-level settings need session pooling. Behind a transaction-mode
pooler (Supavisor on port 6543, PgBouncer), point the checkpointer at a session-mode
pool or a direct connection: ``PAIRLY_CHECKPOINT_DATABASE_URL``, required by the
settings when ``PAIRLY_DATABASE_POOLER=transaction`` (``pairly_core.dbconn``). The search
path is set with a statement after connecting, not a startup option, so poolers that
drop startup parameters still put the saver in ``lg_checkpoints``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.postgres.base import BasePostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from sqlalchemy.engine import make_url

from pairly_core.db import TenantError

CHECKPOINT_SCHEMA = "lg_checkpoints"
# Highest migration of langgraph-checkpoint-postgres that migration 0006 applied.
APPLIED_CHECKPOINT_MIGRATION = 9


class CheckpointSchemaError(RuntimeError):
    """The checkpoint schema does not match the installed langgraph-checkpoint-postgres."""


def psycopg_conninfo(database_url: str) -> str:
    """A libpq URL from a SQLAlchemy URL (``postgresql+asyncpg://`` included).

    asyncpg's ``ssl=`` (what ``to_asyncpg_url`` makes of ``sslmode=``) goes back to
    libpq's ``sslmode=``; libpq would refuse ``ssl`` as an unknown option.
    """
    url = make_url(database_url).set(drivername="postgresql")
    if "ssl" in url.query:
        mode = url.query["ssl"]
        url = url.difference_update_query(["ssl"])
        if "sslmode" not in url.query:
            url = url.update_query_dict({"sslmode": mode if isinstance(mode, str) else mode[0]})
    return url.render_as_string(hide_password=False)


def org_of_thread(thread_id: str) -> uuid.UUID | None:
    prefix, sep, rest = thread_id.partition(":")
    if not sep or not rest:
        return None
    try:
        return uuid.UUID(prefix)
    except ValueError:
        return None


class OrgBoundSaver(AsyncPostgresSaver):
    """``AsyncPostgresSaver`` that refuses thread ids of another organisation."""

    def __init__(self, conn: AsyncConnection[DictRow], org_id: uuid.UUID) -> None:
        # Strict deserialisation: only LangGraph's allowlist of safe types, so even a
        # row written straight into the database cannot make loading run code.
        super().__init__(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=None))
        self.org_id = org_id
        # The organisation-bound connection (typed; the base class also accepts pools).
        self.connection = conn

    def _check(self, config: RunnableConfig | None) -> None:
        configurable = (config or {}).get("configurable") or {}
        thread_id = configurable.get("thread_id")
        if not isinstance(thread_id, str) or org_of_thread(thread_id) != self.org_id:
            raise TenantError("checkpoint access outside the bound organisation")

    async def setup(self) -> None:
        raise CheckpointSchemaError("checkpoint tables are created by Alembic (migration 0006)")

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        self._check(config)
        return await super().aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        self._check(config)
        async for item in super().alist(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self._check(config)
        return await super().aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._check(config)
        await super().aput_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        if org_of_thread(thread_id) != self.org_id:
            raise TenantError("checkpoint access outside the bound organisation")
        await super().adelete_thread(thread_id)


class TenantCheckpointer:
    """Opens organisation-bound checkpoint savers. See the module docstring."""

    def __init__(self, database_url: str) -> None:
        self._conninfo = psycopg_conninfo(database_url)

    async def _connect(self) -> AsyncConnection[DictRow]:
        conn = await AsyncConnection.connect(
            self._conninfo,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        )
        try:
            # Session-level, on a connection this saver alone uses (never pooled here).
            await conn.execute("SELECT set_config('search_path', %s, false)", (CHECKPOINT_SCHEMA,))
        except BaseException:
            await conn.close()
            raise
        return conn

    @asynccontextmanager
    async def bind(self, org_id: uuid.UUID) -> AsyncIterator[OrgBoundSaver]:
        if not isinstance(org_id, uuid.UUID):
            raise TenantError("org_id must be a UUID")
        conn = await self._connect()
        try:
            await conn.execute(
                "SELECT set_config('app.org_id', %s, false), "
                "set_config('app.auth_subject', '', false)",
                (str(org_id),),
            )
            yield OrgBoundSaver(conn, org_id)
        finally:
            await conn.close()

    async def check_schema(self) -> None:
        """Fail closed when the installed package expects checkpoint migrations we lack."""
        expected = len(BasePostgresSaver.MIGRATIONS) - 1
        if expected != APPLIED_CHECKPOINT_MIGRATION:
            raise CheckpointSchemaError(
                f"langgraph-checkpoint-postgres expects checkpoint migration {expected}, "
                f"Pairly migrations apply {APPLIED_CHECKPOINT_MIGRATION}: add an Alembic migration"
            )
        conn = await self._connect()
        try:
            cursor = await conn.execute("SELECT max(v) AS v FROM checkpoint_migrations")
            row = await cursor.fetchone()
        finally:
            await conn.close()
        if row is None or row["v"] != APPLIED_CHECKPOINT_MIGRATION:
            raise CheckpointSchemaError("the checkpoint schema is missing or out of date")
