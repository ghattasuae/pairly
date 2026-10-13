# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic copy: letters ruff mistakes for Latin look-alikes)
"""Interview emails: what they say, their calendar files, and who gets them.

Candidate emails go through the engine's ``notify`` to the candidate's address (an
external recipient: transactional, always sent), in their language and time zone, and
are kept in the conversation (``hire_messages``, so the anti-ghosting clock restarts).
The time is written out here (``interview_details``) in the candidate's own time zone.

Interviewer emails go to each panelist as a Pairly user (their preferences apply; the
renderer shows ``starts_at`` in their own time zone). The candidate shows as the label
blind mode allows (``hidden_identity``): "Candidate <reference>" until a stage reveals
them, read through the pipeline's reveal state.

Calendar files: one UID per interview (``iv-<id>@pairly``) and the interview's
``ics_sequence``. ``METHOD:REQUEST`` for a new or moved time, ``METHOD:CANCEL`` for a
cancellation. The candidate's file lists only the candidate; a panelist's file lists the
panel (never the candidate's address while their identity is hidden).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType
from pairly_core.crypto.types import seal
from pairly_core.errors import NotFoundError
from pairly_core.notifications.email import Attachment
from pairly_core.notifications.models import Urgency
from pairly_core.notifications.service import Extras, Recipient, notify
from pairly_core.notifications.settings import NotificationSettings
from pairly_core.notifications.templates import resolve_template, source_of
from pairly_core.notifications.templating import TemplateRenderError, render_source
from pairly_core.scheduling.availability import zone
from pairly_core.scheduling.ics import IcsEvent, IcsPerson, build_ics
from pairly_hire.links import LinkConfig, links_from_env, links_from_settings
from pairly_hire.messaging.compose import Author, mark_updated
from pairly_hire.messaging.models import HireMessage, MessagePurpose, MessageStatus
from pairly_hire.pipeline.hooks import application_blind, screening_summary
from pairly_hire.pipeline.records import AppRecord, organisation_name
from pairly_hire.scheduling.models import HireInterview
from pairly_hire.scheduling.rules import Interviewer
from pairly_hire.scheduling.templates import BRIEFING
from pairly_hire.sealing import read_sealed


def _sealed(model: Any, column: str, value: Any, *, org_id: uuid.UUID, row_id: object) -> Any:
    """``seal`` typed as ``Any``: an encrypted column takes the plaintext wrapper."""
    return seal(model, column, value, org_id=org_id, row_id=row_id)


WORKFLOW: Final = "hire"
DEFAULT_SENDER: Final = "no-reply@pairly.localhost"

_WEEKDAYS: Final = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "ar": ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"),
}
_MONTHS: Final = {
    "en": (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ),
    "ar": (
        "يناير",
        "فبراير",
        "مارس",
        "أبريل",
        "مايو",
        "يونيو",
        "يوليو",
        "أغسطس",
        "سبتمبر",
        "أكتوبر",
        "نوفمبر",
        "ديسمبر",
    ),
}


@dataclass(frozen=True, slots=True)
class SchedulingEnv:
    """Links and the sender address (the calendar files' organiser)."""

    links: LinkConfig
    sender_email: str = DEFAULT_SENDER

    @classmethod
    def from_settings(
        cls, settings: NotificationSettings | None, *, web_url: str | None = None
    ) -> SchedulingEnv:
        links = links_from_settings(settings)
        if links.web_url is None and web_url:
            links = LinkConfig(links.secret, web_url.rstrip("/"))
        return cls(links, _address(settings.smtp_from if settings else None))

    @classmethod
    def from_environment(cls) -> SchedulingEnv:
        import os

        return cls(links_from_env(), _address(os.environ.get("PAIRLY_SMTP_FROM")))


def _address(value: str | None) -> str:
    _, addr = parseaddr(value or "")
    return addr if "@" in addr else DEFAULT_SENDER


def ics_uid(interview_id: uuid.UUID) -> str:
    return f"iv-{interview_id.hex}@pairly"


def when_text(start: datetime, end: datetime, tz: str, locale: str) -> str:
    """``Tuesday 13 October 2026, 10:00 to 10:45 (Asia/Dubai)`` (plain digits)."""
    lang = locale if locale in _WEEKDAYS else "en"
    zone_ = zone(tz)
    a, b = start.astimezone(zone_), end.astimezone(zone_)
    day = f"{_WEEKDAYS[lang][a.weekday()]} {a.day} {_MONTHS[lang][a.month - 1]} {a.year}"
    if lang == "ar":
        return f"{day}، من {a:%H:%M} إلى {b:%H:%M} ({tz})"
    return f"{day}, {a:%H:%M} to {b:%H:%M} ({tz})"


def interview_details(
    interview: HireInterview, *, panel_size: int, meeting_url: str | None, cancelled: bool = False
) -> str:
    """The candidate's text: when (their zone), how long, with how many people, the link."""
    lang = interview.candidate_locale
    minutes = interview.duration_min
    if interview.starts_at is None or interview.ends_at is None:
        if lang == "ar":
            return f"مكالمة فيديو لمدة {minutes} دقيقة مع {panel_size} من أعضاء الفريق."
        people = "1 person" if panel_size == 1 else f"{panel_size} people"
        return f"A {minutes} minute video call with {people} from the team."
    when = when_text(
        interview.starts_at,
        interview.ends_at,
        interview.candidate_timezone,
        interview.candidate_locale,
    )
    if lang == "ar":
        text = f"{when}. مكالمة فيديو لمدة {minutes} دقيقة مع {panel_size} من أعضاء الفريق."
        if meeting_url and not cancelled:
            text += f"\nرابط المكالمة: {meeting_url}"
        return text
    people = "1 person" if panel_size == 1 else f"{panel_size} people"
    text = f"{when}. A {minutes} minute video call with {people} from the team."
    if meeting_url and not cancelled:
        text += f"\nJoin the call: {meeting_url}"
    return text


async def briefing_kit(session: AsyncSession, record: AppRecord, stage_key: str) -> str | None:
    """The interview kit for the briefing (brief 3.7.1): the stored AI kit for this
    candidate and stage, else the template kit from the plan. Never a CV quote, never a
    model call here (booking does not wait on a model). None without a plan."""
    from pairly_hire.interviewing.kits import build_kit, kit_text
    from pairly_hire.interviewing.plan import interview_stage

    found = await interview_stage(session, record.job, stage_key)
    if found is None:
        return None
    try:
        kit = await build_kit(session, record, found[0].key, "en", router=None)
    except NotFoundError:
        return None
    return kit_text(kit) or None


def meeting_url_of(interview: HireInterview) -> str | None:
    value = read_sealed(interview, "meeting_url")
    return value if isinstance(value, str) and value else None


Method = Literal["REQUEST", "CANCEL"]


def calendar_file(
    interview: HireInterview,
    *,
    method: Method,
    summary: str,
    description: str,
    attendees: Sequence[IcsPerson],
    organizer: IcsPerson,
    meeting_url: str | None,
) -> Attachment | None:
    """The ``.ics`` attachment, or None for an interview without a time."""
    if interview.starts_at is None or interview.ends_at is None:
        return None
    text = build_ics(
        IcsEvent(
            uid=interview.ics_uid,
            sequence=interview.ics_sequence,
            start=interview.starts_at,
            end=interview.ends_at,
            summary=summary,
            organizer=organizer,
            attendees=attendees,
            description=description,
            location=meeting_url or "",
            url=meeting_url,
        ),
        method,
    )
    name = "cancel.ics" if method == "CANCEL" else "invite.ics"
    return Attachment(name, "text/calendar", text, {"method": method})


# ------------------------------------------------------------------ candidate


@dataclass(frozen=True, slots=True)
class CandidateMail:
    template_key: str
    # Extra template values (manage_url, schedule_url...).
    values: Mapping[str, Any]
    method: Method | None
    dedupe: str


async def send_to_candidate(
    session: AsyncSession,
    env: SchedulingEnv,
    interview: HireInterview,
    record: AppRecord,
    mail: CandidateMail,
    *,
    author: Author,
    panel_size: int,
) -> uuid.UUID | None:
    """Queue one email to the candidate (with the calendar file when ``mail.method``),
    keep it in the conversation, restart the anti-ghosting clock. Returns the
    notification id (None when it could not be queued)."""
    locale = interview.candidate_locale
    company = await organisation_name(session, record.app.org_id)
    job_title = (record.job.title_ar if locale == "ar" else "") or record.job.title
    meeting_url = meeting_url_of(interview)
    cancelled = mail.method == "CANCEL"
    details = interview_details(
        interview, panel_size=panel_size, meeting_url=meeting_url, cancelled=cancelled
    )
    values: dict[str, Any] = {
        "candidate_name": record.full_name,
        "job_title": job_title,
        "company": company,
        "interview_details": details,
        **mail.values,
    }
    template = await resolve_template(
        session, key=mail.template_key, channel="email", locale=locale
    )
    if template is None:
        return None
    specs = source_of(template).variables
    values = {k: v for k, v in values.items() if k in specs}
    attachments: tuple[Attachment, ...] = ()
    if mail.method is not None:
        summary = (
            f"مقابلة: {job_title}، {company}"
            if locale == "ar"
            else f"Interview: {job_title}, {company}"
        )
        found = calendar_file(
            interview,
            method=mail.method,
            summary=summary,
            description=details,
            attendees=(IcsPerson(record.email, record.full_name or None),),
            organizer=IcsPerson(env.sender_email, company or None),
            meeting_url=None if cancelled else meeting_url,
        )
        attachments = (found,) if found is not None else ()
    results = await notify(
        session,
        recipient=Recipient.external(
            record.email, locale=locale, timezone=interview.candidate_timezone
        ),
        template_key=mail.template_key,
        variables=values,
        dedupe_key=mail.dedupe,
        channels=["email"],
        extras=Extras(attachments=attachments),
        workflow=WORKFLOW,
        actor_type=author.type,
        actor_id=author.id,
    )
    sent = next((r for r in results if r.channel == "email"), None)
    if sent is None or sent.notification_id is None:
        return None
    if sent.replayed:
        return sent.notification_id
    now = datetime.now(UTC)
    message = HireMessage(
        id=uuid.uuid4(),
        org_id=record.app.org_id,
        application_id=record.app.id,
        candidate_id=record.candidate.id,
        direction="outbound",
        template_key=mail.template_key,
        locale=locale,
        purpose=MessagePurpose.MANUAL.value,
        sensitive=False,
        status=MessageStatus.QUEUED.value,
        created_by_type=author.type.value,
        created_by_id=author.id,
        created_at=now,
        updated_at=now,
        sent_at=now,
        notification_id=sent.notification_id,
    )
    message.variables = _sealed(
        HireMessage, "variables", values, org_id=message.org_id, row_id=message.id
    )
    try:
        rendered = render_source(source_of(template), values, tz=interview.candidate_timezone)
        message.rendered = _sealed(
            HireMessage,
            "rendered",
            {"subject": rendered.subject, "text": rendered.text},
            org_id=message.org_id,
            row_id=message.id,
        )
    except TemplateRenderError:
        pass
    session.add(message)
    await mark_updated(record, session, now)
    return sent.notification_id


# ------------------------------------------------------------------ panel


async def candidate_view(session: AsyncSession, record: AppRecord) -> tuple[str, str, bool]:
    """The candidate as interviewers may see them now: label, short summary, hidden."""
    blind = await application_blind(session, record.app.id)
    hidden = record.identity_hidden(blind)
    label = record.label(blind)
    stage = record.stage
    lines = [f"Stage: {stage.label}."]
    summary = await screening_summary(session, record.app.id)
    if summary is not None:
        if summary.overall:
            lines.append(f"Screening: {summary.overall}.")
        for criterion in summary.criteria[:6]:
            lines.append(f"- {criterion.criterion}: {criterion.result.replace('_', ' ')}")
    else:
        lines.append("Not screened yet.")
    if hidden:
        lines.append("Blind review: the name and contact details stay hidden until a later stage.")
    return label, "\n".join(lines), hidden


@dataclass(frozen=True, slots=True)
class PanelMail:
    template_key: str
    method: Method | None
    dedupe: str
    urgent: bool = False
    channels: tuple[str, ...] = ("email",)


async def send_to_panel(
    session: AsyncSession,
    env: SchedulingEnv,
    interview: HireInterview,
    record: AppRecord,
    people: Sequence[Interviewer],
    mail: PanelMail,
    *,
    author: Author,
    everyone: Sequence[Interviewer] | None = None,
) -> int:
    """Queue ``mail`` to each of ``people`` (``everyone``: the panel the calendar file
    lists, defaults to ``people``). Returns how many were queued."""
    if interview.starts_at is None or interview.ends_at is None:
        return 0
    label, summary, hidden = await candidate_view(session, record)
    meeting_url = meeting_url_of(interview)
    job_title = record.job.title
    company = await organisation_name(session, record.app.org_id)
    listed = list(everyone if everyone is not None else people)
    panel_names = ", ".join(p.name for p in listed)
    base = env.links.web_url
    interview_url = (
        f"{base}/en/hire/jobs/{record.job.id}/pipeline?candidate={record.app.id}" if base else None
    )
    attachees = [IcsPerson(p.email, p.name) for p in listed]
    if not hidden and mail.method == "REQUEST":
        attachees.append(IcsPerson(record.email, record.full_name or None))

    def ics(description: str) -> Attachment | None:
        if mail.method is None:
            return None
        return calendar_file(
            interview,
            method=mail.method,
            summary=f"Interview: {label}, {job_title}",
            description=description,
            attendees=attachees,
            organizer=IcsPerson(env.sender_email, company or None),
            meeting_url=None if mail.method == "CANCEL" else meeting_url,
        )

    attachment = ics(summary)
    values: dict[str, Any] = {
        "candidate_label": label,
        "job_title": job_title,
        "starts_at": interview.starts_at,
        "duration_min": interview.duration_min,
        "summary": summary,
        "panel": panel_names,
    }
    if meeting_url and mail.method != "CANCEL":
        values["meeting_url"] = meeting_url
    if interview_url:
        values["interview_url"] = interview_url
    briefing = mail.template_key == BRIEFING
    if briefing:
        kit = await briefing_kit(session, record, interview.stage_key)
        if kit:
            values["kit"] = kit
    queued = 0
    for person in people:
        own = values
        own_attachment = attachment
        if briefing and base:
            # The one-click link to this person's own scorecard, in the email and in
            # the calendar invitation (brief 5.3). Single use, bound to them.
            from pairly_hire.interviewing.links import issue_link

            token = await issue_link(session, interview, person.id)
            scorecard = f"{base}/en/scorecard#{token}"
            own = values | {"scorecard_url": scorecard}
            own_attachment = ics(f"{summary}\n\nYour scorecard (one click): {scorecard}")
        await notify(
            session,
            recipient=Recipient.user(person.id),
            template_key=mail.template_key,
            variables=own,
            dedupe_key=f"{mail.dedupe}:{person.id}",
            channels=list(mail.channels),
            urgency=Urgency.URGENT if mail.urgent else Urgency.NORMAL,
            extras=Extras(
                link=interview_url,
                attachments=(own_attachment,) if own_attachment is not None else (),
            ),
            workflow=WORKFLOW,
            actor_type=author.type if author.type is not ActorType.API_KEY else ActorType.SYSTEM,
            actor_id=author.id,
        )
        queued += 1
    return queued
