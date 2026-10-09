# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""What the portal and screening mode read from scheduling (WP2.8), through one interface.

Scheduling (``pairly_hire.scheduling``) owns interviews. The candidate portal shows a
candidate their upcoming interviews, and screening mode offers "S: schedule" only when
scheduling can take it. Neither may depend on how scheduling stores interviews, so both
read this interface:

* ``InterviewSource.available()``: whether scheduling is installed (screening mode
  enables its schedule action; until then it says scheduling is coming).
* ``InterviewSource.upcoming_for_candidate(session, candidate_id, application_ids)``:
  the candidate's upcoming interviews for those applications, soonest first, as
  ``PortalInterview`` (what a candidate may see: time, time zone, format, a join link,
  and links to reschedule or cancel when scheduling offers them). Never panel notes,
  scorecards or interviewer details beyond a display name.

Scheduling registers its implementation with ``set_interview_source`` when its module is
imported. Until then the default reports nothing (``available()`` is false and there are
no interviews). Read per call, so a test can swap it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class PortalInterview:
    id: uuid.UUID
    application_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    timezone: str
    # ``video``, ``phone`` or ``onsite``.
    format: str
    title: str
    join_url: str | None = None
    location: str | None = None
    reschedule_url: str | None = None
    cancel_url: str | None = None


class InterviewSource(Protocol):
    def available(self) -> bool: ...

    async def upcoming_for_candidate(
        self,
        session: AsyncSession,
        candidate_id: uuid.UUID,
        application_ids: Sequence[uuid.UUID],
    ) -> Sequence[PortalInterview]: ...


class _NoScheduling:
    def available(self) -> bool:
        return False

    async def upcoming_for_candidate(
        self,
        session: AsyncSession,
        candidate_id: uuid.UUID,
        application_ids: Sequence[uuid.UUID],
    ) -> Sequence[PortalInterview]:
        return ()


_source: InterviewSource = _NoScheduling()


def set_interview_source(source: InterviewSource | None) -> None:
    """Register scheduling (None: back to the default, no scheduling)."""
    global _source
    _source = source if source is not None else _NoScheduling()


def interview_source() -> InterviewSource:
    return _source


def scheduling_available() -> bool:
    try:
        return bool(_source.available())
    except Exception:
        return False


async def upcoming_interviews(
    session: AsyncSession, candidate_id: uuid.UUID, application_ids: Sequence[uuid.UUID]
) -> list[PortalInterview]:
    """The candidate's upcoming interviews (only for ``application_ids``, their own)."""
    if not application_ids:
        return []
    wanted = set(application_ids)
    found = await _source.upcoming_for_candidate(session, candidate_id, list(wanted))
    return sorted((i for i in found if i.application_id in wanted), key=lambda i: i.starts_at)
