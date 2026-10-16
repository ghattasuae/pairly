# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""What the portal and screening mode read from scheduling (WP2.8), through one interface.

Scheduling (``pairly_hire.scheduling``) owns interviews. The candidate portal shows a
candidate their upcoming interviews, and screening mode offers "S: schedule". Neither
depends on how scheduling stores interviews, so both read this interface:

* ``InterviewSource.available()``: whether scheduling can take a booking (screening
  mode enables its schedule action).
* ``InterviewSource.upcoming_for_candidate(session, candidate_id, application_ids, env)``:
  the candidate's open interviews for those applications, as ``PortalInterview`` (what
  a candidate may see: status, time and time zone once booked, a join link, and the
  link the candidate already has to choose a time, or to change or cancel one). Never
  panel members, notes, scorecards or outcomes.

The default source is scheduling itself
(``pairly_hire.scheduling.api.upcoming_interviews_for_candidate``, imported on first
use). It runs in the caller's session: in the portal that is the candidate's portal
session, so Row Level Security shows only that candidate's interviews (hire_0005).
``set_interview_source`` swaps it (tests; ``None`` restores scheduling). Read per call.

``interview_history(session, application_id)`` is what the pipeline's profile timeline
shows: booked, rescheduled, cancelled, completed and no-show events with their times
and who acted (``scheduling.api.interview_history``). It carries no meeting links,
panel members or free text, so it is safe while the application is blind.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from pairly_hire.scheduling.emails import SchedulingEnv


@dataclass(frozen=True, slots=True)
class PortalInterview:
    id: uuid.UUID
    application_id: uuid.UUID
    # ``proposed`` (the candidate chooses a time), ``booked`` or ``rescheduled``.
    status: str
    # None while proposed.
    starts_at: datetime | None
    ends_at: datetime | None
    duration_min: int
    timezone: str
    # ``video`` (a meeting link) or ``unspecified`` (the team sends the details).
    format: str
    title: str
    join_url: str | None = None
    location: str | None = None
    # The candidate's own scheduling link (``/schedule#...``): to choose a time while
    # proposed; to change or cancel (one page) once booked. None when it has expired.
    choose_url: str | None = None
    reschedule_url: str | None = None
    cancel_url: str | None = None


class InterviewSource(Protocol):
    def available(self) -> bool: ...

    async def upcoming_for_candidate(
        self,
        session: AsyncSession,
        candidate_id: uuid.UUID,
        application_ids: Sequence[uuid.UUID],
        env: SchedulingEnv | None,
    ) -> Sequence[PortalInterview]: ...


class SchedulingSource:
    """Scheduling's interviews for the portal (``scheduling.api``)."""

    def available(self) -> bool:
        return True

    async def upcoming_for_candidate(
        self,
        session: AsyncSession,
        candidate_id: uuid.UUID,
        application_ids: Sequence[uuid.UUID],
        env: SchedulingEnv | None,
    ) -> Sequence[PortalInterview]:
        from pairly_hire.scheduling.api import upcoming_interviews_for_candidate
        from pairly_hire.scheduling.emails import SchedulingEnv as _Env

        wanted = set(application_ids)
        found = await upcoming_interviews_for_candidate(
            session, env or _Env.from_environment(), candidate_id
        )
        out: list[PortalInterview] = []
        for item in found:
            if item.application_id not in wanted:
                continue
            proposed = item.starts_at is None
            out.append(
                PortalInterview(
                    id=item.interview_id,
                    application_id=item.application_id,
                    status=item.status,
                    starts_at=item.starts_at,
                    ends_at=item.ends_at,
                    duration_min=item.duration_min,
                    timezone=item.timezone,
                    format="video" if item.meeting_url else "unspecified",
                    title=item.job_title,
                    join_url=None if proposed else item.meeting_url,
                    choose_url=item.schedule_url if proposed else None,
                    # One page changes or cancels a booking (the link the candidate has).
                    reschedule_url=None if proposed else item.schedule_url,
                    cancel_url=None,
                )
            )
        return out


_source: InterviewSource = SchedulingSource()


def set_interview_source(source: InterviewSource | None) -> None:
    """Swap the source (None: back to scheduling)."""
    global _source
    _source = source if source is not None else SchedulingSource()


def interview_source() -> InterviewSource:
    return _source


def scheduling_available() -> bool:
    try:
        return bool(_source.available())
    except Exception:
        return False


async def upcoming_interviews(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    application_ids: Sequence[uuid.UUID],
    env: SchedulingEnv | None = None,
) -> list[PortalInterview]:
    """The candidate's open interviews (only for ``application_ids``, their own): booked
    ones soonest first, then the ones waiting for the candidate to choose a time."""
    if not application_ids:
        return []
    wanted = set(application_ids)
    found = await _source.upcoming_for_candidate(session, candidate_id, list(wanted), env)
    return sorted(
        (i for i in found if i.application_id in wanted),
        key=lambda i: (i.starts_at is None, i.starts_at or datetime.min.replace(tzinfo=UTC)),
    )


@dataclass(frozen=True, slots=True)
class InterviewEvent:
    """One interview event on the profile timeline (staff only)."""

    at: datetime
    interview_id: uuid.UUID
    # ``booked``, ``rescheduled``, ``cancelled``, ``completed`` or ``no_show``.
    kind: str
    starts_at: datetime | None
    previous_starts_at: datetime | None
    duration_min: int | None
    # ``candidate`` or ``team``; ``actor_id`` names the staff user of a team action.
    by: str
    actor_id: str | None


async def interview_history(
    session: AsyncSession, application_id: uuid.UUID
) -> list[InterviewEvent]:
    from pairly_hire.scheduling.api import interview_history as _history

    return [
        InterviewEvent(
            at=e.at,
            interview_id=e.interview_id,
            kind=e.kind,
            starts_at=e.starts_at,
            previous_starts_at=e.previous_starts_at,
            duration_min=e.duration_min,
            by=e.by,
            actor_id=e.actor_id,
        )
        for e in await _history(session, application_id)
    ]
