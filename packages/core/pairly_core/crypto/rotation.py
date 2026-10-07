# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""KEK rotation: re-wrap every data key that is not on the current KEK.

Run as ``pairly-admin crypto rotate`` (owner database role). Steps for an operator:

1. Add the new key as ``PAIRLY_KEK_<new>`` and keep the old one; set
   ``PAIRLY_KEK_CURRENT=<new>``; restart the API and worker (new rows use the new KEK).
2. Run ``pairly-admin crypto rotate``. It walks every organisation (from
   ``crypto_org_ids()``, migration 0008) and every encrypted column, in batches.
   Each batch is one transaction that re-wraps the rows and appends one
   ``core.crypto.rewrapped`` audit event to that organisation's chain.
3. When ``pairly-admin crypto rotate --dry-run`` reports nothing left, remove the old KEK.

Resumable: rows already on the current KEK are skipped (their token starts with
``v1:<current>:``), so an interrupted run simply continues. Only the wrapped data key
changes; the value ciphertext and its AAD binding stay the same. A row that cannot be
decrypted (tampered, or an unknown KEK version) is counted as failed and left as is.
An update only applies if the row still holds the token that was read (no lost writes).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import MetaData, Text, func, not_, select, text, type_coerce, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.envelope import Aad, DecryptionError, current_prefix, rewrap
from pairly_core.crypto.keys import KeyRing, get_keyring
from pairly_core.crypto.types import EncryptedColumn, Sealed, encrypted_columns
from pairly_core.db import set_tenant

REWRAPPED_ACTION: Final = "core.crypto.rewrapped"
DEFAULT_BATCH = 500


@dataclass
class RotationReport:
    current: str
    orgs: int = 0
    # "<table>.<column>" -> rows
    rewrapped: dict[str, int] = field(default_factory=dict)
    pending: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)

    @property
    def total_rewrapped(self) -> int:
        return sum(self.rewrapped.values())

    @property
    def total_failed(self) -> int:
        return sum(self.failed.values())


async def crypto_org_ids(sessionmaker: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    """Every organisation id (owner role only; see migration 0008)."""
    async with sessionmaker() as session, session.begin():
        rows = await session.execute(text("SELECT org_id FROM crypto_org_ids() AS org_id"))
        return [uuid.UUID(str(r[0])) for r in rows]


def _stale(col: EncryptedColumn, prefix: str) -> list[ColumnElement[bool]]:
    raw = type_coerce(col.table.c[col.column], Text)
    return [raw.is_not(None), not_(raw.startswith(prefix, autoescape=True))]


async def _rotate_column(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    col: EncryptedColumn,
    ring: KeyRing,
    *,
    batch_size: int,
    actor_id: str,
    report: RotationReport,
) -> None:
    key = f"{col.table.name}.{col.column}"
    table = col.table
    raw = type_coerce(table.c[col.column], Text)
    prefix = current_prefix(ring)
    after: uuid.UUID | None = None
    while True:
        session = sessionmaker()
        set_tenant(session, org_id, None)
        async with session, session.begin():
            query = (
                select(table.c.id, raw)
                .where(table.c.org_id == org_id, *_stale(col, prefix))
                .order_by(table.c.id)
                .limit(batch_size)
            )
            if after is not None:
                query = query.where(table.c.id > after)
            rows = (await session.execute(query)).all()
            if not rows:
                return
            after = rows[-1][0]
            done = failed = 0
            for row_id, token in rows:
                aad = Aad.for_row(org_id, table.name, col.column, row_id)
                try:
                    new_token = rewrap(str(token), aad, ring)
                except DecryptionError:
                    failed += 1
                    continue
                result = await session.execute(
                    update(table)
                    .where(table.c.id == row_id, table.c.org_id == org_id, raw == token)
                    .values({col.column: Sealed(new_token)})
                )
                done += int(getattr(result, "rowcount", 0) or 0)
            await append_audit(
                session,
                action=REWRAPPED_ACTION,
                actor_type=ActorType.SYSTEM,
                actor_id=actor_id,
                entity_type="crypto_key",
                entity_id=ring.current,
                payload={
                    "table": table.name,
                    "column": col.column,
                    "rewrapped": done,
                    "failed": failed,
                    "kek_version": ring.current,
                },
            )
        report.rewrapped[key] = report.rewrapped.get(key, 0) + done
        if failed:
            report.failed[key] = report.failed.get(key, 0) + failed


async def _count_pending(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    col: EncryptedColumn,
    prefix: str,
) -> int:
    session = sessionmaker()
    set_tenant(session, org_id, None)
    async with session, session.begin():
        query = (
            select(func.count())
            .select_from(col.table)
            .where(col.table.c.org_id == org_id, *_stale(col, prefix))
        )
        return int((await session.execute(query)).scalar_one())


async def rotate_keys(
    sessionmaker: async_sessionmaker[AsyncSession],
    metadata: MetaData,
    *,
    keyring: KeyRing | None = None,
    org_ids: Sequence[uuid.UUID] | None = None,
    batch_size: int = DEFAULT_BATCH,
    dry_run: bool = False,
    actor_id: str = "pairly.admin",
) -> RotationReport:
    """Re-wrap stale data keys in every encrypted column (or count them with ``dry_run``)."""
    if not 1 <= batch_size <= 10_000:
        raise ValueError("batch_size must be 1..10000")
    ring = keyring or get_keyring()
    columns = list(encrypted_columns(metadata))
    orgs = list(org_ids) if org_ids is not None else await crypto_org_ids(sessionmaker)
    report = RotationReport(current=ring.current, orgs=len(orgs))
    prefix = current_prefix(ring)
    for org_id in orgs:
        for col in columns:
            if dry_run:
                pending = await _count_pending(sessionmaker, org_id, col, prefix)
                if pending:
                    key = f"{col.table.name}.{col.column}"
                    report.pending[key] = report.pending.get(key, 0) + pending
                continue
            await _rotate_column(
                sessionmaker,
                org_id,
                col,
                ring,
                batch_size=batch_size,
                actor_id=actor_id,
                report=report,
            )
    return report
