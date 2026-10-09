# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Scheduling routes (WP2.8).

Signed in. Reads need ``hire.interview.read``; changes ``hire.interview.schedule``
(a person). Every change is audited in the request's transaction.

* ``GET  /scheduling/applications/{id}``             the scheduling panel's data
* ``POST /scheduling/applications/{id}/slots``       ``{"panel", "duration_min", ...}``:
                                                      times where the whole panel is free
* ``POST /scheduling/applications/{id}/check``       why one time does or does not work
* ``POST /scheduling/applications/{id}/interviews``  book a time, or send a link (201)
* ``GET  /scheduling/interviews``                    by job, application, or mine
* ``GET|PATCH /scheduling/interviews/{id}``          one form for every change
* ``POST /scheduling/interviews/{id}/cancel``        cancel (both sides told)
* ``POST /scheduling/interviews/{id}/link``          a new self-scheduling link
* ``POST /scheduling/interviews/{id}/outcome``       ``completed`` or ``no_show``
* ``GET|PUT /scheduling/availability/me``            my interview availability
* ``GET|PUT /scheduling/availability/{user_id}``     someone's (``schedule`` to change)
* ``GET|PUT /settings/scheduling``                   organisation defaults (``settings.manage``)
* ``GET  /scheduling/integrations``                  calendar connections and their status
* ``POST /scheduling/integrations/google/start``     the Google consent URL (flag and client)
* ``POST /scheduling/integrations/google/callback``  ``{"code", "state"}``
* ``POST /scheduling/integrations/google/disconnect``

Public, no sign-in, rate limited (the candidate's ``hs1`` token in the body, never in
a URL):

* ``POST /public/scheduling/read``    the interview and free times in the candidate's zone
* ``POST /public/scheduling/book``    hold a time (409 ``slot_taken`` if it went)
* ``POST /public/scheduling/cancel``  cancel
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_api.idempotency import idempotent
from pairly_core.audit import actor_ref
from pairly_core.db import set_tenant
from pairly_core.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
)
from pairly_core.identity import AppContext, Context, CurrentActor, OrgSession, RequestSession
from pairly_core.integrations.calendar.base import CalendarError
from pairly_core.integrations.calendar.google import GoogleOAuth
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_core.scheduling.availability import AvailabilityError, Interval, sociable, zone
from pairly_hire.antispam import SlidingWindowLimiter, client_ip
from pairly_hire.api.common import web_url
from pairly_hire.messaging.compose import Author
from pairly_hire.models import HireJob
from pairly_hire.permissions import CANDIDATE_READ, SETTINGS_MANAGE
from pairly_hire.pipeline.records import AppRecord, load_record, organisation_name
from pairly_hire.scheduling import api as scheduling
from pairly_hire.scheduling import providers, rules, service
from pairly_hire.scheduling.emails import SchedulingEnv
from pairly_hire.scheduling.models import OPEN, HireInterview, HireInterviewPanelist
from pairly_hire.scheduling.permissions import INTERVIEW_READ, INTERVIEW_SCHEDULE
from pairly_hire.scheduling.tokens import connect_state, read_connect_state, read_schedule_token

router = APIRouter(tags=["hire"])

PUBLIC_LIMITS: Final = {"read": (60, 3600.0), "write": (20, 3600.0)}
_Tz = Annotated[str, StringConstraints(min_length=1, max_length=64)]
_Url = Annotated[str, StringConstraints(max_length=900, pattern=r"^https://\S+$")]


def env(request: Request) -> SchedulingEnv:
    return SchedulingEnv.from_settings(
        getattr(request.app.state, "notifications", None), web_url=web_url(request)
    )


