# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Interviews: find times, book, send self-scheduling links, move, cancel, outcomes.

Everything runs in the caller's transaction (a tenant-bound session) and is audited
(``hire.interview.*``). Emails are queued in the same transaction, so an interview and
its invitations commit or roll back together.

Finding times (``suggest``): the panel's rules (``rules.interviewers``), their busy time
from their calendars (``providers.calendar_set``; a calendar that cannot be read fails
the request: no slot is offered on a calendar we could not check), and the interviews
Pairly already booked for them (busy, and counted toward each person's daily cap).
Calendar blocks that are exactly a Pairly interview are dropped (the interview is
counted once, and moving an interview never collides with itself).

Holding a time (``_hold``), shared by every booking path:

1. The interview row is locked (``FOR UPDATE``) by the caller.
2. A transaction-scoped advisory lock per panelist, in a stable order, so two bookings
   that share a person run one after the other (never a deadlock: same order).
3. The slot is checked again under the locks, against calendars and committed
   interviews. A conflict raises ``SlotTakenError`` (409) unless a person forces it
   (recruiters may book over a conflict on purpose; candidates never can).

So two candidates racing for the last slot of the same panel get one booking and one
``slot_taken``, and one self-scheduling link books once (``link_nonce`` changes with
every booking; the old link reads as ``used``).

A first booking (by the team or by the candidate) moves the application to the job's
``interview`` stage when it is earlier in the pipeline, as a system move with the reason
"interview booked" (``pipeline.moves.move_for_interview``): never backwards, never past
``interview``.

Calendar events are written through the organiser's provider (the fake calendar, or
Google or Microsoft when connected) with the interview's ICS UID; the meeting link comes
from Zoom when the organisation uses it (``meetings``), else from the calendar (Meet,
Teams). A provider failure fails the change (fail closed).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.service import member_permissions
from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.types import seal
from pairly_core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PairlyError,
    ServiceUnavailableError,
)
from pairly_core.integrations.calendar.base import (
    CalendarError,
    CalendarUser,
    EventAttendee,
    EventRef,
    EventSpec,
    MeetingLink,
    MeetingProvider,
)
from pairly_core.rbac import RoleRegistry
from pairly_core.scheduling.availability import (
    AvailabilityError,
    Conflict,
    Interval,
    Panelist,
    conflicts,
    find_slots,
    spread,
    valid_timezone,
)
from pairly_hire.messaging import library
from pairly_hire.messaging.compose import Author
from pairly_hire.models import ApplicationStatus
from pairly_hire.pipeline.moves import move_for_interview
from pairly_hire.pipeline.records import AppRecord, load_record
from pairly_hire.scheduling import emails, meetings, operations
from pairly_hire.scheduling import templates as tpl
from pairly_hire.scheduling.emails import CandidateMail, PanelMail, SchedulingEnv
from pairly_hire.scheduling.models import (
    ACTIVE,
    OPEN,
    HireInterview,
    HireInterviewPanelist,
    InterviewStatus,
)
from pairly_hire.scheduling.permissions import INTERVIEW_READ
from pairly_hire.scheduling.providers import CalendarSet, Person, calendar_set, google_client
from pairly_hire.scheduling.rules import Interviewer, OrgScheduling, interviewers, org_scheduling
from pairly_hire.scheduling.tokens import new_nonce, schedule_token
from pairly_hire.sealing import read_sealed

log = structlog.get_logger("pairly_hire.scheduling")


def _sealed(model: Any, column: str, value: Any, *, org_id: uuid.UUID, row_id: object) -> Any:
    """``seal`` typed as ``Any``: an encrypted column takes the plaintext wrapper."""
    return seal(model, column, value, org_id=org_id, row_id=row_id)


WORKFLOW: Final = "hire"
ENTITY: Final = "hire.interview"
CANDIDATE_ACTOR: Final = "pairly.hire.candidate"
MAX_PANEL: Final = 6
SUGGEST_PER_DAY: Final = 4
SUGGEST_DAYS: Final = 5


class SlotTakenError(ConflictError):
    code = "slot_taken"
    title = "This time is not free any more"

    def __init__(self, found: Sequence[Conflict]) -> None:
        reasons = sorted({c.reason for c in found})
        super().__init__("not free: " + ", ".join(reasons))
        self.conflicts = list(found)


class LinkUsedError(PairlyError):
    status = 410
    code = "link_used"
    title = "This link was already used"


class CalendarUnavailableError(ServiceUnavailableError):
    code = "calendar_unavailable"
    title = "A calendar could not be read. Try again in a minute."


# ------------------------------------------------------------------ reading


async def panel_of(session: AsyncSession, interview_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(HireInterviewPanelist.user_id)
        .where(
            HireInterviewPanelist.interview_id == interview_id,
            HireInterviewPanelist.removed_at.is_(None),
        )
        .order_by(HireInterviewPanelist.created_at, HireInterviewPanelist.user_id)
    )
    return list(rows)


async def get_interview(
    session: AsyncSession, interview_id: uuid.UUID, *, lock: bool = False
) -> HireInterview:
    query = select(HireInterview).where(HireInterview.id == interview_id)
    if lock:
        query = query.with_for_update()
    found = await session.scalar(query)
    if found is None:
        raise NotFoundError("interview not found")
    return found


