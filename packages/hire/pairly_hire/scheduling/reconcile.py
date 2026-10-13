# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Settle external calendar writes whose transaction never committed (``operations``).

``run_reconcile_job`` (``hire_calendar_reconcile``, every 5 minutes, worker): per
organisation, every ``pending`` operation not touched for ``STALE`` (its transaction is
over: the interview's transaction marks it ``done`` and holds its lock until commit;
rows are taken ``FOR UPDATE SKIP LOCKED``, and an interview still locked is skipped):

* **Nothing written**: the provider refused the call and gave no id. ``reconciled``.
* **Orphan**: the provider's object is not the interview's (the interview was never
  committed, or it points at another event or meeting). It is cancelled or deleted
  through the organiser's provider (404 counts as gone). ``reconciled``.
* **Lost change**: it is the interview's own object, but the change did not commit
  (a move or a cancel that rolled back). The interview's committed state is written
  again (``service._write_event``, itself journalled); an event deleted by a lost
  cancel is made again. Only the newest operation of an interview does this; older ones
  are ``superseded``.
* **Unknown**: the call may have written but its id is unknown (a crash between the
  call and recording the id of a Microsoft or Zoom create). ``failed`` for a person to
  check; it cannot be found safely.

A transient provider error counts an attempt and tries again next run; after
``MAX_ATTEMPTS``, or on a permanent error (the organiser disconnected, Zoom removed),
the operation is ``failed``. Every outcome is audited (``hire.calendar.reconciled`` or
``hire.calendar.reconcile_failed``) with the provider, kind, action and outcome, never
the meeting link.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.db import set_tenant
from pairly_core.errors import NotFoundError, PairlyError
from pairly_core.integrations.calendar.base import (
    CalendarError,
    CalendarUser,
    EventRef,
    EventSpec,
)
from pairly_core.jobs.registry import JobContext
from pairly_hire.pipeline.records import load_record
from pairly_hire.pipeline.rediscovery import org_ids
from pairly_hire.scheduling import meetings
from pairly_hire.scheduling.models import (
    ACTIVE,
    HireCalendarOperation,
    HireInterview,
    InterviewStatus,
    OperationStatus,
)
from pairly_hire.scheduling.providers import Person, calendar_set, google_client
from pairly_hire.scheduling.rules import interviewers, org_scheduling
from pairly_hire.scheduling.service import (
    CalendarUnavailableError,
    _write_event,
    panel_of,
    panel_people,
)

log = structlog.get_logger("pairly_hire.scheduling.reconcile")

JOB_NAME: Final = "hire_calendar_reconcile"
ACTOR: Final = "pairly.hire.calendar_reconcile"
ENTITY: Final = "hire.calendar_operation"
STALE: Final = timedelta(minutes=10)
MAX_ATTEMPTS: Final = 5
MAX_PER_RUN: Final = 50


class _BusyError(Exception):
    """The interview is locked by a change in flight: try again next run."""


