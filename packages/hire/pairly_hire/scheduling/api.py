# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The scheduling interface other Hire features call (screening mode, candidate portal).

Every function runs in the caller's tenant-bound session and transaction; the caller
checks permissions first (``permissions.INTERVIEW_READ`` to read,
``INTERVIEW_SCHEDULE`` to change). Nothing here talks to the network except through the
calendar providers (fake by default).

Reading:

* ``scheduling_panel(session, registry, viewer_id, application_id)``: everything the
  scheduling panel shows for one application (blind-safe label, stage, time zones,
  defaults, the hiring team to pick a panel from, the default panel, its interviews).
* ``interview_view(session, interview)``: one interview as the team sees it.
* ``upcoming_interviews_for_candidate(session, env, candidate_id)``: for the candidate
  portal: the candidate's open interviews (booked ones with the join link and the link
  to change or cancel; proposed ones with the link to choose a time).
* ``interview_history(session, application_id)``: what happened to the application's
  interviews (booked, rescheduled, cancelled, completed, no-show), from the audit chain,
  for the candidate profile timeline. Times and who acted only: no meeting links,
  panel members or free text, so it is blind-safe as it is.
* ``suggest`` and ``check_slot`` (``service``): times where the whole panel is free, and
  why one time is not.

Changing (``service``): ``create_interview`` (``mode="book"`` or ``"link"``),
``update_interview`` (one form: time, length, panel, meeting link, candidate time zone),
``send_link``, ``cancel_interview``, ``record_outcome``; the candidate's own
``candidate_book`` and ``candidate_cancel`` (through the signed link, ``tokens``).

