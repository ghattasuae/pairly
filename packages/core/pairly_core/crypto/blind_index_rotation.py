# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Blind index key rotation: recompute stored indexes under the current key.

Run as ``pairly-admin crypto rotate-blind-index`` (owner database role), after the key
rollout described in ``pairly_core.crypto.blind_index`` (new key added and made
current, every process restarted). For every organisation and every declared blind
index column (``blind_index_info``), in batches:

1. read rows whose index is set, decrypt the encrypted source value (bound to the
   row, as always), compute the index under the current key;
2. rows already holding it are left alone (so the run is resumable and idempotent);
3. others are updated only if they still hold the index that was read (no lost
   writes); a row whose source cannot be decrypted, is empty, or no longer
   normalises is counted as failed and left as is;
4. each batch is one transaction with one ``core.crypto.blind_index_rewritten`` audit
   event on that organisation's chain.

``dry_run`` counts the rows still to rewrite and changes nothing. When it reports
nothing left, the old key can be removed. Lookups match every configured key during
the whole rotation, so no lookup misses a row at any point.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import MetaData, Text, select, type_coerce, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.blind_index import BlindIndexColumn, blind_index, blind_index_columns
from pairly_core.crypto.envelope import Aad, DecryptionError, decrypt
from pairly_core.crypto.keys import KeyRing, get_keyring
from pairly_core.crypto.rotation import crypto_org_ids
from pairly_core.db import set_tenant

REWRITTEN_ACTION: Final = "core.crypto.blind_index_rewritten"
DEFAULT_BATCH: Final = 500


@dataclass
class BlindIndexRotationReport:
    current: str
    orgs: int = 0
    # "<table>.<column>" -> rows
    rewritten: dict[str, int] = field(default_factory=dict)
    pending: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)

    @property
    def total_rewritten(self) -> int:
        return sum(self.rewritten.values())

    @property
    def total_failed(self) -> int:
        return sum(self.failed.values())


def _current_index(
    col: BlindIndexColumn, org_id: uuid.UUID, row_id: object, token: object, ring: KeyRing
) -> str | None:
    """The index the row should hold, or None when it cannot be computed (failed)."""
    if token is None:
        return None
    aad = Aad.for_row(org_id, col.table.name, col.source, row_id)
    try:
        value = decrypt(str(token), aad, ring).decode("utf-8")
    except (DecryptionError, UnicodeDecodeError):
        return None
    return blind_index(col.kind, value, org_id, ring)


async def _rotate_column(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    col: BlindIndexColumn,
    ring: KeyRing,
    *,
    batch_size: int,
    dry_run: bool,
    actor_id: str,
    report: BlindIndexRotationReport,
) -> None:
    key = f"{col.table.name}.{col.column}"
    table = col.table
    index_col = table.c[col.column]
    source = type_coerce(table.c[col.source], Text)
    after: uuid.UUID | None = None
    while True:
        session = sessionmaker()
        set_tenant(session, org_id, None)
        async with session, session.begin():
            query = (
                select(table.c.id, index_col, source)
                .where(table.c.org_id == org_id, index_col.is_not(None))
                .order_by(table.c.id)
                .limit(batch_size)
            )
            if after is not None:
                query = query.where(table.c.id > after)
            rows = (await session.execute(query)).all()
            if not rows:
                return
            after = rows[-1][0]
            done = failed = stale = 0
            for row_id, stored, token in rows:
                fresh = _current_index(col, org_id, row_id, token, ring)
                if fresh is None:
                    failed += 1
                    continue
                if fresh == stored:
                    continue
                stale += 1
                if dry_run:
                    continue
                result = await session.execute(
                    update(table)
                    .where(table.c.id == row_id, table.c.org_id == org_id, index_col == stored)
                    .values({col.column: fresh})
                )
                done += int(getattr(result, "rowcount", 0) or 0)
            if dry_run:
                if stale:
                    report.pending[key] = report.pending.get(key, 0) + stale
            elif done or failed:
                await append_audit(
                    session,
                    action=REWRITTEN_ACTION,
                    actor_type=ActorType.SYSTEM,
                    actor_id=actor_id,
                    entity_type="crypto_key",
                    entity_id=f"blind_index:{ring.blind_index_current}",
                    payload={
                        "table": table.name,
                        "column": col.column,
                        "rewritten": done,
                        "failed": failed,
                        "blind_index_version": ring.blind_index_current,
                    },
                )
        if done:
            report.rewritten[key] = report.rewritten.get(key, 0) + done
        if failed:
            report.failed[key] = report.failed.get(key, 0) + failed


async def rotate_blind_indexes(
    sessionmaker: async_sessionmaker[AsyncSession],
    metadata: MetaData,
    *,
    keyring: KeyRing | None = None,
    org_ids: Sequence[uuid.UUID] | None = None,
    batch_size: int = DEFAULT_BATCH,
    dry_run: bool = False,
    actor_id: str = "pairly.admin",
) -> BlindIndexRotationReport:
    """Rewrite every declared blind index under the current key (or count, ``dry_run``)."""
    if not 1 <= batch_size <= 10_000:
        raise ValueError("batch_size must be 1..10000")
    ring = keyring or get_keyring()
    columns = list(blind_index_columns(metadata))
    orgs = list(org_ids) if org_ids is not None else await crypto_org_ids(sessionmaker)
    report = BlindIndexRotationReport(current=ring.blind_index_current, orgs=len(orgs))
    for org_id in orgs:
        for col in columns:
            await _rotate_column(
                sessionmaker,
                org_id,
                col,
                ring,
                batch_size=batch_size,
                dry_run=dry_run,
                actor_id=actor_id,
                report=report,
            )
    return report
