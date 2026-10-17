# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Audit chain verification as a worker job with a result row.

``GET /v1/audit/verify`` verifies small chains in the request. A chain longer than
``LARGE_CHAIN_EVENTS`` is verified by the worker instead: the API inserts an
``audit_verifications`` row (``pending``), queues ``verify_audit_chain`` and returns the
row id; the client polls ``GET /v1/audit/verify/{id}``. A lost job is picked up by the
worker's sweeper (``decision_jobs_pending_org_ids``).

``run_verification`` claims the row, verifies the chain against the anchored heads,
stores the result and, when the chain is broken, alerts and appends
``core.audit.chain_verification_failed`` to the chain, like the nightly job.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.audit import (
    ActorType,
    AuditAction,
    ChainVerification,
    append_audit,
    verify_chain,
)
from pairly_core.audit_anchor.models import AuditVerification
from pairly_core.audit_anchor.signing import AnchorError
from pairly_core.db import set_tenant
from pairly_core.models.audit import AuditEvent

log = structlog.get_logger("pairly_core.audit_anchor.verification")

LARGE_CHAIN_EVENTS: Final = 50_000
VERIFY_JOB: Final = "verify_audit_chain"
VERIFY_REQUESTED: Final = "core.audit.verification_requested"
SYSTEM_ACTOR_ID: Final = "pairly.worker.audit_verify"
MAX_ATTEMPTS: Final = 3
# A job nobody finished after this long is picked up again by the sweeper.
STALE_AFTER: Final = timedelta(minutes=2)

# org id -> anchored heads (seq -> hash); raises AnchorError when the anchors are bad.
AnchorLoader = Callable[[uuid.UUID], Awaitable[Mapping[int, str]]]


async def chain_length(session: AsyncSession) -> int:
    """Events in the session organisation's chain (its last ``seq``)."""
    value = await session.scalar(select(func.max(AuditEvent.seq)))
    return int(value or 0)


async def request_verification(
    session: AsyncSession, *, org_id: uuid.UUID, actor_type: ActorType, actor_id: str
) -> uuid.UUID:
    verification_id = uuid.uuid4()
    session.add(
        AuditVerification(
            id=verification_id,
            org_id=org_id,
            requested_by_type=actor_type.value,
            requested_by_id=actor_id,
            status="pending",
        )
    )
    await session.flush()
    await append_audit(
        session,
        action=VERIFY_REQUESTED,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="audit_verification",
        entity_id=verification_id,
    )
    return verification_id


async def record_broken_chain(session: AsyncSession, result: ChainVerification) -> None:
    log.error(
        "audit_chain_alert",
        alert="audit_chain_broken",
        org_id=str(result.org_id),
        broken_seq=result.broken_seq,
        reason=result.reason,
        checked=result.checked,
    )
    await append_audit(
        session,
        action=AuditAction.AUDIT_CHAIN_BROKEN,
        actor_type=ActorType.SYSTEM,
        actor_id=SYSTEM_ACTOR_ID,
        entity_type="audit_chain",
        entity_id=str(result.org_id),
        payload={
            "broken_seq": result.broken_seq,
            "reason": result.reason,
            "checked": result.checked,
        },
    )


async def verify_with_anchors(
    session: AsyncSession, org_id: uuid.UUID, anchors: AnchorLoader | None
) -> tuple[ChainVerification, dict[str, Any]]:
    """``verify_chain`` with the organisation's anchors. Bad anchors fail closed."""
    heads: Mapping[int, str] = {}
    info: dict[str, Any] = {"anchors_checked": anchors is not None, "anchored_seq": None}
    if anchors is not None:
        try:
            heads = await anchors(org_id)
        except AnchorError as exc:
            info["anchor_error"] = str(exc)[:120]
            last = await chain_length(session)
            broken = ChainVerification(
                org_id=org_id,
                ok=False,
                checked=0,
                last_seq=last,
                last_hash="",
                broken_seq=None,
                reason="anchor_invalid",
            )
            return broken, info
        info["anchored_seq"] = max(heads) if heads else None
    return await verify_chain(session, org_id, anchors=heads), info


async def run_verification(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    verification_id: uuid.UUID,
    *,
    anchors: AnchorLoader | None = None,
) -> dict[str, Any]:
    """Claim, run and store one verification. Idempotent: a finished row is left as is."""
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            claimed = await session.execute(
                update(AuditVerification)
                .where(
                    AuditVerification.id == verification_id,
                    AuditVerification.status.in_(["pending", "running"]),
                    AuditVerification.attempts < MAX_ATTEMPTS,
                )
                .values(
                    status="running",
                    attempts=AuditVerification.attempts + 1,
                    started_at=datetime.now(UTC),
                )
                .returning(AuditVerification.id)
            )
            if claimed.first() is None:
                return {"status": "skipped"}
        try:
            async with session.begin():
                result, info = await verify_with_anchors(session, org_id, anchors)
        except Exception as exc:
            async with session.begin():
                await session.execute(
                    update(AuditVerification)
                    .where(AuditVerification.id == verification_id)
                    .values(
                        status="failed",
                        error=f"verification_error:{type(exc).__name__}"[:128],
                        completed_at=datetime.now(UTC),
                    )
                )
            log.error("audit_verification_failed", org_id=str(org_id), error=type(exc).__name__)
            return {"status": "failed"}
        async with session.begin():
            await session.execute(
                update(AuditVerification)
                .where(AuditVerification.id == verification_id)
                .values(
                    status="done",
                    result=result.to_json() | info,
                    completed_at=datetime.now(UTC),
                )
            )
            if not result.ok:
                await record_broken_chain(session, result)
    return {"status": "done", "ok": result.ok}


async def get_verification(
    session: AsyncSession, verification_id: uuid.UUID
) -> AuditVerification | None:
    return await session.scalar(
        select(AuditVerification).where(AuditVerification.id == verification_id)
    )


async def stale_verifications(session: AsyncSession) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(AuditVerification.id)
        .where(
            AuditVerification.status.in_(["pending", "running"]),
            AuditVerification.attempts < MAX_ATTEMPTS,
            func.coalesce(AuditVerification.started_at, AuditVerification.created_at)
            < func.now() - STALE_AFTER,
        )
        .order_by(AuditVerification.created_at)
        .limit(20)
    )
    return list(rows)


async def give_up_verifications(session: AsyncSession) -> int:
    """Rows that used every attempt are failed (never left pending forever)."""
    result = await session.execute(
        update(AuditVerification)
        .where(
            AuditVerification.status.in_(["pending", "running"]),
            AuditVerification.attempts >= MAX_ATTEMPTS,
        )
        .values(status="failed", error="attempts_exhausted", completed_at=datetime.now(UTC))
        .returning(AuditVerification.id)
    )
    return len(result.all())
