# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Reference requests: ask, send, save progress, submit, cancel (brief 3.8.2, 5.1.6).

* The hiring team (``hire.reference.write``) names a referee for an application: name,
  email address, how they know the candidate, the email language. Stored encrypted.
* ``send``: a new single use link (``links.issue_link``) by email
  (``hire.referee.request``); sending again replaces the link and keeps any progress.
* The referee opens the link (a short session), saves progress any number of times
  (``save``), and submits once (``submit``, every required answer). A submitted or
  cancelled reference never changes, and its sessions end.
* Every step is audited with ids, never the referee's name, address or answers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.types import seal
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.notifications.service import Recipient, notify
from pairly_hire.links import LinkConfig
from pairly_hire.pipeline.records import AppRecord, load_record, organisation_name
from pairly_hire.references import form
from pairly_hire.references.links import ENTITY, WORKFLOW, RefereeIdentity, issue_link
from pairly_hire.references.models import (
    OPEN_STATUSES,
    RELATIONSHIPS,
    HireReferenceRequest,
    HireReferenceSession,
    ReferenceStatus,
)
from pairly_hire.references.templates import REFEREE_REQUEST
from pairly_hire.sealing import read_sealed

MAX_PER_APPLICATION: Final = 5


def _seal(row: HireReferenceRequest, column: str, value: Any) -> Any:
    return seal(HireReferenceRequest, column, value, org_id=row.org_id, row_id=row.id)


async def audit_reference(
    session: AsyncSession,
    row: HireReferenceRequest,
    action: str,
    *,
    actor_type: ActorType,
    actor_id: str,
    extra: dict[str, Any] | None = None,
) -> None:
    await append_audit(
        session,
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type=ENTITY,
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={"application_id": str(row.application_id), "status": row.status} | (extra or {}),
    )


def answers_of(row: HireReferenceRequest) -> dict[str, Any]:
    value = read_sealed(row, "answers")
    return dict(value) if isinstance(value, dict) else {}


def referee_name(row: HireReferenceRequest) -> str:
    return str(read_sealed(row, "referee_name") or "")


def referee_email(row: HireReferenceRequest) -> str:
    return str(read_sealed(row, "referee_email") or "")


async def get_request(
    session: AsyncSession, request_id: uuid.UUID, *, lock: bool = False
) -> HireReferenceRequest:
    query = select(HireReferenceRequest).where(HireReferenceRequest.id == request_id)
    if lock:
        query = query.with_for_update()
    row = await session.scalar(query)
    if row is None:
        raise NotFoundError("reference not found")
    return row


async def for_application(
    session: AsyncSession, application_id: uuid.UUID
) -> list[HireReferenceRequest]:
    return list(
        await session.scalars(
            select(HireReferenceRequest)
            .where(HireReferenceRequest.application_id == application_id)
            .order_by(HireReferenceRequest.created_at, HireReferenceRequest.id)
        )
    )


async def create_request(
    session: AsyncSession,
    actor_id: uuid.UUID,
    application_id: uuid.UUID,
    *,
    name: str,
    email: str,
    relationship: str,
    locale: str,
) -> HireReferenceRequest:
    record = await load_record(session, application_id)
    if record.candidate.erased_at is not None:
        raise ConflictError("this candidate's data was erased")
    if relationship not in RELATIONSHIPS:
        raise BadRequestError("choose how the referee knows the candidate")
    clean_name = " ".join(name.split())
    clean_email = email.strip()
    if not 2 <= len(clean_name) <= 120:
        raise BadRequestError("give the referee's name")
    if "@" not in clean_email or len(clean_email) > 254 or any(c.isspace() for c in clean_email):
        raise BadRequestError("give the referee's email address")
    live = [
        r
        for r in await for_application(session, application_id)
        if r.status != ReferenceStatus.CANCELLED.value
    ]
    if len(live) >= MAX_PER_APPLICATION:
        raise ConflictError(f"at most {MAX_PER_APPLICATION} references per application")
    if any(referee_email(r).casefold() == clean_email.casefold() for r in live):
        raise ConflictError("this referee was already asked for this application")
    now = datetime.now(UTC)
    row = HireReferenceRequest(
        id=uuid.uuid4(),
        org_id=record.app.org_id,
        application_id=record.app.id,
        candidate_id=record.candidate.id,
        job_id=record.job.id,
        relationship=relationship,
        locale="ar" if locale == "ar" else "en",
        status=ReferenceStatus.REQUESTED.value,
        requested_by=actor_id,
        created_at=now,
        updated_at=now,
    )
    row.referee_name = _seal(row, "referee_name", clean_name)
    row.referee_email = _seal(row, "referee_email", clean_email)
    session.add(row)
    await session.flush()
    await audit_reference(
        session,
        row,
        "hire.reference.requested",
        actor_type=ActorType.USER,
        actor_id=str(actor_id),
        extra={"relationship": relationship, "locale": row.locale},
    )
    return row


def form_url(config: LinkConfig, locale: str, token: str) -> str:
    """The referee's form. The token rides in the fragment (never in a server log)."""
    if not config.web_url:
        raise ConflictError("links are not configured")
    return f"{config.web_url}/{locale}/reference#{token}"