The web entry point is ``apps/web/src/components/hire/scheduling/scheduling-panel.tsx``
(``<SchedulingPanel applicationId=... onClose=... onChanged=... />``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.service import member_permissions
from pairly_core.models import Membership, User
from pairly_core.models.audit import AuditEvent
from pairly_core.rbac import RoleRegistry
from pairly_hire.models import HireJob
from pairly_hire.pipeline.hooks import application_blind
from pairly_hire.pipeline.records import load_record, organisation_name
from pairly_hire.scheduling.emails import SchedulingEnv, meeting_url_of
from pairly_hire.scheduling.models import (
    ACTIVE,
    OPEN,
    HireInterview,
    HireInterviewPanelist,
    InterviewStatus,
)
from pairly_hire.scheduling.permissions import INTERVIEW_READ
from pairly_hire.scheduling.providers import google_config, google_enabled
from pairly_hire.scheduling.rules import Interviewer, interviewers, org_scheduling
from pairly_hire.scheduling.service import (
    ENTITY,
    check_slot,
    interviews_for_application,
    panel_of,
    schedule_url,
    suggest,
)

__all__ = [
    "CandidateInterview",
    "InterviewHistoryEvent",
    "InterviewView",
    "PanelData",
    "PanelMember",
    "check_slot",
    "interview_history",
    "interview_view",
    "scheduling_panel",
    "suggest",
    "team",
    "upcoming_interviews_for_candidate",
]

MAX_TEAM: Final = 200


@dataclass(frozen=True, slots=True)
class PanelMember:
    id: uuid.UUID
    name: str
    timezone: str


@dataclass(frozen=True, slots=True)
class InterviewView:
    id: uuid.UUID
    application_id: uuid.UUID
    job_id: uuid.UUID
    stage_key: str
    status: str
    starts_at: datetime | None
    ends_at: datetime | None
    duration_min: int
    candidate_timezone: str
    panel: list[PanelMember]
    meeting_url: str | None
    meeting_provider: str | None
    calendar_provider: str | None
    ics_sequence: int
    link_expires_at: datetime | None
    link_active: bool
    cancelled_by: str | None
    can_record_outcome: bool
    created_at: datetime


def _member(person: Interviewer) -> PanelMember:
    return PanelMember(person.id, person.name, person.rules.timezone)


async def interview_view(
    session: AsyncSession, interview: HireInterview, *, now: datetime | None = None
) -> InterviewView:
    now = now or datetime.now(UTC)
    org = await org_scheduling(session)
    ids = await panel_of(session, interview.id)
    people = await interviewers(session, ids, org)
    link_active = (
        interview.link_nonce is not None
        and interview.link_expires_at is not None
        and interview.link_expires_at > now
        and interview.status in OPEN
    )
    return InterviewView(
        id=interview.id,
        application_id=interview.application_id,
        job_id=interview.job_id,
        stage_key=interview.stage_key,
        status=interview.status,
        starts_at=interview.starts_at,
        ends_at=interview.ends_at,
        duration_min=interview.duration_min,
        candidate_timezone=interview.candidate_timezone,
        panel=[_member(people[i]) for i in ids if i in people],
        meeting_url=meeting_url_of(interview),
        meeting_provider=interview.meeting_provider,
        calendar_provider=interview.calendar_provider,
        ics_sequence=interview.ics_sequence,
        link_expires_at=interview.link_expires_at,
        link_active=link_active,
        cancelled_by=interview.cancelled_by,
        can_record_outcome=interview.starts_at is not None
        and interview.starts_at <= now
        and interview.status in ACTIVE | {InterviewStatus.COMPLETED, InterviewStatus.NO_SHOW},
        created_at=interview.created_at,
    )


async def team(session: AsyncSession, registry: RoleRegistry) -> list[Interviewer]:
    """Members who may sit on a panel (``hire.interview.read``), by name."""
    rows = list(
        await session.scalars(
            select(User.id).join(Membership, Membership.user_id == User.id).distinct()
        )
    )[:MAX_TEAM]
    perms = await member_permissions(session, registry, rows)
    ids = [u for u in rows if INTERVIEW_READ in perms.get(u, frozenset())]
    org = await org_scheduling(session)
    found = await interviewers(session, ids, org)
    return sorted(found.values(), key=lambda p: (p.name.casefold(), str(p.id)))


@dataclass(frozen=True, slots=True)
class PanelData:
    application_id: uuid.UUID
    job_id: uuid.UUID
    label: str
    identity_hidden: bool
    job_title: str
    stage_key: str
    stage_label: str
    stage_label_ar: str
    candidate_timezone: str
    viewer_timezone: str
    duration_min: int
    step_min: int
    team: list[PanelMember]
    default_panel: list[uuid.UUID]
    interviews: list[InterviewView] = field(default_factory=list)
    google_enabled: bool = False
    google_configured: bool = False


async def _default_panel(
    session: AsyncSession, job_id: uuid.UUID, viewer_id: uuid.UUID, allowed: set[uuid.UUID]
) -> list[uuid.UUID]:
    """The panel of the job's latest interview (people still on the team), else the
    viewer alone."""
    last = await session.scalar(
        select(HireInterview.id)
        .where(HireInterview.job_id == job_id)
        .order_by(HireInterview.created_at.desc())
        .limit(1)
    )
    if last is not None:
        ids = [
            u
            for u in await session.scalars(
                select(HireInterviewPanelist.user_id).where(
                    HireInterviewPanelist.interview_id == last,
                    HireInterviewPanelist.removed_at.is_(None),
                )
            )
            if u in allowed
        ]
        if ids:
            return sorted(ids, key=str)
    return [viewer_id] if viewer_id in allowed else sorted(allowed, key=str)[:1]


async def scheduling_panel(
    session: AsyncSession,
    registry: RoleRegistry,
    viewer_id: uuid.UUID,
    application_id: uuid.UUID,
) -> PanelData:
    record = await load_record(session, application_id)
    blind = await application_blind(session, record.app.id)
    org = await org_scheduling(session)
    people = await team(session, registry)
    viewer = (await interviewers(session, [viewer_id], org)).get(viewer_id)
    interviews = await interviews_for_application(session, record.app.id)
    open_one = next((i for i in interviews if i.status in OPEN), None)
    allowed = {p.id for p in people}
    stage = record.stage
    return PanelData(
        application_id=record.app.id,
        job_id=record.job.id,
        label=record.label(blind),
        identity_hidden=record.identity_hidden(blind),
        job_title=record.job.title,
        stage_key=record.stage_key,
        stage_label=stage.label,
        stage_label_ar=stage.label_ar,
        candidate_timezone=open_one.candidate_timezone if open_one else org.timezone,
        viewer_timezone=viewer.rules.timezone if viewer else org.timezone,
        duration_min=open_one.duration_min if open_one else org.duration_min,
        step_min=org.step_min,
        team=[_member(p) for p in people],
        default_panel=await panel_of(session, open_one.id)
        if open_one
        else await _default_panel(session, record.job.id, viewer_id, allowed),
        interviews=[await interview_view(session, i) for i in interviews],
        google_enabled=await google_enabled(session),
        google_configured=google_config() is not None,
    )


@dataclass(frozen=True, slots=True)
class CandidateInterview:
    """One interview as its candidate sees it (the candidate portal)."""

    interview_id: uuid.UUID
    application_id: uuid.UUID
    job_title: str
    company: str
    status: str
    starts_at: datetime | None
    ends_at: datetime | None
    duration_min: int
    timezone: str
    panel_size: int
    meeting_url: str | None
    # Choose a time (proposed), or change or cancel it (booked). None without a link.
    schedule_url: str | None


async def upcoming_interviews_for_candidate(
    session: AsyncSession,
    env: SchedulingEnv,
    candidate_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> list[CandidateInterview]:
    """The candidate's open interviews that have not ended, soonest first (proposed
    ones last). The links are the ones already sent (no new link is issued, so the
    email the candidate has keeps working)."""
    now = now or datetime.now(UTC)
    rows = list(
        await session.scalars(
            select(HireInterview)
            .where(HireInterview.candidate_id == candidate_id, HireInterview.status.in_(OPEN))
            .order_by(HireInterview.starts_at.asc().nulls_last(), HireInterview.created_at)
        )
    )
    out: list[CandidateInterview] = []
    for interview in rows:
        if interview.ends_at is not None and interview.ends_at <= now:
            continue
        job = await session.scalar(select(HireJob).where(HireJob.id == interview.job_id))
        locale = interview.candidate_locale
        title = ((job.title_ar if locale == "ar" else "") or job.title) if job else ""
        link_ok = interview.link_expires_at is not None and interview.link_expires_at > now
        out.append(
            CandidateInterview(
                interview_id=interview.id,
                application_id=interview.application_id,
                job_title=title,
                company=await organisation_name(session, interview.org_id),
                status=interview.status,
                starts_at=interview.starts_at,
                ends_at=interview.ends_at,
                duration_min=interview.duration_min,
                timezone=interview.candidate_timezone,
                panel_size=len(await panel_of(session, interview.id)),
                meeting_url=meeting_url_of(interview) if interview.status in ACTIVE else None,
                schedule_url=schedule_url(env, interview) if link_ok else None,
            )
        )
    return out


# ------------------------------------------------------------------ history (timeline)

_HISTORY_ACTIONS: Final = {
    "hire.interview.booked": "booked",
    "hire.interview.rescheduled": "rescheduled",
    "hire.interview.cancelled": "cancelled",
    "hire.interview.outcome_recorded": "outcome",
}
_OUTCOME_KINDS: Final = frozenset({"completed", "no_show"})


@dataclass(frozen=True, slots=True)
class InterviewHistoryEvent:
    """One thing that happened to an interview (the profile timeline)."""

    at: datetime
    interview_id: uuid.UUID
    # ``booked``, ``rescheduled``, ``cancelled``, ``completed`` or ``no_show``.
    kind: str
    starts_at: datetime | None
    # The earlier time of a rescheduled interview.
    previous_starts_at: datetime | None
    duration_min: int | None
    # ``candidate`` (through their link) or ``team``; ``actor_id`` is the staff user
    # for a team action (None for the candidate or the system).
    by: str
    actor_id: str | None


def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def interview_history(
    session: AsyncSession, application_id: uuid.UUID
) -> list[InterviewHistoryEvent]:
    """The application's interview events, oldest first (audit chain, same tenant)."""
    ids = [
        str(i)
        for i in await session.scalars(
            select(HireInterview.id).where(HireInterview.application_id == application_id)
        )
    ]
    if not ids:
        return []
    rows = await session.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.entity_type == ENTITY,
            AuditEvent.entity_id.in_(ids),
            AuditEvent.action.in_(tuple(_HISTORY_ACTIONS)),
        )
        .order_by(AuditEvent.seq)
    )
    out: list[InterviewHistoryEvent] = []
    for row in rows:
        payload = row.payload or {}
        if payload.get("application_id") != str(application_id):
            continue
        kind = _HISTORY_ACTIONS[row.action]
        if kind == "outcome":
            kind = str(payload.get("status"))
            if kind not in _OUTCOME_KINDS:
                continue
        by_candidate = payload.get("by") == "candidate" or row.actor_type == "candidate"
        duration = payload.get("duration_min")
        out.append(
            InterviewHistoryEvent(
                at=row.occurred_at,
                interview_id=uuid.UUID(str(row.entity_id)),
                kind=kind,
                starts_at=_when(payload.get("starts_at")),
                previous_starts_at=_when(payload.get("from")) if kind == "rescheduled" else None,
                duration_min=duration if isinstance(duration, int) else None,
                by="candidate" if by_candidate else "team",
                actor_id=None if by_candidate or row.actor_type != "user" else row.actor_id,
            )
        )
    return out