def _person(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can do this")


def _author(actor: Actor) -> Author:
    kind, actor_id = actor_ref(actor)
    return Author(kind, actor_id)


def _scheduler(actor: Actor) -> None:
    authorize(actor, INTERVIEW_SCHEDULE)
    _person(actor)


# ------------------------------------------------------------------ schemas


class HirePanelMemberOut(BaseModel):
    id: uuid.UUID
    name: str
    timezone: str


class HireInterviewOut(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    job_id: uuid.UUID
    stage_key: str
    status: str
    starts_at: datetime | None
    ends_at: datetime | None
    duration_min: int
    candidate_timezone: str
    panel: list[HirePanelMemberOut]
    meeting_url: str | None
    meeting_provider: str | None
    calendar_provider: str | None
    ics_sequence: int
    link_expires_at: datetime | None
    link_active: bool
    cancelled_by: str | None
    can_record_outcome: bool
    created_at: datetime


class HireSchedulingPanelOut(BaseModel):
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
    team: list[HirePanelMemberOut]
    default_panel: list[uuid.UUID]
    interviews: list[HireInterviewOut]
    google_enabled: bool
    google_configured: bool


class HireSlotsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    panel: list[uuid.UUID] = Field(min_length=1, max_length=service.MAX_PANEL)
    duration_min: int = Field(ge=15, le=240)
    candidate_timezone: _Tz | None = None
    # Moving an interview: its own time is not a conflict.
    interview_id: uuid.UUID | None = None
    start: datetime | None = None
    days: int | None = Field(default=None, ge=1, le=60)


class HireSlotOut(BaseModel):
    start: datetime
    end: datetime
    # For the candidate: inside 08:00 to 20:00 in their zone.
    sociable: bool


class HireBusyOut(BaseModel):
    user_id: uuid.UUID
    blocks: list[HireSlotOut]


class HireSlotsOut(BaseModel):
    timezone: str
    duration_min: int
    window_start: datetime
    window_end: datetime
    slots: list[HireSlotOut]
    # Every free start in the window (the small calendar view).
    free: list[datetime]
    busy: list[HireBusyOut]


class HireCheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    panel: list[uuid.UUID] = Field(min_length=1, max_length=service.MAX_PANEL)
    starts_at: datetime
    duration_min: int = Field(ge=15, le=240)
    interview_id: uuid.UUID | None = None


class HireConflictOut(BaseModel):
    user_id: uuid.UUID | None
    name: str | None
    reason: str


class HireCheckOut(BaseModel):
    ok: bool
    conflicts: list[HireConflictOut]


class HireInterviewCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Annotated[str, StringConstraints(pattern=r"^(book|link)$")]
    panel: list[uuid.UUID] = Field(min_length=1, max_length=service.MAX_PANEL)
    duration_min: int = Field(ge=15, le=240)
    starts_at: datetime | None = None
    candidate_timezone: _Tz | None = None
    meeting_url: _Url | None = None
    force: bool = False


class HireInterviewUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starts_at: datetime | None = None
    duration_min: int | None = Field(default=None, ge=15, le=240)
    panel: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=service.MAX_PANEL)
    meeting_url: _Url | None = None
    clear_meeting_url: bool = False
    candidate_timezone: _Tz | None = None
    force: bool = False


class HireOutcomeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Annotated[str, StringConstraints(pattern=r"^(completed|no_show)$")]


class HireAvailabilityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: _Tz
    working_hours: dict[str, list[list[str]]] = Field(max_length=7)
    buffer_before_min: int = Field(ge=0, le=240)
    buffer_after_min: int = Field(ge=0, le=240)
    max_per_day: int | None = Field(default=None, ge=0, le=24)
    holidays: list[Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]] = Field(
        default_factory=list, max_length=366
    )


class HireAvailabilityOut(BaseModel):
    user_id: uuid.UUID
    name: str
    saved: bool
    timezone: str
    working_hours: dict[str, list[list[str]]]
    buffer_before_min: int
    buffer_after_min: int
    max_per_day: int | None
    holidays: list[str]


class HireSchedulingSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: _Tz | None = None
    duration_min: int | None = Field(default=None, ge=15, le=240)
    step_min: int | None = Field(default=None)
    notice_hours: int | None = Field(default=None, ge=0, le=168)
    horizon_days: int | None = Field(default=None, ge=1, le=60)
    link_days: int | None = Field(default=None, ge=1, le=30)


class HireSchedulingSettingsOut(BaseModel):
    timezone: str
    duration_min: int
    step_min: int
    notice_hours: int
    horizon_days: int
    link_days: int


class HireCalendarProviderOut(BaseModel):
    provider: str
    label: str
    # ``available`` (can be connected), ``needs_setup`` (flag or OAuth client missing),
    # ``planned`` (a later milestone).
    availability: str
    flag_enabled: bool
    configured: bool
    connected: bool
    status: str | None
    account_email: str | None
    steps: list[str]


