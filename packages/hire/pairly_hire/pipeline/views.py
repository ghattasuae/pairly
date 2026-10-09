# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Read models: the kanban board of a job and the candidate profile with its timeline.

Both apply blind mode per application (``hooks.blind_applications``, screening's
``is_blind``, which reads the application's one reveal state): while an application is
blind, the card and the profile show "Candidate <reference>" and hide the contact
details and the CV file (which names the person), message previews show the label
instead of the name, and free text (notes, move reasons, message subjects and text)
goes through the blind redactor with the candidate's known identity
(``hooks.identity_redactor``). Everything else (stage, tags, flags) stays visible.
The routes never return self-identification answers (they live in
``pairly_hire.selfid``, which this package never imports).

The timeline lists every event of an application, newest first: the application, its
acknowledgement, flags (needs a person, duplicate hints), stage moves, notes, messages
(drafted, submitted for approval, sent, declined), a referral, and the screening result
when screening has one (``hooks.screening_summary``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.models import User
from pairly_core.notifications.models import Notification
from pairly_hire.blindtext import Redactor
from pairly_hire.messaging import library
from pairly_hire.messaging.models import HireMessage, MessageStatus
from pairly_hire.messaging.prefs import prefs_for
from pairly_hire.models import ApplicationStatus, HireApplication, HireCandidate, HireJob
from pairly_hire.pipeline.hooks import (
    ScreeningSummary,
    application_blind,
    blind_applications,
    identity_redactor,
    screening_summary,
)
from pairly_hire.pipeline.models import HireNote, HirePipelineEntry, HireReferral, HireStageMove
from pairly_hire.pipeline.records import AppRecord, load_record
from pairly_hire.pipeline.stages import FIRST, StageSet, stages_for
from pairly_hire.sealing import read_sealed

MAX_CARDS: Final = 500
OPEN_MESSAGE: Final = frozenset(
    {
        MessageStatus.DRAFT.value,
        MessageStatus.CHANGES_REQUESTED.value,
        MessageStatus.SUBMITTED.value,
        MessageStatus.AWAITING_APPROVAL.value,
    }
)


def days_between(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() // 86400))


@dataclass(frozen=True, slots=True)
class Card:
    application_id: uuid.UUID
    reference: str
    label: str
    identity_hidden: bool
    stage_key: str
    stage_entered_at: datetime
    submitted_at: datetime | None
    waiting_days: int
    sla_overdue: bool
    tags: list[str]
    needs_human_review: bool
    review_reasons: list[str]
    duplicate_hints: int
    referred: bool
    # The open draft or approval of the application, if any (``draft``, ``awaiting_approval``...).
    open_message: str | None
    open_message_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class Board:
    job: HireJob
    stages: StageSet
    cards: list[Card]
    blind_mode: bool
    sla_days: int


async def _users(session: AsyncSession, ids: set[str]) -> dict[str, str]:
    uuids: list[uuid.UUID] = []
    for value in ids:
        try:
            uuids.append(uuid.UUID(value))
        except ValueError:
            continue
    if not uuids:
        return {}
    rows = await session.execute(
        select(User.id, User.display_name, User.email).where(User.id.in_(uuids))
    )
    return {str(r.id): (r.display_name or r.email.split("@", 1)[0]).strip() for r in rows}


async def board(
    session: AsyncSession,
    job: HireJob,
    *,
    blind: bool,
    sla_days: int,
    now: datetime | None = None,
) -> Board:
    now = now or datetime.now(UTC)
    stages = await stages_for(session, job.id)
    rows = (
        await session.execute(
            select(HireApplication, HireCandidate, HirePipelineEntry)
            .join(HireCandidate, HireCandidate.id == HireApplication.candidate_id)
            .outerjoin(HirePipelineEntry, HirePipelineEntry.application_id == HireApplication.id)
            .where(
                HireApplication.job_id == job.id,
                HireApplication.status != ApplicationStatus.INCOMPLETE.value,
            )
            .order_by(HireApplication.submitted_at.asc(), HireApplication.id)
            .limit(MAX_CARDS)
        )
    ).all()
    app_ids = [a.id for a, _, _ in rows]
    blind_ids = await blind_applications(session, app_ids)
    referred = set(
        await session.scalars(
            select(HireReferral.application_id).where(HireReferral.application_id.in_(app_ids))
        )
    )
    open_messages: dict[uuid.UUID, tuple[str, uuid.UUID]] = {}
    for m in await session.scalars(
        select(HireMessage)
        .where(HireMessage.application_id.in_(app_ids), HireMessage.status.in_(OPEN_MESSAGE))
        .order_by(HireMessage.created_at.asc())
    ):
        open_messages[m.application_id] = (m.status, m.id)
    cards: list[Card] = []
    for app, candidate, entry in rows:
        record = AppRecord(app, candidate, job, entry, stages)
        waiting = days_between(record.waiting_since(), now)
        active = not record.stage.terminal
        opened = open_messages.get(app.id)
        cards.append(
            Card(
                application_id=app.id,
                reference=app.reference,
                label=record.label(app.id in blind_ids),
                identity_hidden=record.identity_hidden(app.id in blind_ids),
                stage_key=record.stage_key if stages.get(record.stage_key) else FIRST,
                stage_entered_at=entry.stage_entered_at
                if entry
                else (app.submitted_at or app.created_at),
                submitted_at=app.submitted_at,
                waiting_days=waiting,
                sla_overdue=active and waiting >= sla_days,
                tags=list(entry.tags or []) if entry else [],
                needs_human_review=app.needs_human_review,
                review_reasons=list(app.review_reasons or []),
                duplicate_hints=len(app.duplicate_signals or []),
                referred=app.id in referred,
                open_message=opened[0] if opened else None,
                open_message_id=opened[1] if opened else None,
            )
        )
    return Board(job, stages, cards, blind, sla_days)


# ------------------------------------------------------------------ profile


@dataclass(frozen=True, slots=True)
class Event:
    at: datetime
    kind: str
    title: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MessageView:
    id: uuid.UUID
    template_key: str | None
    label: str
    status: str
    sensitive: bool
    purpose: str
    subject: str | None
    text: str | None
    approval_id: uuid.UUID | None
    ai_fields: list[str]
    reason_source: str | None
    delivery: str | None
    created_at: datetime
    sent_at: datetime | None


@dataclass(frozen=True, slots=True)
class Profile:
    record: AppRecord
    blind_mode: bool
    identity_hidden: bool
    label: str
    contact: dict[str, Any] | None
    files: list[dict[str, str]]
    tags: list[str]
    updates_email: bool
    talent_pool: bool
    referred_by: str | None
    screening: ScreeningSummary | None
    messages: list[MessageView]
    timeline: list[Event]


def message_view(message: HireMessage, delivery: str | None) -> MessageView:
    rendered = read_sealed(message, "rendered")
    rendered = rendered if isinstance(rendered, dict) else {}
    entry = library.entry(message.template_key or "")
    return MessageView(
        id=message.id,
        template_key=message.template_key,
        label=entry.label if entry else (message.template_key or "Reply"),
        status=message.status,
        sensitive=message.sensitive,
        purpose=message.purpose,
        subject=rendered.get("subject"),
        text=rendered.get("text"),
        approval_id=message.approval_id,
        ai_fields=list(message.ai_fields or []),
        reason_source=message.reason_source,
        delivery=delivery,
        created_at=message.created_at,
        sent_at=message.sent_at,
    )


def mask(value: str | None, name: str, label: str) -> str | None:
    return value.replace(name, label) if value and name else value


def mask_message(
    view: MessageView, name: str, label: str, redact: Redactor | None = None
) -> MessageView:
    """The message as a reviewer may see it while blind: the name reads as the label,
    and with ``redact`` every other mention of the person is redacted too."""

    def run(value: str | None) -> str | None:
        masked = mask(value, name, label)
        return redact(masked) if redact is not None and masked else masked

    return replace(view, subject=run(view.subject), text=run(view.text))


async def profile(session: AsyncSession, application_id: uuid.UUID, *, blind: bool) -> Profile:
    record = await load_record(session, application_id)
    app = record.app
    app_blind = await application_blind(session, app.id)
    hidden = record.identity_hidden(app_blind)
    moves = list(
        await session.scalars(
            select(HireStageMove)
            .where(HireStageMove.application_id == app.id)
            .order_by(HireStageMove.created_at)
        )
    )
    notes = list(
        await session.scalars(
            select(HireNote).where(HireNote.application_id == app.id).order_by(HireNote.created_at)
        )
    )
    messages = list(
        await session.scalars(
            select(HireMessage)
            .where(HireMessage.application_id == app.id)
            .order_by(HireMessage.created_at)
        )
    )
    note_ids = {str(n.author_id) for n in notes} | {m for n in notes for m in n.mentions}
    people = await _users(session, {m.actor_id for m in moves} | note_ids)
    deliveries: dict[uuid.UUID, str] = {}
    notification_ids = [m.notification_id for m in messages if m.notification_id]
    if notification_ids:
        for row in await session.execute(
            select(Notification.id, Notification.status).where(
                Notification.id.in_(notification_ids)
            )
        ):
            deliveries[row.id] = row.status
    referral = await session.scalar(
        select(HireReferral).where(HireReferral.application_id == app.id)
    )
    referred_by = None
    if referral is not None:
        referred_by = (await _users(session, {str(referral.referrer_id)})).get(
            str(referral.referrer_id), "an employee"
        )
    summary = await screening_summary(session, app.id)
    prefs = await prefs_for(session, record.candidate.id)
    stages = record.stages
    # Free text people wrote may name the candidate: redacted while blind.
    redact: Redactor | None = (
        await identity_redactor(session, app.id, record.identity_contacts()) if hidden else None
    )

    def shown(text: str | None) -> str | None:
        return redact(text) if redact is not None and text else text

    def stage_label(key: str) -> str:
        stage = stages.get(key)
        return stage.label if stage else key

    events: list[Event] = []
    submitted = app.submitted_at or app.created_at
    events.append(
        Event(
            submitted, "application", "Applied", {"reference": app.reference, "source": app.source}
        )
    )
    if app.acknowledged_at:
        events.append(Event(app.acknowledged_at, "message", "Acknowledgement sent", {}))
    if app.review_reasons:
        events.append(
            Event(submitted, "flag", "Needs a person", {"reasons": list(app.review_reasons)})
        )
    if app.duplicate_signals:
        events.append(
            Event(submitted, "flag", "Possible duplicate", {"hints": len(app.duplicate_signals)})
        )
    if referral is not None:
        events.append(Event(referral.created_at, "referral", "Referred", {"by": referred_by}))
    for mv in moves:
        reason = read_sealed(mv, "reason")
        events.append(
            Event(
                mv.created_at,
                "stage",
                ("Undo: " if mv.undo_of else "")
                + f"{stage_label(mv.from_stage)} to {stage_label(mv.to_stage)}",
                {
                    "move_id": str(mv.id),
                    "from": mv.from_stage,
                    "to": mv.to_stage,
                    "by": people.get(mv.actor_id, "system"),
                    "reason": shown(reason if isinstance(reason, str) else None),
                    "undo": mv.undo_of is not None,
                },
            )
        )
    for note in notes:
        events.append(
            Event(
                note.created_at,
                "note",
                "Note",
                {
                    "note_id": str(note.id),
                    "by": people.get(str(note.author_id), "a colleague"),
                    "text": shown(str(read_sealed(note, "body") or "")) or "",
                    "mentions": [people.get(m, "a colleague") for m in note.mentions],
                },
            )
        )
    views = [
        message_view(m, deliveries.get(m.notification_id) if m.notification_id else None)
        for m in messages
    ]
    if hidden:
        # The candidate's name never reaches the reviewer, not even in a message
        # preview or its timeline entry. The email itself is sent with the name.
        views = [mask_message(v, record.full_name, record.label(app_blind), redact) for v in views]
    for view in views:
        when = view.sent_at or view.created_at
        events.append(
            Event(
                when,
                "message",
                f"{view.label}: {view.status.replace('_', ' ')}",
                {
                    "message_id": str(view.id),
                    "status": view.status,
                    "subject": view.subject,
                    "sensitive": view.sensitive,
                    "delivery": view.delivery,
                },
            )
        )
    if summary is not None:
        events.append(
            Event(
                datetime.fromisoformat(summary.scored_at) if summary.scored_at else submitted,
                "screening",
                "Screening result",
                {"status": summary.status, "overall": summary.overall},
            )
        )
    events.sort(key=lambda e: e.at, reverse=True)

    contact: dict[str, Any] | None = None
    files: list[dict[str, str]] = []
    if not hidden:
        contact = {
            "full_name": record.full_name,
            "email": record.email,
            "phone": record.contact.get("phone"),
            "location": record.contact.get("location"),
            "profile_url": record.contact.get("profile_url"),
        }
        if app.cv_document_id:
            files.append({"field": "cv", "document_id": str(app.cv_document_id)})
        files += [{"field": k, "document_id": str(v)} for k, v in sorted((app.files or {}).items())]
    return Profile(
        record=record,
        blind_mode=blind,
        identity_hidden=hidden,
        label=record.label(app_blind),
        contact=contact,
        files=files,
        tags=list(record.entry.tags or []) if record.entry else [],
        updates_email=prefs.updates_email,
        talent_pool=prefs.talent_pool,
        referred_by=referred_by,
        screening=summary,
        messages=views,
        timeline=events,
    )