async def interviews_for_application(
    session: AsyncSession, application_id: uuid.UUID
) -> list[HireInterview]:
    rows = await session.scalars(
        select(HireInterview)
        .where(HireInterview.application_id == application_id)
        .order_by(HireInterview.created_at.desc())
    )
    return list(rows)


async def booked_for(
    session: AsyncSession,
    user_ids: Sequence[uuid.UUID],
    window: Interval,
    *,
    exclude: uuid.UUID | None = None,
) -> dict[uuid.UUID, list[Interval]]:
    """Interviews Pairly holds for these people inside ``window`` (by person)."""
    query = (
        select(HireInterviewPanelist.user_id, HireInterview.starts_at, HireInterview.ends_at)
        .join(HireInterview, HireInterview.id == HireInterviewPanelist.interview_id)
        .where(
            HireInterviewPanelist.user_id.in_(list(user_ids)),
            HireInterviewPanelist.removed_at.is_(None),
            HireInterview.status.in_(ACTIVE),
            HireInterview.starts_at < window.end,
            HireInterview.ends_at > window.start,
        )
    )
    if exclude is not None:
        query = query.where(HireInterview.id != exclude)
    out: dict[uuid.UUID, list[Interval]] = {u: [] for u in user_ids}
    for user_id, start, end in (await session.execute(query)).all():
        if start is not None and end is not None:
            out.setdefault(user_id, []).append(Interval(start, end))
    return out


@dataclass(frozen=True, slots=True)
class PanelView:
    """What availability looks like for a panel over a window."""

    people: list[Interviewer]
    panelists: list[Panelist]
    busy: dict[uuid.UUID, list[Interval]]
    window: Interval


def _people(found: dict[uuid.UUID, Interviewer], ids: Sequence[uuid.UUID]) -> list[Person]:
    return [Person(i, found[i].email, found[i].rules.timezone) for i in ids if i in found]


async def panel_view(
    session: AsyncSession,
    org: OrgScheduling,
    panel_ids: Sequence[uuid.UUID],
    window: Interval,
    *,
    exclude: HireInterview | None = None,
) -> PanelView:
    """Rules, calendars and booked interviews of a panel (see the module docstring)."""
    found = await interviewers(session, panel_ids, org)
    missing = [i for i in panel_ids if i not in found]
    if missing:
        raise BadRequestError("a panelist is not a member of this organisation")
    # Whole local days around the window, so daily caps see every interview of a day.
    wide = Interval(window.start - timedelta(days=1), window.end + timedelta(days=1))
    people = _people(found, panel_ids)
    async with calendar_set(session, _org_id(session), org.seed, people) as calendars:
        try:
            calendar_busy = await calendars.busy(people, wide)
        except CalendarError as exc:
            log.warning("calendar_read_failed", code=exc.code)
            raise CalendarUnavailableError() from None
    booked = await booked_for(
        session, panel_ids, wide, exclude=exclude.id if exclude is not None else None
    )
    own: set[Interval] = set()
    if exclude is not None and exclude.starts_at is not None and exclude.ends_at is not None:
        own.add(Interval(exclude.starts_at, exclude.ends_at))
    panelists: list[Panelist] = []
    busy_out: dict[uuid.UUID, list[Interval]] = {}
    for user_id in panel_ids:
        mine = set(booked.get(user_id, []))
        blocks = [b for b in calendar_busy.get(user_id, []) if b not in mine and b not in own]
        busy_out[user_id] = sorted([*blocks, *mine])
        panelists.append(
            Panelist(str(user_id), found[user_id].rules, busy=tuple(blocks), interviews=tuple(mine))
        )
    return PanelView([found[i] for i in panel_ids], panelists, busy_out, window)


def _org_id(session: AsyncSession) -> uuid.UUID:
    from pairly_core.db import current_org

    org_id = current_org(session)
    if org_id is None:
        raise BadRequestError("scheduling needs a tenant-bound session")
    return org_id


@dataclass(frozen=True, slots=True)
class Suggestions:
    timezone: str
    duration_min: int
    slots: list[Interval]
    view: PanelView
    all_slots: list[Interval] = field(default_factory=list)


def _window(
    org: OrgScheduling, now: datetime, start: datetime | None, days: int | None
) -> Interval:
    begin = max(start or now, now)
    span = timedelta(days=min(days or org.horizon_days, 60))
    return Interval(begin, begin + span)


def _duration(minutes: int) -> timedelta:
    if not 15 <= minutes <= 240:
        raise BadRequestError("an interview lasts 15 to 240 minutes")
    return timedelta(minutes=minutes)