def _time(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return fallback
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return fallback


async def _interview(session: AsyncSession, op: HireCalendarOperation) -> HireInterview | None:
    exists = await session.scalar(
        select(HireInterview.id).where(HireInterview.id == op.interview_id)
    )
    if exists is None:
        return None
    locked = await session.scalar(
        select(HireInterview)
        .where(HireInterview.id == op.interview_id)
        .with_for_update(skip_locked=True)
    )
    if locked is None:
        raise _BusyError()
    return locked


def _is_current(op: HireCalendarOperation, interview: HireInterview | None) -> bool:
    """The provider's object of ``op`` is the interview's committed one."""
    if interview is None or op.external_id is None:
        return False
    if op.kind == "meeting":
        return interview.meeting_external_id == op.external_id
    return (
        interview.calendar_provider == op.provider and interview.calendar_event_id == op.external_id
    )


async def _newer_exists(session: AsyncSession, op: HireCalendarOperation) -> bool:
    found = await session.scalar(
        select(HireCalendarOperation.id)
        .where(
            HireCalendarOperation.interview_id == op.interview_id,
            HireCalendarOperation.kind == op.kind,
            HireCalendarOperation.created_at > op.created_at,
        )
        .limit(1)
    )
    return found is not None


async def _remove_orphan(session: AsyncSession, op: HireCalendarOperation, now: datetime) -> None:
    assert op.external_id is not None  # noqa: S101 (checked by the caller)
    intended = op.intended or {}
    start = _time(intended.get("starts_at"), now)
    end = _time(intended.get("ends_at"), start + timedelta(minutes=30))
    org = await org_scheduling(session)
    found = await interviewers(session, [op.organizer_id], org)
    organizer = found.get(op.organizer_id)
    if organizer is None:
        raise CalendarError("organizer_missing", transient=False)
    person = Person(organizer.id, organizer.email, organizer.rules.timezone)
    if op.kind == "meeting":
        credentials = await meetings.zoom_credentials(session)
        if credentials is None:
            raise CalendarError("zoom_not_configured", transient=False)
        async with google_client() as client:
            zoom = meetings.zoom_provider(client, credentials)
            user = CalendarUser(str(person.id), person.email, person.timezone)
            await zoom.delete_meeting(user, op.external_id)
        return
    async with calendar_set(session, op.org_id, org.seed, [person]) as calendars:
        provider = calendars.provider_for(person.id)
        if provider.name != op.provider:
            raise CalendarError(f"{op.provider}_not_connected", transient=False)
        spec = EventSpec(
            uid=str(intended.get("uid") or f"op-{op.id.hex}@pairly"),
            sequence=int(intended.get("sequence") or 0) + 1,
            summary="Cancelled",
            start=start,
            end=end,
        )
        await provider.cancel_event(
            calendars.user(person), EventRef(op.provider, op.external_id), spec
        )


async def _resync(
    session: AsyncSession, op: HireCalendarOperation, interview: HireInterview
) -> str:
    if interview.starts_at is None or interview.ends_at is None:
        return "nothing_to_write"
    active = interview.status in ACTIVE
    if op.action == "cancel" and active:
        # A cancel that did not commit: the provider's object is gone, make it again.
        if op.kind == "event":
            interview.calendar_event_id = None
        else:
            interview.meeting_external_id = None
            interview.meeting_url = None
            interview.meeting_provider = None
    try:
        record = await load_record(session, interview.application_id)
    except NotFoundError:
        return "nothing_to_write"
    panel = await panel_people(session, await panel_of(session, interview.id))
    cancelled = interview.status == InterviewStatus.CANCELLED.value
    await _write_event(session, interview, record, panel, cancel=cancelled)
    interview.updated_at = datetime.now(UTC)
    await session.flush()
    return "resynced"


async def _settle_one(session: AsyncSession, op: HireCalendarOperation, now: datetime) -> str:
    interview = await _interview(session, op)
    if op.external_id is None:
        if op.rejected:
            return "nothing_written"
        raise CalendarError("unknown_external_id", transient=False)
    if not _is_current(op, interview):
        await _remove_orphan(session, op, now)
        return "orphan_cancelled" if op.kind == "event" else "orphan_meeting_deleted"
    assert interview is not None  # noqa: S101 (current)
    if await _newer_exists(session, op):
        return "superseded"
    return await _resync(session, op, interview)


async def _audit(session: AsyncSession, action: str, op: HireCalendarOperation) -> None:
    await append_audit(
        session,
        action=action,
        actor_type=ActorType.SYSTEM,
        actor_id=ACTOR,
        entity_type=ENTITY,
        entity_id=op.id,
        workflow="hire",
        payload={
            "interview_id": str(op.interview_id),
            "provider": op.provider,
            "kind": op.kind,
            "action": op.action,
            "outcome": op.outcome,
            "attempts": op.attempts,
            "error": op.last_error,
        },
    )


async def reconcile_pending(
    session: AsyncSession, *, now: datetime | None = None, stale: timedelta = STALE
) -> dict[str, int]:
    """One organisation's pass (the session is bound to it, in a transaction)."""
    now = now or datetime.now(UTC)
    rows = list(
        await session.scalars(
            select(HireCalendarOperation)
            .where(
                HireCalendarOperation.status == OperationStatus.PENDING.value,
                HireCalendarOperation.updated_at <= now - stale,
            )
            .order_by(HireCalendarOperation.created_at)
            .limit(MAX_PER_RUN)
            .with_for_update(skip_locked=True)
        )
    )
    counts = {"reconciled": 0, "failed": 0, "retry": 0, "busy": 0}
    for op in rows:
        error: CalendarError | None = None
        outcome: str | None = None
        try:
            async with session.begin_nested():
                outcome = await _settle_one(session, op, now)
        except _BusyError:
            counts["busy"] += 1
            continue
        except CalendarError as exc:
            error = exc
        except CalendarUnavailableError:
            error = CalendarError("provider_unavailable", transient=True)
        except PairlyError as exc:
            error = CalendarError(exc.code[:64], transient=False)
        except httpx.HTTPError:
            error = CalendarError("provider_unavailable", transient=True)
        if error is not None:
            # The savepoint rolled back: read the row again before changing it.
            await session.refresh(op)
        op.updated_at = now
        if error is None:
            op.status = OperationStatus.RECONCILED.value
            op.outcome = outcome
            op.resolved_at = now
            counts["reconciled"] += 1
            await session.flush()
            await _audit(session, "hire.calendar.reconciled", op)
            continue
        op.attempts += 1
        op.last_error = error.code[:64]
        if error.transient and op.attempts < MAX_ATTEMPTS:
            counts["retry"] += 1
            await session.flush()
            continue
        op.status = OperationStatus.FAILED.value
        op.outcome = "needs_review"
        op.resolved_at = now
        counts["failed"] += 1
        await session.flush()
        log.warning("calendar_reconcile_failed", operation_id=str(op.id), code=error.code)
        await _audit(session, "hire.calendar.reconcile_failed", op)
    return counts


async def run_reconcile_job(ctx: JobContext) -> dict[str, Any]:
    """``hire_calendar_reconcile``: every 5 minutes, per organisation with applicants."""
    totals: dict[str, Any] = {"orgs": 0, "reconciled": 0, "failed": 0, "errors": 0}
    for org_id in await org_ids(ctx):
        session = ctx.sessionmaker()
        set_tenant(session, org_id, None)
        try:
            async with session, session.begin():
                counts = await reconcile_pending(session)
        except Exception as exc:
            log.warning(
                "calendar_reconcile_org_failed", org_id=str(org_id), error=type(exc).__name__
            )
            totals["errors"] += 1
            continue
        totals["orgs"] += 1
        totals["reconciled"] += counts["reconciled"]
        totals["failed"] += counts["failed"]
    return totals
