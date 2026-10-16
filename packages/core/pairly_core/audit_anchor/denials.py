# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Rate limit for ``core.authz.denied`` audit events, per actor.

Every refused ``authorize`` call used to append one audit event. A script hammering a
forbidden endpoint would then flood the chain. Each actor now has a token bucket in
``audit_denial_limits`` (Postgres, row locked, so every API process shares it):

* ``CAPACITY`` events in a burst, refilled at ``REFILL_PER_SECOND``.
* A denial that finds a token is audited as before.
* A denial that finds none is counted (``suppressed``), with when the run started and
  which permissions were asked for. Nothing is dropped silently: the next denial that
  finds a token, or the worker's flush job (``flush_suppressed``), first writes one
  ``core.authz.denials_suppressed`` summary event with the count.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, AuditAction, actor_ref, append_audit, record_denial
from pairly_core.audit_anchor.models import AuditDenialLimit
from pairly_core.db import TenantError, current_org
from pairly_core.rbac import Denial, PermissionDeniedError

DENIALS_SUPPRESSED: Final = AuditAction.AUTHZ_DENIALS_SUPPRESSED
CAPACITY: Final = 20.0
REFILL_PER_SECOND: Final = 0.2
MAX_PERMISSIONS: Final = 10
FLUSH_AFTER: Final = timedelta(minutes=1)


def _actor_parts(actor_key: str) -> tuple[ActorType, str]:
    kind, _, actor_id = actor_key.partition(":")
    return ActorType(kind), actor_id


async def _write_summary(session: AsyncSession, row: AuditDenialLimit, now: datetime) -> None:
    kind, actor_id = _actor_parts(row.actor_key)
    since = row.suppressed_since or now
    await append_audit(
        session,
        action=DENIALS_SUPPRESSED,
        actor_type=kind,
        actor_id=actor_id,
        payload={
            "count": int(row.suppressed),
            "since": since.astimezone(UTC).isoformat(),
            "until": now.astimezone(UTC).isoformat(),
            "permissions": list(row.suppressed_permissions)[:MAX_PERMISSIONS],
        },
    )
    row.suppressed = 0
    row.suppressed_since = None
    row.suppressed_permissions = []


async def _bucket(
    session: AsyncSession, org: uuid.UUID, actor_key: str, now: datetime, capacity: float
) -> AuditDenialLimit:
    await session.execute(
        pg_insert(AuditDenialLimit)
        .values(org_id=org, actor_key=actor_key, tokens=capacity, updated_at=now)
        .on_conflict_do_nothing(constraint="uq_audit_denial_limits_actor")
    )
    row = await session.scalar(
        select(AuditDenialLimit).where(AuditDenialLimit.actor_key == actor_key).with_for_update()
    )
    if row is None:  # pragma: no cover - RLS would have refused the insert
        raise TenantError("denial bucket not visible")
    return row


async def record_denial_limited(
    session: AsyncSession,
    denial: Denial | PermissionDeniedError,
    *,
    capacity: float = CAPACITY,
    refill_per_second: float = REFILL_PER_SECOND,
    now: datetime | None = None,
) -> bool:
    """Audit a denial if the actor's bucket has a token. Returns whether it was written.

    Runs in the session's current transaction (``pairly_core.identity`` opens a fresh
    one after rolling back the denied work).
    """
    info = denial.denial if isinstance(denial, PermissionDeniedError) else denial
    if info is None:
        return False
    org = current_org(session)
    if org is None or org != info.actor.org_id:
        raise TenantError("a denial is audited in the actor's own organisation")
    kind, actor_id = actor_ref(info.actor)
    moment = now or datetime.now(UTC)
    row = await _bucket(session, org, f"{kind.value}:{actor_id}", moment, capacity)
    elapsed = max((moment - row.updated_at).total_seconds(), 0.0)
    tokens = min(capacity, float(row.tokens) + elapsed * refill_per_second)
    row.updated_at = moment
    if tokens < 1.0:
        row.tokens = tokens
        row.suppressed = int(row.suppressed) + 1
        row.suppressed_since = row.suppressed_since or moment
        permissions = list(row.suppressed_permissions)
        if info.permission not in permissions and len(permissions) < MAX_PERMISSIONS:
            row.suppressed_permissions = [*permissions, info.permission]
        await session.flush()
        return False
    row.tokens = tokens - 1.0
    if row.suppressed:
        await _write_summary(session, row, moment)
    await session.flush()
    await record_denial(session, info)
    return True


async def flush_suppressed(
    session: AsyncSession, *, older_than: timedelta = FLUSH_AFTER, now: datetime | None = None
) -> int:
    """Write the summary event of every bucket of the session's organisation whose last
    suppressed denial is older than ``older_than``. Returns how many were written."""
    moment = now or datetime.now(UTC)
    rows = (
        await session.scalars(
            select(AuditDenialLimit)
            .where(
                AuditDenialLimit.suppressed > 0,
                AuditDenialLimit.updated_at < moment - older_than,
            )
            .order_by(AuditDenialLimit.updated_at)
            .limit(500)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for row in rows:
        await _write_summary(session, row, moment)
    await session.flush()
    return len(rows)


def bucket_snapshot(row: AuditDenialLimit) -> dict[str, Any]:
    return {
        "actor_key": row.actor_key,
        "tokens": row.tokens,
        "suppressed": row.suppressed,
        "permissions": list(row.suppressed_permissions),
    }