class HireCalendarIntegrationsOut(BaseModel):
    using: str
    providers: list[HireCalendarProviderOut]


class HireConnectOut(BaseModel):
    url: str


class HireConnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    state: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class HirePublicScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, StringConstraints(min_length=20, max_length=300)]
    timezone: _Tz | None = None


class HirePublicBookIn(HirePublicScheduleIn):
    starts_at: datetime


class HirePublicSlotOut(BaseModel):
    start: datetime
    end: datetime
    date: str
    time: str
    sociable: bool


class HirePublicScheduleOut(BaseModel):
    # ``choose`` (pick a time), ``booked`` (a time is held: change or cancel it),
    # ``cancelled``.
    state: str
    organisation: str
    job_title: str
    duration_min: int
    panel_size: int
    timezone: str
    starts_at: datetime | None
    ends_at: datetime | None
    meeting_url: str | None
    slots: list[HirePublicSlotOut]
    # A new link after a booking (the one used is spent).
    token: str | None = None


# ------------------------------------------------------------------ helpers


def _interview_out(view: scheduling.InterviewView) -> HireInterviewOut:
    return HireInterviewOut(
        id=view.id,
        application_id=view.application_id,
        job_id=view.job_id,
        stage_key=view.stage_key,
        status=view.status,
        starts_at=view.starts_at,
        ends_at=view.ends_at,
        duration_min=view.duration_min,
        candidate_timezone=view.candidate_timezone,
        panel=[HirePanelMemberOut(id=p.id, name=p.name, timezone=p.timezone) for p in view.panel],
        meeting_url=view.meeting_url,
        meeting_provider=view.meeting_provider,
        calendar_provider=view.calendar_provider,
        ics_sequence=view.ics_sequence,
        link_expires_at=view.link_expires_at,
        link_active=view.link_active,
        cancelled_by=view.cancelled_by,
        can_record_outcome=view.can_record_outcome,
        created_at=view.created_at,
    )


async def _out(session: AsyncSession, interview: HireInterview) -> HireInterviewOut:
    return _interview_out(await scheduling.interview_view(session, interview))


async def _exclude(session: AsyncSession, interview_id: uuid.UUID | None) -> HireInterview | None:
    if interview_id is None:
        return None
    return await service.get_interview(session, interview_id)


def _calendar_failed(exc: CalendarError) -> ServiceUnavailableError:
    return ServiceUnavailableError(f"a calendar could not be used ({exc.code})")


# ------------------------------------------------------------------ panel, slots, check


@router.get("/scheduling/applications/{application_id}", response_model=HireSchedulingPanelOut)
async def panel(
    application_id: uuid.UUID, actor: CurrentActor, session: OrgSession, ctx: Context
) -> HireSchedulingPanelOut:
    authorize(actor, INTERVIEW_READ)
    authorize(actor, CANDIDATE_READ)
    data = await scheduling.scheduling_panel(session, ctx.registry, actor.id, application_id)
    return HireSchedulingPanelOut(
        application_id=data.application_id,
        job_id=data.job_id,
        label=data.label,
        identity_hidden=data.identity_hidden,
        job_title=data.job_title,
        stage_key=data.stage_key,
        stage_label=data.stage_label,
        stage_label_ar=data.stage_label_ar,
        candidate_timezone=data.candidate_timezone,
        viewer_timezone=data.viewer_timezone,
        duration_min=data.duration_min,
        step_min=data.step_min,
        team=[HirePanelMemberOut(id=p.id, name=p.name, timezone=p.timezone) for p in data.team],
        default_panel=data.default_panel,
        interviews=[_interview_out(i) for i in data.interviews],
        google_enabled=data.google_enabled,
        google_configured=data.google_configured,
    )