def _tz(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    try:
        return valid_timezone(value)
    except AvailabilityError:
        raise BadRequestError("unknown time zone") from None


async def suggest(
    session: AsyncSession,
    panel_ids: Sequence[uuid.UUID],
    *,
    duration_min: int,
    timezone: str,
    exclude: HireInterview | None = None,
    start: datetime | None = None,
    days: int | None = None,
    now: datetime | None = None,
    per_day: int = SUGGEST_PER_DAY,
    max_days: int = SUGGEST_DAYS,
) -> Suggestions:
    """Slots where the whole panel is free, a short list spread over the first days
    (``slots``) and every one (``all_slots``)."""
    _check_panel(panel_ids)
    org = await org_scheduling(session)
    now = now or datetime.now(UTC)
    tz = _tz(timezone, org.timezone)
    window = _window(org, now, start, days)
    view = await panel_view(session, org, panel_ids, window, exclude=exclude)
    try:
        found = find_slots(
            view.panelists,
            window,
            _duration(duration_min),
            step=org.step,
            now=now,
            min_notice=org.notice,
        )
    except AvailabilityError as exc:
        raise BadRequestError(str(exc)) from None
    return Suggestions(
        tz, duration_min, spread(found, tz, per_day=per_day, days=max_days), view, found
    )


async def check_slot(
    session: AsyncSession,
    panel_ids: Sequence[uuid.UUID],
    slot: Interval,
    *,
    exclude: HireInterview | None = None,
    now: datetime | None = None,
) -> list[Conflict]:
    _check_panel(panel_ids)
    org = await org_scheduling(session)
    view = await panel_view(session, org, panel_ids, slot, exclude=exclude)
    return conflicts(view.panelists, slot, now=now or datetime.now(UTC), min_notice=timedelta(0))


def _check_panel(panel_ids: Sequence[uuid.UUID]) -> None:
    if not panel_ids or len(panel_ids) > MAX_PANEL or len(set(panel_ids)) != len(panel_ids):
        raise BadRequestError(f"a panel has 1 to {MAX_PANEL} different people")


async def validate_panel(
    session: AsyncSession, registry: RoleRegistry, panel_ids: Sequence[uuid.UUID]
) -> None:
    """Every panelist is a member who may read interviews (an interviewer at least)."""
    _check_panel(panel_ids)
    perms = await member_permissions(session, registry, panel_ids)
    if any(INTERVIEW_READ not in perms.get(p, frozenset()) for p in panel_ids):
        raise BadRequestError("every panelist must be on the hiring team")


# ------------------------------------------------------------------ holding a slot


def advisory_key(org_id: uuid.UUID, user_id: uuid.UUID) -> int:
    digest = hashlib.sha256(f"pairly.hire.interviewer|{org_id}|{user_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def lock_people(session: AsyncSession, org_id: uuid.UUID, ids: Sequence[uuid.UUID]) -> None:
    """Transaction-scoped advisory locks, one per person, in a stable order."""
    for key in sorted({advisory_key(org_id, i) for i in ids}):
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


async def _hold(
    session: AsyncSession,
    interview: HireInterview,
    panel_ids: Sequence[uuid.UUID],
    slot: Interval,
    *,
    force: bool,
    now: datetime,
    min_notice: timedelta,
) -> None:
    await lock_people(session, interview.org_id, panel_ids)
    org = await org_scheduling(session)
    view = await panel_view(session, org, panel_ids, slot, exclude=interview)
    found = conflicts(view.panelists, slot, now=now, min_notice=min_notice)
    if found and not force:
        raise SlotTakenError(found)
    if found:
        # Booked over a conflict on purpose: audited by the caller with the reasons.
        log.info("interview_forced", interview_id=str(interview.id))


# ------------------------------------------------------------------ calendar events


async def _organizer(session: AsyncSession, interview: HireInterview) -> Interviewer:
    org = await org_scheduling(session)
    found = await interviewers(session, [interview.organizer_id], org)
    person = found.get(interview.organizer_id)
    if person is None:  # pragma: no cover - foreign key
        raise NotFoundError("organiser not found")
    return person


async def _event_spec(
    session: AsyncSession, interview: HireInterview, record: AppRecord, panel: list[Interviewer]
) -> EventSpec:
    assert interview.starts_at is not None  # noqa: S101 (booked)
    assert interview.ends_at is not None  # noqa: S101 (booked)
    label, summary, hidden = await emails.candidate_view(session, record)
    attendees = [EventAttendee(p.email, p.name, user_key=str(p.id)) for p in panel]
    if not hidden:
        attendees.append(EventAttendee(record.email, record.full_name or None))
    return EventSpec(
        uid=interview.ics_uid,
        sequence=interview.ics_sequence,
        summary=f"Interview: {label}, {record.job.title}",
        start=interview.starts_at,
        end=interview.ends_at,
        attendees=attendees,
        description=summary,
        want_meeting=True,
        meeting_url=emails.meeting_url_of(interview)
        if interview.meeting_provider == "manual"
        else None,
        timezone=interview.candidate_timezone,
    )


async def _write_event(
    session: AsyncSession,
    interview: HireInterview,
    record: AppRecord,
    panel: list[Interviewer],
    *,
    cancel: bool = False,
) -> None:
    organizer = await _organizer(session, interview)
    org = await org_scheduling(session)
    person = Person(organizer.id, organizer.email, organizer.rules.timezone)
    zoom = await meetings.zoom_credentials(session)
    async with AsyncExitStack() as stack:
        calendars = await stack.enter_async_context(
            calendar_set(session, interview.org_id, org.seed, [person])
        )
        meeting: MeetingProvider | None = None
        if zoom is not None:
            client = await stack.enter_async_context(google_client())
            meeting = meetings.zoom_provider(client, zoom)
        await _write_with(
            calendars, session, interview, record, panel, person, cancel=cancel, meeting=meeting
        )


def _intended(interview: HireInterview, action: str) -> dict[str, Any]:
    """What a journalled write asks for (``operations``), enough to undo it later."""
    return {
        "action": action,
        "uid": interview.ics_uid,
        "sequence": interview.ics_sequence,
        "status": interview.status,
        "starts_at": interview.starts_at.isoformat() if interview.starts_at else None,
        "ends_at": interview.ends_at.isoformat() if interview.ends_at else None,
    }


def _made_id(result: object) -> str | None:
    if isinstance(result, EventRef):
        return result.event_id
    if isinstance(result, MeetingLink):
        return result.meeting_id
    return None


async def _external[T](
    session: AsyncSession,
    interview: HireInterview,
    organizer_id: uuid.UUID,
    *,
    provider: str,
    kind: str,
    action: str,
    external_id: str | None,
    call: Callable[[str | None], Awaitable[T]],
) -> T:
    """One provider call, journalled when the provider is external (``operations``):
    recorded before, its id stored after, settled in this transaction."""
    if not operations.journalled(provider):
        return await call(None)
    op = await operations.begin(
        session,
        org_id=interview.org_id,
        interview_id=interview.id,
        organizer_id=organizer_id,
        provider=provider,
        kind=kind,
        action=action,
        external_id=external_id,
        intended=_intended(interview, action),
    )
    try:
        result = await call(op.client_request_id)
    except CalendarError as exc:
        await operations.failed(session, op, exc)
        raise
    made = _made_id(result)
    if action == "create" and made and made != op.client_request_id:
        await operations.written(session, op, made)
    await operations.settle(session, op)
    return result


async def _write_meeting(
    session: AsyncSession,
    meeting: MeetingProvider | None,
    interview: HireInterview,
    user: CalendarUser,
    organizer_id: uuid.UUID,
    spec: EventSpec,
    *,
    cancel: bool,
) -> EventSpec:
    """The interview's meeting outside the calendar (Zoom). Returns the event to write:
    with the Zoom link as its location when there is one.

    * A Zoom meeting no longer used (cancelled, or a person typed another link) is
      deleted, when Zoom is still configured.
    * Otherwise an existing Zoom meeting is moved with the interview.
    * A first booking without a link gets a new Zoom meeting when Zoom is in use."""
    current = emails.meeting_url_of(interview)
    zoom_id = interview.meeting_external_id
    if zoom_id and (cancel or interview.meeting_provider != "zoom"):
        if meeting is not None:
            zoom = meeting

            async def delete(_key: str | None) -> None:
                await zoom.delete_meeting(user, zoom_id)

            await _external(
                session,
                interview,
                organizer_id,
                provider=zoom.name,
                kind="meeting",
                action="cancel",
                external_id=zoom_id,
                call=delete,
            )
            interview.meeting_external_id = None
        return spec
    if interview.meeting_provider == "zoom" and current:
        if zoom_id and meeting is not None:
            zoom = meeting

            async def move(_key: str | None) -> None:
                await zoom.update_meeting(user, zoom_id, spec)

            await _external(
                session,
                interview,
                organizer_id,
                provider=zoom.name,
                kind="meeting",
                action="update",
                external_id=zoom_id,
                call=move,
            )
        return replace(spec, meeting_url=current, want_meeting=False)
    if cancel or meeting is None or current:
        return spec
    zoom = meeting

    async def create(_key: str | None) -> MeetingLink:
        return await zoom.create_meeting(user, spec)

    link = await _external(
        session,
        interview,
        organizer_id,
        provider=zoom.name,
        kind="meeting",
        action="create",
        external_id=None,
        call=create,
    )
    _set_meeting(interview, link.url, zoom.name)
    interview.meeting_external_id = (link.meeting_id or "")[:64] or None
    return replace(spec, meeting_url=link.url, want_meeting=False)


async def _write_with(
    calendars: CalendarSet,
    session: AsyncSession,
    interview: HireInterview,
    record: AppRecord,
    panel: list[Interviewer],
    person: Person,
    *,
    cancel: bool,
    meeting: MeetingProvider | None = None,
) -> None:
    """Write the interview to the organiser's calendar (and meeting provider). Every
    external call is journalled (``_external``), so a transaction that fails after the
    provider wrote leaves a pending operation for the reconciliation job."""
    provider = calendars.provider_for(person.id)
    user = calendars.user(person)
    spec = await _event_spec(session, interview, record, panel)
    ref = (
        EventRef(
            interview.calendar_provider or provider.name,
            interview.calendar_event_id,
            emails.meeting_url_of(interview),
            interview.meeting_provider,
        )
        if interview.calendar_event_id
        else None
    )
    try:
        spec = await _write_meeting(
            session, meeting, interview, user, person.id, spec, cancel=cancel
        )
        if cancel:
            if ref is not None:
                existing = ref

                async def cancel_call(_key: str | None) -> None:
                    await provider.cancel_event(user, existing, spec)

                await _external(
                    session,
                    interview,
                    person.id,
                    provider=provider.name,
                    kind="event",
                    action="cancel",
                    external_id=existing.event_id,
                    call=cancel_call,
                )
            return
        if ref is None or interview.calendar_provider != provider.name:

            async def create_call(key: str | None) -> EventRef:
                return await provider.create_event(user, replace(spec, client_request_id=key))

            ref = await _external(
                session,
                interview,
                person.id,
                provider=provider.name,
                kind="event",
                action="create",
                external_id=None,
                call=create_call,
            )
        else:
            existing = ref

            async def update_call(_key: str | None) -> EventRef:
                return await provider.update_event(user, existing, spec)

            ref = await _external(
                session,
                interview,
                person.id,
                provider=provider.name,
                kind="event",
                action="update",
                external_id=existing.event_id,
                call=update_call,
            )
    except CalendarError as exc:
        log.warning("calendar_write_failed", code=exc.code, interview_id=str(interview.id))
        raise CalendarUnavailableError() from None
    interview.calendar_provider = provider.name
    interview.calendar_event_id = ref.event_id[:256]
    if interview.meeting_provider not in ("manual", "zoom") and ref.meeting_url:
        _set_meeting(interview, ref.meeting_url, ref.meeting_provider or provider.name)


def _set_meeting(interview: HireInterview, url: str | None, provider: str | None) -> None:
    interview.meeting_url = (
        _sealed(HireInterview, "meeting_url", url, org_id=interview.org_id, row_id=interview.id)
        if url
        else None
    )
    interview.meeting_provider = provider if url else None


# ------------------------------------------------------------------ audit


async def audit_interview(
    session: AsyncSession,
    author: Author,
    action: str,
    interview: HireInterview,
    payload: dict[str, Any] | None = None,
) -> None:
    await append_audit(
        session,
        action=action,
        actor_type=author.type,
        actor_id=author.id,
        entity_type=ENTITY,
        entity_id=interview.id,
        workflow=WORKFLOW,
        payload={
            "application_id": str(interview.application_id),
            "stage_key": interview.stage_key,
            "status": interview.status,
            "starts_at": interview.starts_at.isoformat() if interview.starts_at else None,
            "duration_min": interview.duration_min,
            "ics_sequence": interview.ics_sequence,
            **(payload or {}),
        },
    )


# ------------------------------------------------------------------ links


def schedule_url(env: SchedulingEnv, interview: HireInterview) -> str | None:
    """The candidate's link (``/<locale>/schedule#<token>``), or None without a secret."""
    if not env.links.ready or interview.link_nonce is None or interview.link_expires_at is None:
        return None
    assert env.links.secret is not None  # noqa: S101 (ready)
    token = schedule_token(
        env.links.secret,
        org_id=interview.org_id,
        interview_id=interview.id,
        application_id=interview.application_id,
        stage_key=interview.stage_key,
        nonce=interview.link_nonce,
        expires_at=interview.link_expires_at,
    )
    return f"{env.links.web_url}/{interview.candidate_locale}/schedule#{token}"


def _rotate_link(interview: HireInterview, expires_at: datetime) -> None:
    interview.link_nonce = new_nonce()
    interview.link_expires_at = expires_at


def manage_url(env: SchedulingEnv, interview: HireInterview) -> str | None:
    """After a booking: a fresh single-use link to change or cancel, valid until the
    interview starts."""
    if interview.starts_at is None:
        return None
    _rotate_link(interview, interview.starts_at)
    return schedule_url(env, interview)


# ------------------------------------------------------------------ creating


def _active_record(record: AppRecord) -> None:
    if record.app.status != ApplicationStatus.RECEIVED.value or record.stage.terminal:
        raise ConflictError("this application is closed")


async def _new_interview(
    session: AsyncSession,
    author: Author,
    record: AppRecord,
    *,
    organizer_id: uuid.UUID,
    panel_ids: Sequence[uuid.UUID],
    duration_min: int,
    candidate_tz: str,
    stage_key: str,
    meeting_url: str | None,
) -> HireInterview:
    now = datetime.now(UTC)
    interview_id = uuid.uuid4()
    interview = HireInterview(
        id=interview_id,
        org_id=record.app.org_id,
        application_id=record.app.id,
        job_id=record.job.id,
        candidate_id=record.candidate.id,
        stage_key=stage_key,
        status=InterviewStatus.PROPOSED.value,
        duration_min=duration_min,
        candidate_timezone=candidate_tz,
        candidate_locale=record.locale,
        organizer_id=organizer_id,
        ics_uid=emails.ics_uid(interview_id),
        ics_sequence=0,
        created_by_type=author.type.value,
        created_by_id=author.id,
        created_at=now,
        updated_at=now,
    )
    if meeting_url:
        _set_meeting(interview, meeting_url, "manual")
    session.add(interview)
    await session.flush()
    for user_id in panel_ids:
        session.add(
            HireInterviewPanelist(
                id=uuid.uuid4(),
                org_id=interview.org_id,
                interview_id=interview.id,
                user_id=user_id,
                created_at=now,
            )
        )
    await session.flush()
    return interview


async def panel_people(session: AsyncSession, panel_ids: Sequence[uuid.UUID]) -> list[Interviewer]:
    org = await org_scheduling(session)
    found = await interviewers(session, panel_ids, org)
    return [found[i] for i in panel_ids if i in found]


async def _open_interview_for(session: AsyncSession, application_id: uuid.UUID) -> bool:
    count = await session.scalar(
        select(func.count())
        .select_from(HireInterview)
        .where(HireInterview.application_id == application_id, HireInterview.status.in_(OPEN))
    )
    return bool(count)


async def create_interview(
    session: AsyncSession,
    env: SchedulingEnv,
    registry: RoleRegistry,
    author: Author,
    organizer_id: uuid.UUID,
    record: AppRecord,
    *,
    mode: str,
    panel_ids: Sequence[uuid.UUID],
    duration_min: int,
    starts_at: datetime | None = None,
    candidate_timezone: str | None = None,
    meeting_url: str | None = None,
    force: bool = False,
) -> HireInterview:
    """A new interview for ``record`` at its current stage: ``book`` holds ``starts_at``
    now and sends the invitations; ``link`` sends the candidate a self-scheduling link."""
    _active_record(record)
    if mode not in {"book", "link"}:
        raise BadRequestError("mode is book or link")
    await validate_panel(session, registry, panel_ids)
    org = await org_scheduling(session)
    _duration(duration_min)
    if await _open_interview_for(session, record.app.id):
        raise ConflictError("this candidate already has an open interview: move or cancel it")
    interview = await _new_interview(
        session,
        author,
        record,
        organizer_id=organizer_id,
        panel_ids=panel_ids,
        duration_min=duration_min,
        candidate_tz=_tz(candidate_timezone, org.timezone),
        stage_key=record.stage_key,
        meeting_url=meeting_url,
    )
    if mode == "link":
        await _send_link(session, env, author, interview, record, panel_size=len(panel_ids))
        await audit_interview(session, author, "hire.interview.link_sent", interview)
        return interview
    if starts_at is None:
        raise BadRequestError("choose a time to book")
    await _book(
        session,
        env,
        author,
        interview,
        record,
        starts_at=starts_at,
        force=force,
        by_candidate=False,
    )
    return interview


async def _send_link(
    session: AsyncSession,
    env: SchedulingEnv,
    author: Author,
    interview: HireInterview,
    record: AppRecord,
    *,
    panel_size: int,
) -> None:
    org = await org_scheduling(session)
    expires = datetime.now(UTC) + timedelta(days=org.link_days)
    if interview.starts_at is not None:
        expires = min(expires, interview.starts_at)
    _rotate_link(interview, expires)
    url = schedule_url(env, interview)
    if url is None:
        raise ServiceUnavailableError("signed links are not configured here")
    interview.updated_at = datetime.now(UTC)
    await session.flush()
    await emails.send_to_candidate(
        session,
        env,
        interview,
        record,
        CandidateMail(
            library.INTERVIEW_INVITE,
            {"schedule_url": url},
            None,
            f"hire.iv:{interview.id}:link:{interview.link_nonce}",
        ),
        author=author,
        panel_size=panel_size,
    )


async def send_link(
    session: AsyncSession,
    env: SchedulingEnv,
    author: Author,
    interview: HireInterview,
    record: AppRecord,
) -> HireInterview:
    """Send (again) a self-scheduling link: a new link, the old one stops working."""
    if interview.status not in OPEN:
        raise ConflictError("this interview is closed")
    _active_record(record)
    panel = await panel_of(session, interview.id)
    await _send_link(session, env, author, interview, record, panel_size=len(panel))
    await audit_interview(session, author, "hire.interview.link_sent", interview)
    return interview


# ------------------------------------------------------------------ booking


async def _book(
    session: AsyncSession,
    env: SchedulingEnv,
    author: Author,
    interview: HireInterview,
    record: AppRecord,
    *,
    starts_at: datetime,
    force: bool,
    by_candidate: bool,
    panel_ids: Sequence[uuid.UUID] | None = None,
    added: Sequence[uuid.UUID] = (),
    removed: Sequence[uuid.UUID] = (),
    details_changed: bool = False,
) -> None:
    """Hold ``starts_at`` for the panel and tell everyone what changed.

    A change attendees must see (a new start or end, or ``details_changed``: a new
    meeting link) takes the next ``ics_sequence``; new panelists get the briefing,
    panelists taken off get a cancellation, the others the new time.
    """
    if starts_at.tzinfo is None:
        raise BadRequestError("times carry a time zone")
    now = datetime.now(UTC)
    org = await org_scheduling(session)
    panel_ids = list(panel_ids) if panel_ids is not None else await panel_of(session, interview.id)
    slot = Interval(starts_at, starts_at + timedelta(minutes=interview.duration_min))
    was_booked = interview.status in ACTIVE
    old_start = interview.starts_at
    new_time = (old_start, interview.ends_at) != (slot.start, slot.end)
    if not was_booked or new_time or added:
        notice = org.notice if by_candidate else timedelta(0)
        await _hold(
            session,
            interview,
            panel_ids,
            slot,
            force=force and not by_candidate,
            now=now,
            min_notice=notice,
        )
    changed = was_booked and (new_time or details_changed)
    interview.starts_at, interview.ends_at = slot.start, slot.end
    if not was_booked:
        interview.status = InterviewStatus.BOOKED.value
    elif changed:
        interview.ics_sequence += 1
        if new_time:
            interview.status = InterviewStatus.RESCHEDULED.value
            interview.reminded_24h_at = None
            interview.reminded_1h_at = None
    interview.updated_at = now
    panel = await panel_people(session, panel_ids)
    await _write_event(session, interview, record, panel)
    url = manage_url(env, interview)
    await session.flush()

    sequence = interview.ics_sequence
    key = f"hire.iv:{interview.id}:{sequence}:{slot.start.isoformat()}"
    if not was_booked or by_candidate:
        await emails.send_to_candidate(
            session,
            env,
            interview,
            record,
            CandidateMail(
                tpl.CONFIRMED, {"manage_url": url} if url else {}, "REQUEST", f"{key}:confirmed"
            ),
            author=author,
            panel_size=len(panel),
        )
    elif changed:
        await emails.send_to_candidate(
            session,
            env,
            interview,
            record,
            CandidateMail(
                library.INTERVIEW_RESCHEDULE,
                {"schedule_url": url} if url else {},
                "REQUEST",
                f"{key}:moved",
            ),
            author=author,
            panel_size=len(panel),
        )
    if not was_booked:
        await emails.send_to_panel(
            session,
            env,
            interview,
            record,
            panel,
            PanelMail(tpl.BRIEFING, "REQUEST", f"{key}:brief"),
            author=author,
        )
    else:
        taken_off = await panel_people(session, list(removed))
        if taken_off:
            await emails.send_to_panel(
                session,
                env,
                interview,
                record,
                taken_off,
                PanelMail(tpl.PANEL_CANCELLED, "CANCEL", f"{key}:off"),
                author=author,
            )
        new_people = [p for p in panel if p.id in set(added)]
        known = [p for p in panel if p.id not in set(added)]
        if new_people:
            await emails.send_to_panel(
                session,
                env,
                interview,
                record,
                new_people,
                PanelMail(tpl.BRIEFING, "REQUEST", f"{key}:brief"),
                author=author,
                everyone=panel,
            )
        if known and changed:
            await emails.send_to_panel(
                session,
                env,
                interview,
                record,
                known,
                PanelMail(tpl.PANEL_RESCHEDULED, "REQUEST", f"{key}:moved"),
                author=author,
                everyone=panel,
            )
    if not was_booked:
        action = "hire.interview.booked"
    elif new_time:
        action = "hire.interview.rescheduled"
    else:
        action = "hire.interview.updated"
    await audit_interview(
        session,
        author,
        action,
        interview,
        {
            "by": "candidate" if by_candidate else "team",
            "from": old_start.isoformat() if old_start else None,
            "panel": [str(p) for p in panel_ids],
            "added": [str(p) for p in added],
            "removed": [str(p) for p in removed],
            "forced": force and not by_candidate,
        },
    )
    if not was_booked:
        # A booked interview moves the application to the interview stage (never back).
        await move_for_interview(session, record.app.id, interview_id=interview.id)


async def _set_panel(
    session: AsyncSession,
    interview: HireInterview,
    current: Sequence[uuid.UUID],
    wanted: Sequence[uuid.UUID],
    now: datetime,
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Replace the panel (rows are kept: ``removed_at``). Returns (added, removed)."""
    removed = [p for p in current if p not in wanted]
    added = [p for p in wanted if p not in current]
    for user_id in [*removed, *added]:
        row = await session.scalar(
            select(HireInterviewPanelist).where(
                HireInterviewPanelist.interview_id == interview.id,
                HireInterviewPanelist.user_id == user_id,
            )
        )
        if user_id in removed:
            if row is not None:
                row.removed_at = now
        elif row is None:
            session.add(
                HireInterviewPanelist(
                    id=uuid.uuid4(),
                    org_id=interview.org_id,
                    interview_id=interview.id,
                    user_id=user_id,
                    created_at=now,
                )
            )
        else:
            row.removed_at = None
    await session.flush()
    return added, removed


async def update_interview(
    session: AsyncSession,
    env: SchedulingEnv,
    registry: RoleRegistry,
    author: Author,
    interview: HireInterview,
    record: AppRecord,
    *,
    starts_at: datetime | None = None,
    duration_min: int | None = None,
    panel_ids: Sequence[uuid.UUID] | None = None,
    meeting_url: str | None = None,
    clear_meeting_url: bool = False,
    candidate_timezone: str | None = None,
    force: bool = False,
) -> HireInterview:
    """One form for every change: time, length, panel, meeting link, the candidate's
    time zone. A proposed interview given a time is booked. See ``_book``."""
    if interview.status not in OPEN:
        raise ConflictError("this interview is closed")
    _active_record(record)
    now = datetime.now(UTC)
    current = await panel_of(session, interview.id)
    wanted = list(panel_ids) if panel_ids is not None else current
    if panel_ids is not None:
        await validate_panel(session, registry, wanted)
    changes: dict[str, Any] = {}
    if candidate_timezone is not None:
        interview.candidate_timezone = _tz(candidate_timezone, interview.candidate_timezone)
        changes["candidate_timezone"] = interview.candidate_timezone
    if duration_min is not None and duration_min != interview.duration_min:
        _duration(duration_min)
        interview.duration_min = duration_min
        changes["duration_min"] = duration_min
    link_changed = False
    if clear_meeting_url and interview.meeting_provider == "manual":
        _set_meeting(interview, None, None)
        link_changed = True
    elif meeting_url and meeting_url != emails.meeting_url_of(interview):
        _set_meeting(interview, meeting_url, "manual")
        link_changed = True
    added, removed = await _set_panel(session, interview, current, wanted, now)
    interview.updated_at = now
    target = starts_at or interview.starts_at
    if target is None:
        # Still waiting for the candidate's choice: only the details changed.
        await session.flush()
        await audit_interview(
            session,
            author,
            "hire.interview.updated",
            interview,
            {"changes": changes, "panel": [str(p) for p in wanted]},
        )
        return interview
    await _book(
        session,
        env,
        author,
        interview,
        record,
        starts_at=target,
        force=force,
        by_candidate=False,
        panel_ids=wanted,
        added=added,
        removed=removed,
        details_changed=link_changed,
    )
    return interview


# ------------------------------------------------------------------ cancel and outcome


async def cancel_interview(
    session: AsyncSession,
    env: SchedulingEnv,
    author: Author,
    interview: HireInterview,
    record: AppRecord,
    *,
    by_candidate: bool = False,
) -> HireInterview:
    """Cancel: everyone gets ``METHOD:CANCEL`` with the next sequence."""
    if interview.status not in OPEN:
        raise ConflictError("this interview is closed")
    now = datetime.now(UTC)
    had_time = interview.status in ACTIVE
    panel_ids = await panel_of(session, interview.id)
    panel = await panel_people(session, panel_ids)
    interview.status = InterviewStatus.CANCELLED.value
    interview.cancelled_at = now
    interview.cancelled_by = "candidate" if by_candidate else "team"
    interview.link_nonce = None
    interview.link_expires_at = None
    interview.updated_at = now
    if had_time:
        interview.ics_sequence += 1
        await _write_event(session, interview, record, panel, cancel=True)
    await session.flush()
    if had_time:
        await emails.send_to_candidate(
            session,
            env,
            interview,
            record,
            CandidateMail(
                tpl.CANCELLED, {}, "CANCEL", f"hire.iv:{interview.id}:{interview.ics_sequence}:off"
            ),
            author=author,
            panel_size=len(panel),
        )
        await emails.send_to_panel(
            session,
            env,
            interview,
            record,
            panel,
            PanelMail(
                tpl.PANEL_CANCELLED,
                "CANCEL",
                f"hire.iv:{interview.id}:{interview.ics_sequence}:off",
            ),
            author=author,
        )
    await audit_interview(
        session,
        author,
        "hire.interview.cancelled",
        interview,
        {"by": interview.cancelled_by},
    )
    return interview


OUTCOMES: Final = frozenset({InterviewStatus.COMPLETED.value, InterviewStatus.NO_SHOW.value})


async def record_outcome(
    session: AsyncSession,
    author: Author,
    user_id: uuid.UUID,
    interview: HireInterview,
    outcome: str,
) -> HireInterview:
    """After the start time: ``completed`` or ``no_show`` (no-show tracking). A second
    call can correct the first."""
    if outcome not in OUTCOMES:
        raise BadRequestError("outcome is completed or no_show")
    if interview.status not in ACTIVE | OUTCOMES or interview.starts_at is None:
        raise ConflictError("only a booked interview has an outcome")
    now = datetime.now(UTC)
    if interview.starts_at > now:
        raise ConflictError("the interview has not started yet")
    before = interview.status
    interview.status = outcome
    interview.outcome_at = now
    interview.outcome_by = user_id
    interview.link_nonce = None
    interview.link_expires_at = None
    interview.updated_at = now
    await session.flush()
    await audit_interview(
        session, author, "hire.interview.outcome_recorded", interview, {"from": before}
    )
    return interview


# ------------------------------------------------------------------ candidate side


async def candidate_record(session: AsyncSession, interview: HireInterview) -> AppRecord:
    record = await load_record(session, interview.application_id)
    _active_record(record)
    return record


async def candidate_book(
    session: AsyncSession,
    env: SchedulingEnv,
    interview: HireInterview,
    *,
    starts_at: datetime,
    timezone: str | None,
) -> HireInterview:
    """The candidate picks a time through their link (the caller checked the token and
    locked the row). Never forced; the organisation's minimum notice applies."""
    if interview.status not in OPEN:
        raise ConflictError("this interview is closed")
    record = await candidate_record(session, interview)
    if timezone:
        interview.candidate_timezone = _tz(timezone, interview.candidate_timezone)
    author = Author(ActorType.SYSTEM, CANDIDATE_ACTOR)
    await _book(
        session,
        env,
        author,
        interview,
        record,
        starts_at=starts_at,
        force=False,
        by_candidate=True,
    )
    return interview


async def candidate_cancel(
    session: AsyncSession, env: SchedulingEnv, interview: HireInterview
) -> HireInterview:
    record = await load_record(session, interview.application_id)
    author = Author(ActorType.SYSTEM, CANDIDATE_ACTOR)
    if interview.status == InterviewStatus.PROPOSED.value:
        interview.status = InterviewStatus.CANCELLED.value
        interview.cancelled_at = datetime.now(UTC)
        interview.cancelled_by = "candidate"
        interview.link_nonce = None
        interview.link_expires_at = None
        await session.flush()
        await audit_interview(
            session, author, "hire.interview.cancelled", interview, {"by": "candidate"}
        )
        return interview
    return await cancel_interview(session, env, author, interview, record, by_candidate=True)


def meeting_url(interview: HireInterview) -> str | None:
    value = read_sealed(interview, "meeting_url")
    return value if isinstance(value, str) and value else None
