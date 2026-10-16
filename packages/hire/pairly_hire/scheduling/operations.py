# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""A journal of writes to external calendars and meeting providers (orphan events).

An interview change runs in one database transaction, but a write to Google, Microsoft
or Zoom happens at once and cannot roll back. If the transaction fails after the
provider wrote (a commit failure, a crash, a later error in the same request), the
provider keeps an event or meeting that Pairly does not know about (an orphan), or a
change Pairly does not have. So every external write is journalled:

1. ``begin`` records a ``pending`` row in ``hire_calendar_operations`` in its own short
   transaction, committed before the call: provider, kind (``event`` or ``meeting``),
   action, the interview, the organiser, our idempotency key (``client_request_id``:
   Google uses it as the event id, Microsoft as ``transactionId``), the provider's id
   when known (always for updates and cancels; for a Google create it is the key), and
   the intended state.
2. The provider is called. ``written`` stores the provider's id when it was not known
   (a Microsoft or Zoom create), again in its own transaction; ``failed`` stores the
   error and whether the provider refused (nothing written).
3. ``settle`` marks the row ``done`` in the interview's own transaction, so the mark
   commits if and only if the change does. It holds the row's lock until then, so the
   reconciliation job (``reconcile``) never touches a change still in flight.

The fake calendar writes through the same database transaction, so it is not journalled.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.integrations.calendar.base import CalendarError
from pairly_hire.scheduling.models import (
    HireCalendarOperation,
    HireInterview,
    OperationStatus,
)

LOCAL_PROVIDERS: Final = frozenset({"fake"})


@dataclass(frozen=True, slots=True)
class Operation:
    id: uuid.UUID
    org_id: uuid.UUID
    client_request_id: str


def journalled(provider: str) -> bool:
    return provider not in LOCAL_PROVIDERS


def _side(session: AsyncSession, org_id: uuid.UUID) -> AsyncSession:
    """A second session on the same database, outside the caller's transaction."""
    bind = session.bind
    if not isinstance(bind, AsyncEngine):  # pragma: no cover - sessions bind an engine
        raise RuntimeError("the calendar journal needs a session bound to an engine")
    side = create_sessionmaker(bind)()
    set_tenant(side, org_id, None)
    return side


async def begin(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    interview_id: uuid.UUID,
    organizer_id: uuid.UUID,
    provider: str,
    kind: str,
    action: str,
    external_id: str | None,
    intended: dict[str, Any],
) -> Operation:
    """Record the write before it happens (committed on its own)."""
    key = uuid.uuid4().hex
    if external_id is None and provider == "google" and kind == "event" and action == "create":
        external_id = key  # Google takes our id: known before the call
    now = datetime.now(UTC)
    row_id = uuid.uuid4()
    side = _side(session, org_id)
    async with side, side.begin():
        side.add(
            HireCalendarOperation(
                id=row_id,
                org_id=org_id,
                interview_id=interview_id,
                organizer_id=organizer_id,
                provider=provider,
                kind=kind,
                action=action,
                client_request_id=key,
                external_id=external_id[:256] if external_id else None,
                intended=intended,
                status=OperationStatus.PENDING.value,
                created_at=now,
                updated_at=now,
            )
        )
    return Operation(row_id, org_id, key)


async def _side_update(session: AsyncSession, op: Operation, values: dict[str, Any]) -> None:
    side = _side(session, op.org_id)
    async with side, side.begin():
        await side.execute(
            update(HireCalendarOperation)
            .where(HireCalendarOperation.id == op.id)
            .values(**values, updated_at=datetime.now(UTC))
        )


async def written(session: AsyncSession, op: Operation, external_id: str) -> None:
    """The provider's id of what it just made (committed on its own)."""
    await _side_update(session, op, {"external_id": external_id[:256]})


async def failed(session: AsyncSession, op: Operation, exc: CalendarError) -> None:
    """The call failed: refused (nothing written) or unknown (maybe written)."""
    await _side_update(session, op, {"last_error": exc.code[:64], "rejected": not exc.transient})


async def settle(session: AsyncSession, op: Operation) -> None:
    """Mark the operation done in the caller's transaction (commits with the change)."""
    now = datetime.now(UTC)
    result = await session.execute(
        update(HireCalendarOperation)
        .where(
            HireCalendarOperation.id == op.id,
            HireCalendarOperation.status == OperationStatus.PENDING.value,
        )
        .values(status=OperationStatus.DONE.value, outcome="committed", resolved_at=now)
        .returning(HireCalendarOperation.id)
    )
    if result.scalar_one_or_none() is None:
        # Not visible or already settled: fail closed, the change rolls back and the
        # reconciliation job sees the pending row.
        raise RuntimeError("the calendar operation could not be settled")


async def needing_review(session: AsyncSession) -> int:
    """Operations the reconciliation job marked ``failed`` (a person checks them)."""
    found = await session.scalar(
        select(func.count())
        .select_from(HireCalendarOperation)
        .where(HireCalendarOperation.status == OperationStatus.FAILED.value)
    )
    return int(found or 0)


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """A failed external write, with where to look (the interview's application)."""

    id: uuid.UUID
    provider: str
    kind: str
    action: str
    rejected: bool
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    interview_id: uuid.UUID
    job_id: uuid.UUID | None
    application_id: uuid.UUID | None


async def review_items(session: AsyncSession, *, limit: int = 50) -> list[ReviewItem]:
    """The operations a person should check, newest first (ids and codes only: never the
    intended state, which may hold a meeting link)."""
    rows = await session.execute(
        select(HireCalendarOperation, HireInterview.job_id, HireInterview.application_id)
        .outerjoin(HireInterview, HireInterview.id == HireCalendarOperation.interview_id)
        .where(HireCalendarOperation.status == OperationStatus.FAILED.value)
        .order_by(HireCalendarOperation.updated_at.desc(), HireCalendarOperation.id)
        .limit(limit)
    )
    return [
        ReviewItem(
            id=op.id,
            provider=op.provider,
            kind=op.kind,
            action=op.action,
            rejected=op.rejected,
            attempts=op.attempts,
            last_error=op.last_error,
            created_at=op.created_at,
            updated_at=op.updated_at,
            interview_id=op.interview_id,
            job_id=job_id,
            application_id=application_id,
        )
        for op, job_id, application_id in rows.all()
    ]