@router.post("/scheduling/applications/{application_id}/slots", response_model=HireSlotsOut)
async def slots(
    application_id: uuid.UUID, body: HireSlotsIn, actor: CurrentActor, session: OrgSession
) -> HireSlotsOut:
    authorize(actor, INTERVIEW_READ)
    record = await load_record(session, application_id)
    exclude = await _exclude(session, body.interview_id)
    if exclude is not None and exclude.application_id != record.app.id:
        raise NotFoundError("interview not found")
    tz = body.candidate_timezone or (exclude.candidate_timezone if exclude else "")
    found = await service.suggest(
        session,
        body.panel,
        duration_min=body.duration_min,
        timezone=tz,
        exclude=exclude,
        start=body.start,
        days=body.days,
    )
    window = found.view.window
    return HireSlotsOut(
        timezone=found.timezone,
        duration_min=found.duration_min,
        window_start=window.start,
        window_end=window.end,
        slots=[
            HireSlotOut(start=s.start, end=s.end, sociable=sociable(s, found.timezone))
            for s in found.slots
        ],
        free=[s.start for s in found.all_slots],
        busy=[
            HireBusyOut(
                user_id=user_id,
                blocks=[
                    HireSlotOut(start=b.start, end=b.end, sociable=True)
                    for b in blocks
                    if b.overlaps(window)
                ],
            )
            for user_id, blocks in found.view.busy.items()
        ],
    )


@router.post("/scheduling/applications/{application_id}/check", response_model=HireCheckOut)
async def check(
    application_id: uuid.UUID, body: HireCheckIn, actor: CurrentActor, session: OrgSession
) -> HireCheckOut:
    authorize(actor, INTERVIEW_READ)
    record = await load_record(session, application_id)
    exclude = await _exclude(session, body.interview_id)
    if exclude is not None and exclude.application_id != record.app.id:
        raise NotFoundError("interview not found")
    slot = Interval(body.starts_at, body.starts_at + timedelta(minutes=body.duration_min))
    found = await service.check_slot(session, body.panel, slot, exclude=exclude)
    org = await rules.org_scheduling(session)
    names = await rules.interviewers(session, body.panel, org)
    return HireCheckOut(
        ok=not found,
        conflicts=[
            HireConflictOut(
                user_id=uuid.UUID(c.key) if c.key else None,
                name=names[uuid.UUID(c.key)].name if c.key and uuid.UUID(c.key) in names else None,
                reason=c.reason,
            )
            for c in found
        ],
    )


# ------------------------------------------------------------------ interviews


@router.post(
    "/scheduling/applications/{application_id}/interviews",
    status_code=201,
    response_model=HireInterviewOut,
    dependencies=[Depends(idempotent, scope="function")],
)
async def create(
    application_id: uuid.UUID,
    body: HireInterviewCreateIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
) -> HireInterviewOut:
    _scheduler(actor)
    record = await load_record(session, application_id, lock=True)
    interview = await service.create_interview(
        session,
        env(request),
        ctx.registry,
        _author(actor),
        actor.id,
        record,
        mode=body.mode,
        panel_ids=body.panel,
        duration_min=body.duration_min,
        starts_at=body.starts_at,
        candidate_timezone=body.candidate_timezone,
        meeting_url=body.meeting_url,
        force=body.force,
    )
    return await _out(session, interview)