async def send(
    session: AsyncSession,
    row: HireReferenceRequest,
    *,
    actor_id: uuid.UUID,
    links: LinkConfig,
    now: datetime | None = None,
) -> HireReferenceRequest:
    """Email the referee a new link (any earlier link stops working)."""
    if row.status not in OPEN_STATUSES:
        raise ConflictError("this reference is already submitted or cancelled")
    if row.erased_at is not None:
        raise ConflictError("this candidate's data was erased")
    now = now or datetime.now(UTC)
    record = await load_record(session, row.application_id)
    token, expires = await issue_link(session, row, now=now)
    link_id = token.split(".")[2]
    title = record.job.title_ar if row.locale == "ar" and record.job.title_ar else record.job.title
    await notify(
        session,
        recipient=Recipient.external(referee_email(row), locale=row.locale),
        template_key=REFEREE_REQUEST,
        variables={
            "referee_name": referee_name(row),
            "candidate_name": record.full_name,
            "job_title": title,
            "company": await organisation_name(session, row.org_id),
            "form_url": form_url(links, row.locale, token),
            "expires_at": expires,
        },
        dedupe_key=f"hire.ref:{row.id}:{link_id}",
        workflow=WORKFLOW,
        actor_type=ActorType.USER,
        actor_id=str(actor_id),
    )
    row.sent_at = now
    row.updated_at = now
    await session.flush()
    await audit_reference(
        session,
        row,
        "hire.reference.sent",
        actor_type=ActorType.USER,
        actor_id=str(actor_id),
        extra={"link_id": str(uuid.UUID(hex=link_id)), "expires_at": expires.isoformat()},
    )
    return row


async def _end_sessions(session: AsyncSession, row: HireReferenceRequest, now: datetime) -> None:
    await session.execute(
        update(HireReferenceSession)
        .where(HireReferenceSession.request_id == row.id, HireReferenceSession.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )


async def cancel(
    session: AsyncSession, row: HireReferenceRequest, *, actor_id: uuid.UUID
) -> HireReferenceRequest:
    if row.status not in OPEN_STATUSES:
        raise ConflictError("only an open reference request can be cancelled")
    now = datetime.now(UTC)
    row.status = ReferenceStatus.CANCELLED.value
    row.updated_at = now
    await _end_sessions(session, row, now)
    await session.flush()
    await audit_reference(
        session, row, "hire.reference.cancelled", actor_type=ActorType.USER, actor_id=str(actor_id)
    )
    return row


# ------------------------------------------------------------------ the referee


async def _own(session: AsyncSession, identity: RefereeIdentity) -> HireReferenceRequest:
    row = await get_request(session, identity.request_id, lock=True)
    if row.status not in OPEN_STATUSES or row.erased_at is not None:
        raise ConflictError("this reference was already submitted")
    return row


async def save(
    session: AsyncSession, identity: RefereeIdentity, raw: dict[str, Any]
) -> HireReferenceRequest:
    """Save progress (a partial draft is fine)."""
    row = await _own(session, identity)
    answers = form.clean_answers(raw)
    row.answers = _seal(row, "answers", answers)
    row.status = ReferenceStatus.IN_PROGRESS.value
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await audit_reference(
        session,
        row,
        "hire.reference.saved",
        actor_type=ActorType.CANDIDATE,
        actor_id=identity.actor_id,
        extra={"answered": len(answers)},
    )
    return row


async def submit(
    session: AsyncSession, identity: RefereeIdentity, raw: dict[str, Any]
) -> HireReferenceRequest:
    row = await _own(session, identity)
    answers = form.check_answers(raw)
    now = datetime.now(UTC)
    row.answers = _seal(row, "answers", answers)
    row.status = ReferenceStatus.SUBMITTED.value
    row.submitted_at = now
    row.updated_at = now
    await _end_sessions(session, row, now)
    await session.flush()
    await audit_reference(
        session,
        row,
        "hire.reference.submitted",
        actor_type=ActorType.CANDIDATE,
        actor_id=identity.actor_id,
        extra={"form": form.FORM_VERSION},
    )
    return row


@dataclass(frozen=True, slots=True)
class RefereePage:
    company: str
    candidate_name: str
    job_title: str
    referee_name: str
    status: str
    answers: dict[str, Any]
    questions: list[dict[str, Any]]


async def referee_page(
    session: AsyncSession, identity: RefereeIdentity, locale: str
) -> RefereePage:
    """What the referee sees: who asks, about whom, for which role, and the form with
    their saved answers. Nothing else about the candidate."""
    row = await get_request(session, identity.request_id)
    record: AppRecord = await load_record(session, row.application_id)
    ar = locale == "ar"
    return RefereePage(
        company=await organisation_name(session, row.org_id),
        candidate_name=record.full_name,
        job_title=record.job.title_ar if ar and record.job.title_ar else record.job.title,
        referee_name=referee_name(row),
        status=row.status,
        answers=answers_of(row),
        questions=form.questions_out(locale),
    )


async def seed_submitted(
    session: AsyncSession,
    actor_id: uuid.UUID,
    application_id: uuid.UUID,
    *,
    name: str,
    email: str,
    relationship: str,
    answers: dict[str, Any],
) -> HireReferenceRequest:
    """A reference already answered (the demo seed only: synthetic referees). Audited
    like a request and a submission."""
    row = await create_request(
        session,
        actor_id,
        application_id,
        name=name,
        email=email,
        relationship=relationship,
        locale="en",
    )
    now = datetime.now(UTC)
    row.answers = _seal(row, "answers", form.check_answers(answers))
    row.status = ReferenceStatus.SUBMITTED.value
    row.submitted_at = now
    row.updated_at = now
    await session.flush()
    await audit_reference(
        session,
        row,
        "hire.reference.submitted",
        actor_type=ActorType.SYSTEM,
        actor_id="pairly.devseed.demo",
        extra={"form": form.FORM_VERSION, "seed": True},
    )
    return row