@router.get("/scheduling/interviews", response_model=list[HireInterviewOut])
async def list_interviews(
    actor: CurrentActor,
    session: OrgSession,
    job_id: Annotated[uuid.UUID | None, Query()] = None,
    application_id: Annotated[uuid.UUID | None, Query()] = None,
    mine: Annotated[bool, Query()] = False,
    upcoming: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[HireInterviewOut]:
    authorize(actor, INTERVIEW_READ)
    query = select(HireInterview)
    if job_id is not None:
        query = query.where(HireInterview.job_id == job_id)
    if application_id is not None:
        query = query.where(HireInterview.application_id == application_id)
    if mine:
        query = query.where(
            HireInterview.id.in_(
                select(HireInterviewPanelist.interview_id).where(
                    HireInterviewPanelist.user_id == actor.id,
                    HireInterviewPanelist.removed_at.is_(None),
                )
            )
        )
    if upcoming:
        query = query.where(
            HireInterview.status.in_(OPEN),
            (HireInterview.ends_at.is_(None)) | (HireInterview.ends_at > datetime.now(UTC)),
        )
    rows = await session.scalars(
        query.order_by(HireInterview.starts_at.asc().nulls_last(), HireInterview.created_at).limit(
            limit
        )
    )
    return [await _out(session, i) for i in rows]


@router.get("/scheduling/interviews/{interview_id}", response_model=HireInterviewOut)
async def get_interview(
    interview_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> HireInterviewOut:
    authorize(actor, INTERVIEW_READ)
    return await _out(session, await service.get_interview(session, interview_id))


async def _locked(
    session: AsyncSession, interview_id: uuid.UUID
) -> tuple[HireInterview, AppRecord]:
    interview = await service.get_interview(session, interview_id, lock=True)
    record = await load_record(session, interview.application_id)
    return interview, record


@router.patch("/scheduling/interviews/{interview_id}", response_model=HireInterviewOut)
async def update(
    interview_id: uuid.UUID,
    body: HireInterviewUpdateIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
) -> HireInterviewOut:
    _scheduler(actor)
    interview, record = await _locked(session, interview_id)
    await service.update_interview(
        session,
        env(request),
        ctx.registry,
        _author(actor),
        interview,
        record,
        starts_at=body.starts_at,
        duration_min=body.duration_min,
        panel_ids=body.panel,
        meeting_url=body.meeting_url,
        clear_meeting_url=body.clear_meeting_url,
        candidate_timezone=body.candidate_timezone,
        force=body.force,
    )
    return await _out(session, interview)


@router.post(
    "/scheduling/interviews/{interview_id}/cancel",
    response_model=HireInterviewOut,
    dependencies=[Depends(idempotent, scope="function")],
)
async def cancel(
    interview_id: uuid.UUID, request: Request, actor: CurrentActor, session: OrgSession
) -> HireInterviewOut:
    _scheduler(actor)
    interview, record = await _locked(session, interview_id)
    await service.cancel_interview(session, env(request), _author(actor), interview, record)
    return await _out(session, interview)


@router.post(
    "/scheduling/interviews/{interview_id}/link",
    response_model=HireInterviewOut,
    dependencies=[Depends(idempotent, scope="function")],
)
async def link(
    interview_id: uuid.UUID, request: Request, actor: CurrentActor, session: OrgSession
) -> HireInterviewOut:
    _scheduler(actor)
    interview, record = await _locked(session, interview_id)
    await service.send_link(session, env(request), _author(actor), interview, record)
    return await _out(session, interview)


@router.post("/scheduling/interviews/{interview_id}/outcome", response_model=HireInterviewOut)
async def outcome(
    interview_id: uuid.UUID, body: HireOutcomeIn, actor: CurrentActor, session: OrgSession
) -> HireInterviewOut:
    authorize(actor, INTERVIEW_READ)
    _person(actor)
    interview = await service.get_interview(session, interview_id, lock=True)
    if not actor.has(INTERVIEW_SCHEDULE) and actor.id not in await service.panel_of(
        session, interview.id
    ):
        raise ForbiddenError("only the panel or a scheduler records the outcome")
    await service.record_outcome(session, _author(actor), actor.id, interview, body.outcome)
    return await _out(session, interview)


# ------------------------------------------------------------------ availability


async def _availability_out(session: AsyncSession, user_id: uuid.UUID) -> HireAvailabilityOut:
    org = await rules.org_scheduling(session)
    found = (await rules.interviewers(session, [user_id], org)).get(user_id)
    if found is None:
        raise NotFoundError("person not found")
    r = found.rules
    return HireAvailabilityOut(
        user_id=user_id,
        name=found.name,
        saved=found.saved,
        timezone=r.timezone,
        working_hours=r.hours.to_json(),
        buffer_before_min=int(r.buffer_before.total_seconds() // 60),
        buffer_after_min=int(r.buffer_after.total_seconds() // 60),
        max_per_day=r.max_per_day,
        holidays=sorted(d.isoformat() for d in r.holidays),
    )


async def _save_availability(
    session: AsyncSession, actor: Actor, user_id: uuid.UUID, body: HireAvailabilityIn
) -> HireAvailabilityOut:
    await rules.save_interviewer_rules(
        session,
        actor,
        user_id,
        timezone=body.timezone,
        working_hours=body.working_hours,
        buffer_before_min=body.buffer_before_min,
        buffer_after_min=body.buffer_after_min,
        max_per_day=body.max_per_day,
        holidays=body.holidays,
    )
    return await _availability_out(session, user_id)


@router.get("/scheduling/availability/me", response_model=HireAvailabilityOut)
async def my_availability(actor: CurrentActor, session: OrgSession) -> HireAvailabilityOut:
    authorize(actor, INTERVIEW_READ)
    _person(actor)
    return await _availability_out(session, actor.id)


@router.put("/scheduling/availability/me", response_model=HireAvailabilityOut)
async def save_my_availability(
    body: HireAvailabilityIn, actor: CurrentActor, session: OrgSession
) -> HireAvailabilityOut:
    authorize(actor, INTERVIEW_READ)
    _person(actor)
    return await _save_availability(session, actor, actor.id, body)


async def _team_member(session: AsyncSession, ctx: AppContext, user_id: uuid.UUID) -> None:
    if user_id not in {p.id for p in await scheduling.team(session, ctx.registry)}:
        raise NotFoundError("person not found")


@router.get("/scheduling/availability/{user_id}", response_model=HireAvailabilityOut)
async def availability(
    user_id: uuid.UUID, actor: CurrentActor, session: OrgSession, ctx: Context
) -> HireAvailabilityOut:
    authorize(actor, INTERVIEW_READ)
    await _team_member(session, ctx, user_id)
    return await _availability_out(session, user_id)


@router.put("/scheduling/availability/{user_id}", response_model=HireAvailabilityOut)
async def save_availability(
    user_id: uuid.UUID,
    body: HireAvailabilityIn,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
) -> HireAvailabilityOut:
    _scheduler(actor)
    await _team_member(session, ctx, user_id)
    return await _save_availability(session, actor, user_id, body)


# ------------------------------------------------------------------ settings


def _settings_out(org: rules.OrgScheduling) -> HireSchedulingSettingsOut:
    return HireSchedulingSettingsOut(
        timezone=org.timezone,
        duration_min=org.duration_min,
        step_min=org.step_min,
        notice_hours=org.notice_hours,
        horizon_days=org.horizon_days,
        link_days=org.link_days,
    )


@router.get("/settings/scheduling", response_model=HireSchedulingSettingsOut)
async def get_settings(actor: CurrentActor, session: OrgSession) -> HireSchedulingSettingsOut:
    authorize(actor, INTERVIEW_READ)
    return _settings_out(await rules.org_scheduling(session))


@router.put("/settings/scheduling", response_model=HireSchedulingSettingsOut)
async def put_settings(
    body: HireSchedulingSettingsIn, actor: CurrentActor, session: OrgSession
) -> HireSchedulingSettingsOut:
    authorize(actor, SETTINGS_MANAGE)
    _person(actor)
    if body.step_min is not None and body.step_min not in (15, 30, 60):
        raise BadRequestError("the step is 15, 30 or 60 minutes")
    values = body.model_dump(exclude_none=True)
    return _settings_out(await rules.save_org_scheduling(session, actor, values))


# ------------------------------------------------------------------ calendar integrations

GOOGLE_STEPS: Final = [
    "An administrator creates a Google Cloud project for your organisation.",
    "In that project, turn on the Google Calendar API.",
    "Set up the OAuth consent screen (internal to your Google Workspace).",
    "Create an OAuth client of type web application, with the Pairly callback URL.",
    "Give the client id, secret and callback URL to Pairly as PAIRLY_GOOGLE_CLIENT_ID, "
    "PAIRLY_GOOGLE_CLIENT_SECRET and PAIRLY_GOOGLE_REDIRECT_URL.",
    "Turn on the hire.scheduling.google feature flag in Settings, Feature flags.",
    "Each interviewer then connects their own calendar here.",
]


@router.get("/scheduling/integrations", response_model=HireCalendarIntegrationsOut)
async def integrations(actor: CurrentActor, session: OrgSession) -> HireCalendarIntegrationsOut:
    authorize(actor, INTERVIEW_READ)
    flag = await providers.google_enabled(session)
    configured = providers.google_config() is not None
    mine = {c.provider: c for c in await providers.my_connections(session, actor.id)}
    google = mine.get("google")
    connected = google is not None and google.status == "active"
    return HireCalendarIntegrationsOut(
        using="google" if flag and configured and connected else "fake",
        providers=[
            HireCalendarProviderOut(
                provider="google",
                label="Google Calendar and Google Meet",
                availability="available" if flag and configured else "needs_setup",
                flag_enabled=flag,
                configured=configured,
                connected=connected,
                status=google.status if google else None,
                account_email=google.account_email if google and connected else None,
                steps=GOOGLE_STEPS,
            ),
            HireCalendarProviderOut(
                provider="microsoft",
                label="Microsoft 365 and Teams",
                availability="planned",
                flag_enabled=False,
                configured=False,
                connected=False,
                status=None,
                account_email=None,
                steps=["Planned for a later version (M3)."],
            ),
            HireCalendarProviderOut(
                provider="zoom",
                label="Zoom",
                availability="planned",
                flag_enabled=False,
                configured=False,
                connected=False,
                status=None,
                account_email=None,
                steps=["Planned for a later version (M3)."],
            ),
        ],
    )


async def _google_ready(session: AsyncSession) -> Any:
    config = providers.google_config()
    if not await providers.google_enabled(session):
        raise ConflictError("Google Calendar is turned off for this organisation")
    if config is None:
        raise ServiceUnavailableError("Google Calendar is not set up on this server")
    return config


def _secret(request: Request) -> str:
    secret = env(request).links.secret
    if not secret:
        raise ServiceUnavailableError("signed links are not configured here")
    return secret


@router.post("/scheduling/integrations/google/start", response_model=HireConnectOut)
async def google_start(
    request: Request, actor: CurrentActor, session: OrgSession
) -> HireConnectOut:
    authorize(actor, INTERVIEW_READ)
    _person(actor)
    config = await _google_ready(session)
    state = connect_state(_secret(request), actor.org_id, actor.id)
    async with providers.google_client() as client:
        url = GoogleOAuth(config, client).authorization_url(state, login_hint=actor.email)
    return HireConnectOut(url=url)


@router.post("/scheduling/integrations/google/callback", response_model=HireCalendarIntegrationsOut)
async def google_callback(
    body: HireConnectIn, request: Request, actor: CurrentActor, session: OrgSession
) -> HireCalendarIntegrationsOut:
    authorize(actor, INTERVIEW_READ)
    _person(actor)
    config = await _google_ready(session)
    claim = read_connect_state(_secret(request), body.state)
    if claim is None or claim.org_id != actor.org_id or claim.user_id != actor.id:
        raise BadRequestError("this connection request is not valid any more; start again")
    async with providers.google_client() as client:
        try:
            tokens = await GoogleOAuth(config, client).exchange_code(body.code)
        except CalendarError as exc:
            raise _calendar_failed(exc) from None
    await providers.save_google_connection(
        session,
        actor,
        refresh_token=tokens.refresh_token or "",
        scopes=tokens.scope,
        account_email=actor.email,
    )
    return await integrations(actor, session)


@router.post(
    "/scheduling/integrations/google/disconnect", response_model=HireCalendarIntegrationsOut
)
async def google_disconnect(
    actor: CurrentActor, session: OrgSession
) -> HireCalendarIntegrationsOut:
    authorize(actor, INTERVIEW_READ)
    _person(actor)
    await providers.disconnect(session, actor, "google")
    return await integrations(actor, session)


# ------------------------------------------------------------------ public (candidates)


def _limiter(request: Request, kind: str) -> SlidingWindowLimiter:
    store: dict[str, SlidingWindowLimiter] | None = getattr(
        request.app.state, "hire_schedule_limiters", None
    )
    if store is None:
        store = {k: SlidingWindowLimiter(n, w) for k, (n, w) in PUBLIC_LIMITS.items()}
        request.app.state.hire_schedule_limiters = store
    return store[kind]


async def _claimed(
    request: Request, session: AsyncSession, token: str, kind: str, *, lock: bool = False
) -> HireInterview:
    """The interview the token opens (409/410/404 otherwise; the session is bound)."""
    _limiter(request, kind).hit(client_ip(request))
    secret = env(request).links.secret
    if not secret:
        raise ServiceUnavailableError("scheduling links are not available here")
    claim = read_schedule_token(secret, token)
    if claim is None:
        raise NotFoundError("this link is not valid any more")
    set_tenant(session, claim.org_id, None)
    query = select(HireInterview).where(HireInterview.id == claim.interview_id)
    if lock:
        query = query.with_for_update()
    interview = await session.scalar(query)
    if (
        interview is None
        or interview.application_id != claim.application_id
        or interview.stage_key != claim.stage_key
    ):
        raise NotFoundError("this link is not valid any more")
    if interview.link_nonce != claim.nonce:
        if interview.status == "cancelled":
            return interview
        raise service.LinkUsedError("this link was used; use the link in your latest email")
    return interview


def _candidate_tz(value: str | None, interview: HireInterview) -> str:
    if value:
        try:
            zone(value)
            return value
        except AvailabilityError:
            pass
    return interview.candidate_timezone


async def _public_out(
    request: Request,
    session: AsyncSession,
    interview: HireInterview,
    tz: str,
    *,
    with_slots: bool,
    token: str | None = None,
) -> HirePublicScheduleOut:
    job = await session.scalar(select(HireJob).where(HireJob.id == interview.job_id))
    locale = interview.candidate_locale
    title = ((job.title_ar if locale == "ar" else "") or job.title) if job else ""
    panel_ids = await service.panel_of(session, interview.id)
    state = (
        "cancelled"
        if interview.status == "cancelled"
        else (
            "booked" if interview.starts_at is not None and interview.status in OPEN else "choose"
        )
    )
    if interview.status not in OPEN and state != "cancelled":
        state = "closed"
    slots_out: list[HirePublicSlotOut] = []
    if with_slots and interview.status in OPEN and panel_ids:
        found = await service.suggest(
            session,
            panel_ids,
            duration_min=interview.duration_min,
            timezone=tz,
            exclude=interview,
            per_day=8,
            max_days=7,
        )
        zone_ = zone(tz)
        for s in found.slots:
            local = s.start.astimezone(zone_)
            slots_out.append(
                HirePublicSlotOut(
                    start=s.start,
                    end=s.end,
                    date=local.date().isoformat(),
                    time=f"{local:%H:%M}",
                    sociable=sociable(s, tz),
                )
            )
    return HirePublicScheduleOut(
        state=state,
        organisation=await organisation_name(session, interview.org_id),
        job_title=title,
        duration_min=interview.duration_min,
        panel_size=len(panel_ids),
        timezone=tz,
        starts_at=interview.starts_at if state == "booked" else None,
        ends_at=interview.ends_at if state == "booked" else None,
        meeting_url=service.meeting_url(interview) if state == "booked" else None,
        slots=slots_out,
        token=token,
    )


def _token_of(request: Request, interview: HireInterview) -> str | None:
    url = service.schedule_url(env(request), interview)
    return url.split("#", 1)[1] if url and "#" in url else None


@router.post("/public/scheduling/read", response_model=HirePublicScheduleOut, tags=["hire-public"])
async def public_read(
    body: HirePublicScheduleIn, request: Request, session: RequestSession
) -> HirePublicScheduleOut:
    interview = await _claimed(request, session, body.token, "read")
    tz = _candidate_tz(body.timezone, interview)
    return await _public_out(request, session, interview, tz, with_slots=True)


@router.post("/public/scheduling/book", response_model=HirePublicScheduleOut, tags=["hire-public"])
async def public_book(
    body: HirePublicBookIn, request: Request, session: RequestSession
) -> HirePublicScheduleOut:
    interview = await _claimed(request, session, body.token, "write", lock=True)
    if interview.status not in OPEN:
        raise ConflictError("this interview is closed")
    tz = _candidate_tz(body.timezone, interview)
    await service.candidate_book(
        session, env(request), interview, starts_at=body.starts_at, timezone=tz
    )
    return await _public_out(
        request, session, interview, tz, with_slots=False, token=_token_of(request, interview)
    )


@router.post(
    "/public/scheduling/cancel", response_model=HirePublicScheduleOut, tags=["hire-public"]
)
async def public_cancel(
    body: HirePublicScheduleIn, request: Request, session: RequestSession
) -> HirePublicScheduleOut:
    interview = await _claimed(request, session, body.token, "write", lock=True)
    if interview.status not in OPEN:
        raise ConflictError("this interview is closed")
    await service.candidate_cancel(session, env(request), interview)
    tz = _candidate_tz(body.timezone, interview)
    return await _public_out(request, session, interview, tz, with_slots=False)
