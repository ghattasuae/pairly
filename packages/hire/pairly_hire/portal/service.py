# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""What a candidate can see and do in the portal (brief 3.9), always as themselves.

Every function takes the request's ``PortalIdentity`` and a database session bound to
it (``portal.auth``: the organisation and the portal subject, so Row Level Security
already limits every query to this candidate's rows). Each query names the candidate
too; an application, message, request or decision of anyone else reads as not found.

* ``applications`` / ``application``: the candidate's submitted applications with their
  honest timeline (``portal.status``), whether they can withdraw, open document
  requests and upcoming interviews (``pairly_hire.interviews``). Never scores, notes,
  tags, flags, reasons, internal stage names or who did what.
* ``messages`` / ``reply``: what the organisation sent them (sent messages only, never
  drafts or messages waiting for approval) and their replies (``messaging.replies``).
* ``withdraw``: confirmed by the caller, audited, a confirmation email.
* ``document_requests`` / ``attach_requested``: files the hiring team asked for.
* ``open_jobs``: other open jobs of the organisation (applying goes through the careers
  apply flow).
* Privacy centre: ``export_data`` (their own data as JSON, including the
  self-identification answers they gave, read through the one narrow reader
  ``pairly_hire.selfid.export``; what reviewers wrote and the screening computed stays
  out, see ``NOT_EXPORTED``), ``request_deletion`` (a data request for admins,
  never an automatic deletion), ``decisions`` / ``receipt`` (decision records about
  their applications and the candidate-facing receipt, ``subject_receipt``),
  ``appeal`` / ``appeals`` (a human review request by decision id, through the
  engine's workflow-scoped appeal creation).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.types import seal, unseal
from pairly_core.decisions.appeals import AppealView, clean_text, create_appeal
from pairly_core.decisions.models import Appeal, DecisionRecord, RequesterType
from pairly_core.decisions.receipts import ReceiptContext, SubjectRef, subject_receipt
from pairly_core.decisions.service import (
    get_record,
    history,
    open_sensitive,
    superseded_by,
    verify_record,
)
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.ingestion.models import Document
from pairly_core.models import Organisation
from pairly_core.notifications.service import Recipient, notify
from pairly_hire.interviews import PortalInterview, upcoming_interviews
from pairly_hire.messaging.models import HireMessage, MessagePurpose, MessageStatus
from pairly_hire.messaging.prefs import prefs_for
from pairly_hire.messaging.replies import record_reply
from pairly_hire.models import (
    ApplicationStatus,
    HireApplication,
    HireCandidate,
    HireJob,
    JobStatus,
)
from pairly_hire.pipeline import moves
from pairly_hire.pipeline.models import HireStageMove
from pairly_hire.pipeline.stages import stages_for_jobs
from pairly_hire.portal.auth import PortalIdentity
from pairly_hire.portal.models import (
    DataRequestKind,
    DataRequestStatus,
    DocumentRequestStatus,
    HireDataRequest,
    HireDocumentRequest,
)
from pairly_hire.portal.status import MoveFact, Timeline, timeline
from pairly_hire.portal.templates import WITHDRAWN
from pairly_hire.sealing import read_sealed
from pairly_hire.selfid.export import subject_answers

if TYPE_CHECKING:
    from pairly_hire.scheduling.emails import SchedulingEnv

WORKFLOW: Final = "hire"
APPLICATION_ENTITY: Final = "hire.application"
EXPORT_FORMAT: Final = "pairly.hire.candidate_export.v1"
SENT: Final = frozenset({MessageStatus.QUEUED.value, MessageStatus.SENT.value})
# Decision kinds a candidate sees and may ask a person to review.
VISIBLE_KINDS: Final = ("extraction", "score", "shortlist_suggestion", "human_override")
SHOWN_KINDS: Final = (*VISIBLE_KINDS, "appeal_outcome")
NOT_EXPORTED: Final = (
    "Your self-identification answers are included as you gave them; the hiring team never "
    "sees them, only bias reports with counts. Reviewers' notes, tags and scores are not in "
    "this file: ask for a human review of a decision to have it explained."
)
MAX_DELETION_NOTE: Final = 1000


def requester_id(identity: PortalIdentity) -> str:
    return f"hire_candidate:{identity.candidate_id}"


async def _audit(
    session: AsyncSession,
    identity: PortalIdentity,
    action: str,
    *,
    entity_type: str,
    entity_id: uuid.UUID | str,
    payload: dict[str, Any] | None = None,
) -> None:
    await append_audit(
        session,
        action=action,
        actor_type=ActorType.CANDIDATE,
        actor_id=identity.actor_id,
        entity_type=entity_type,
        entity_id=entity_id,
        workflow=WORKFLOW,
        payload=payload or {},
    )


async def candidate_row(session: AsyncSession, identity: PortalIdentity) -> HireCandidate:
    row = await session.scalar(
        select(HireCandidate).where(HireCandidate.id == identity.candidate_id)
    )
    if row is None:
        raise NotFoundError("not found")
    return row


async def own_applications(
    session: AsyncSession, identity: PortalIdentity
) -> list[HireApplication]:
    return list(
        await session.scalars(
            select(HireApplication)
            .where(
                HireApplication.candidate_id == identity.candidate_id,
                HireApplication.status != ApplicationStatus.INCOMPLETE.value,
            )
            .order_by(HireApplication.submitted_at.desc(), HireApplication.id)
        )
    )


async def own_application(
    session: AsyncSession,
    identity: PortalIdentity,
    application_id: uuid.UUID,
    *,
    lock: bool = False,
) -> HireApplication:
    query = select(HireApplication).where(
        HireApplication.id == application_id,
        HireApplication.candidate_id == identity.candidate_id,
        HireApplication.status != ApplicationStatus.INCOMPLETE.value,
    )
    if lock:
        query = query.with_for_update()
    app = await session.scalar(query)
    if app is None:
        raise NotFoundError("application not found")
    return app


# ------------------------------------------------------------------ applications


@dataclass(frozen=True, slots=True)
class PortalApplication:
    id: uuid.UUID
    reference: str
    job_id: uuid.UUID
    job_title: str
    job_title_ar: str
    submitted_at: datetime
    timeline: Timeline
    can_withdraw: bool
    open_document_requests: int
    interviews: list[PortalInterview] = field(default_factory=list)


async def _rejections_sent(
    session: AsyncSession, app_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    out: dict[uuid.UUID, datetime] = {}
    for application_id, sent_at in await session.execute(
        select(HireMessage.application_id, HireMessage.sent_at).where(
            HireMessage.application_id.in_(app_ids),
            HireMessage.purpose == MessagePurpose.REJECTION.value,
            HireMessage.status.in_(SENT),
            HireMessage.sent_at.is_not(None),
        )
    ):
        if sent_at is None:
            continue
        if application_id not in out or sent_at < out[application_id]:
            out[application_id] = sent_at
    return out


async def applications(
    session: AsyncSession, identity: PortalIdentity, env: SchedulingEnv | None = None
) -> list[PortalApplication]:
    apps = await own_applications(session, identity)
    if not apps:
        return []
    ids = [a.id for a in apps]
    jobs = {
        j.id: j
        for j in await session.scalars(
            select(HireJob).where(HireJob.id.in_({a.job_id for a in apps}))
        )
    }
    stages = await stages_for_jobs(session, list(jobs))
    facts: dict[uuid.UUID, list[MoveFact]] = defaultdict(list)
    for m in await session.execute(
        select(
            HireStageMove.id,
            HireStageMove.application_id,
            HireStageMove.created_at,
            HireStageMove.to_stage,
            HireStageMove.undo_of,
        ).where(HireStageMove.application_id.in_(ids))
    ):
        facts[m.application_id].append(MoveFact(m.id, m.created_at, m.to_stage, m.undo_of))
    sent = await _rejections_sent(session, ids)
    open_requests: dict[uuid.UUID, int] = defaultdict(int)
    for application_id in await session.scalars(
        select(HireDocumentRequest.application_id).where(
            HireDocumentRequest.application_id.in_(ids),
            HireDocumentRequest.status == DocumentRequestStatus.OPEN.value,
        )
    ):
        open_requests[application_id] += 1
    interviews: dict[uuid.UUID, list[PortalInterview]] = defaultdict(list)
    for interview in await upcoming_interviews(session, identity.candidate_id, ids, env):
        interviews[interview.application_id].append(interview)
    out: list[PortalApplication] = []
    for app in apps:
        job = jobs[app.job_id]
        submitted = app.submitted_at or app.created_at
        line = timeline(
            stages[app.job_id], submitted, facts[app.id], rejection_sent_at=sent.get(app.id)
        )
        out.append(
            PortalApplication(
                id=app.id,
                reference=app.reference,
                job_id=job.id,
                job_title=job.title,
                job_title_ar=job.title_ar,
                submitted_at=submitted,
                timeline=line,
                can_withdraw=not line.final,
                open_document_requests=open_requests[app.id],
                interviews=interviews[app.id],
            )
        )
    return out


async def application(
    session: AsyncSession,
    identity: PortalIdentity,
    application_id: uuid.UUID,
    env: SchedulingEnv | None = None,
) -> PortalApplication:
    await own_application(session, identity, application_id)
    for item in await applications(session, identity, env):
        if item.id == application_id:
            return item
    raise NotFoundError("application not found")


# ------------------------------------------------------------------ messages


@dataclass(frozen=True, slots=True)
class PortalMessage:
    id: uuid.UUID
    application_id: uuid.UUID
    # ``from_company`` or ``from_you``.
    direction: str
    subject: str | None
    text: str
    at: datetime


def _portal_message(message: HireMessage) -> PortalMessage | None:
    inbound = message.direction == "inbound"
    if not inbound and message.status not in SENT:
        return None
    rendered = read_sealed(message, "rendered")
    data: Mapping[str, Any] = rendered if isinstance(rendered, Mapping) else {}
    text = data.get("text")
    if not isinstance(text, str):
        return None
    subject = data.get("subject")
    return PortalMessage(
        id=message.id,
        application_id=message.application_id,
        direction="from_you" if inbound else "from_company",
        subject=subject if isinstance(subject, str) else None,
        text=text,
        at=(message.sent_at or message.created_at) if not inbound else message.created_at,
    )


async def messages(
    session: AsyncSession, identity: PortalIdentity, application_id: uuid.UUID
) -> list[PortalMessage]:
    await own_application(session, identity, application_id)
    rows = await session.scalars(
        select(HireMessage)
        .where(
            HireMessage.application_id == application_id,
            HireMessage.candidate_id == identity.candidate_id,
            or_(
                HireMessage.direction == "inbound",
                and_(HireMessage.direction == "outbound", HireMessage.status.in_(SENT)),
            ),
        )
        .order_by(HireMessage.created_at)
    )
    shown = [_portal_message(m) for m in rows]
    return sorted((m for m in shown if m is not None), key=lambda m: m.at)


async def reply(
    session: AsyncSession, identity: PortalIdentity, application_id: uuid.UUID, text: str
) -> PortalMessage:
    await own_application(session, identity, application_id)
    message = await record_reply(
        session,
        application_id,
        text=text,
        actor_id=identity.actor_id,
        actor_type=ActorType.CANDIDATE,
    )
    shown = _portal_message(message)
    if shown is None:  # pragma: no cover - a reply always has its text
        raise BadRequestError("the reply could not be stored")
    return shown


# ------------------------------------------------------------------ withdraw


async def withdraw(
    session: AsyncSession, identity: PortalIdentity, application_id: uuid.UUID
) -> moves.WithdrawResult:
    """Withdraw (the caller asked the candidate to confirm). Audited; a confirmation
    email in the application's language."""
    app = await own_application(session, identity, application_id)
    result = await moves.withdraw(session, identity.actor(), app.id)
    candidate = await candidate_row(session, identity)
    job = await session.scalar(select(HireJob).where(HireJob.id == app.job_id))
    org_name = await session.scalar(
        select(Organisation.name).where(Organisation.id == identity.org_id)
    )
    contact = unseal(app, "contact")
    name = contact.get("full_name") if isinstance(contact, dict) else None
    locale = app.consent_locale if app.consent_locale in {"en", "ar"} else "en"
    title = job.title_ar if job is not None and locale == "ar" and job.title_ar else None
    await notify(
        session,
        recipient=Recipient.external(str(unseal(candidate, "email")), locale=locale),
        template_key=WITHDRAWN,
        variables={
            "candidate_name": str(name or unseal(candidate, "full_name") or ""),
            "job_title": title or (job.title if job is not None else ""),
            "org_name": str(org_name or ""),
            "reference": app.reference,
        },
        dedupe_key=f"hire.withdrawn:{app.id}",
        channels=["email"],
        workflow=WORKFLOW,
        actor_type=ActorType.CANDIDATE,
        actor_id=identity.actor_id,
    )
    await _audit(
        session,
        identity,
        "hire.application.withdrawn",
        entity_type=APPLICATION_ENTITY,
        entity_id=app.id,
        payload={
            "move_id": str(result.move.move_id),
            "cancelled_drafts": [str(d) for d in result.cancelled_drafts],
        },
    )
    return result


# ------------------------------------------------------------------ document requests


async def document_requests(
    session: AsyncSession, identity: PortalIdentity, application_id: uuid.UUID
) -> list[HireDocumentRequest]:
    await own_application(session, identity, application_id)
    return list(
        await session.scalars(
            select(HireDocumentRequest)
            .where(
                HireDocumentRequest.application_id == application_id,
                HireDocumentRequest.candidate_id == identity.candidate_id,
                HireDocumentRequest.status != DocumentRequestStatus.CANCELLED.value,
            )
            .order_by(HireDocumentRequest.created_at)
        )
    )


async def open_request(
    session: AsyncSession, identity: PortalIdentity, request_id: uuid.UUID
) -> HireDocumentRequest:
    row = await session.scalar(
        select(HireDocumentRequest)
        .where(
            HireDocumentRequest.id == request_id,
            HireDocumentRequest.candidate_id == identity.candidate_id,
        )
        .with_for_update()
    )
    if row is None:
        raise NotFoundError("request not found")
    if row.status != DocumentRequestStatus.OPEN.value:
        raise ConflictError("this file was already sent or is no longer needed")
    await own_application(session, identity, row.application_id)
    return row


async def attach_requested(
    session: AsyncSession,
    identity: PortalIdentity,
    row: HireDocumentRequest,
    document: Document,
) -> HireDocumentRequest:
    app = await own_application(session, identity, row.application_id, lock=True)
    now = datetime.now(UTC)
    row.status = DocumentRequestStatus.RECEIVED.value
    row.document_id = document.id
    row.fulfilled_at = now
    files = dict(app.files or {})
    files[f"request_{row.id.hex[:12]}"] = str(document.id)
    app.files = files
    app.updated_at = now
    await session.flush()
    await _audit(
        session,
        identity,
        "hire.document_request.fulfilled",
        entity_type="hire.document_request",
        entity_id=row.id,
        payload={"application_id": str(app.id), "document_id": str(document.id)},
    )
    return row


# ------------------------------------------------------------------ jobs


@dataclass(frozen=True, slots=True)
class PortalJob:
    slug: str
    title: str
    title_ar: str
    location: str
    work_model: str
    employment_type: str
    applied: bool


async def open_jobs(session: AsyncSession, identity: PortalIdentity) -> list[PortalJob]:
    applied = {a.job_id for a in await own_applications(session, identity)}
    rows = await session.scalars(
        select(HireJob)
        .where(HireJob.status == JobStatus.OPEN.value)
        .order_by(HireJob.published_at.desc().nulls_last(), HireJob.title)
        .limit(200)
    )
    return [
        PortalJob(
            slug=j.slug,
            title=j.title,
            title_ar=j.title_ar,
            location=j.location,
            work_model=j.work_model,
            employment_type=j.employment_type,
            applied=j.id in applied,
        )
        for j in rows
    ]


# ------------------------------------------------------------------ privacy centre


async def _decision_rows(
    session: AsyncSession, apps: Sequence[HireApplication]
) -> list[DecisionRecord]:
    if not apps:
        return []
    documents = [str(a.cv_document_id) for a in apps if a.cv_document_id is not None]
    clauses = [
        and_(
            DecisionRecord.entity_type == APPLICATION_ENTITY,
            DecisionRecord.entity_id.in_([str(a.id) for a in apps]),
        )
    ]
    if documents:
        clauses.append(
            and_(DecisionRecord.entity_type == "document", DecisionRecord.entity_id.in_(documents))
        )
    return list(
        await session.scalars(
            select(DecisionRecord)
            .where(
                DecisionRecord.workflow == WORKFLOW,
                DecisionRecord.kind.in_(SHOWN_KINDS),
                or_(*clauses),
            )
            .order_by(DecisionRecord.decided_at, DecisionRecord.id)
        )
    )


@dataclass(frozen=True, slots=True)
class PortalDecision:
    decision_id: str
    kind: str
    decided_at: datetime
    application_id: uuid.UUID
    reference: str
    job_title: str
    can_appeal: bool
    appealed: bool


async def decisions(session: AsyncSession, identity: PortalIdentity) -> list[PortalDecision]:
    apps = await own_applications(session, identity)
    rows = await _decision_rows(session, apps)
    by_app = {str(a.id): a for a in apps}
    by_doc = {str(a.cv_document_id): a for a in apps if a.cv_document_id is not None}
    titles = {
        j.id: j.title
        for j in await session.scalars(
            select(HireJob).where(HireJob.id.in_({a.job_id for a in apps}))
        )
    }
    appealed = set(
        await session.scalars(
            select(Appeal.decision_id).where(Appeal.requester_id == requester_id(identity))
        )
    )
    out: list[PortalDecision] = []
    for row in rows:
        app = (
            by_app.get(row.entity_id)
            if row.entity_type == APPLICATION_ENTITY
            else by_doc.get(row.entity_id)
        )
        if app is None:
            continue
        out.append(
            PortalDecision(
                decision_id=row.id,
                kind=row.kind,
                decided_at=row.decided_at,
                application_id=app.id,
                reference=app.reference,
                job_title=titles.get(app.job_id, ""),
                can_appeal=row.kind in VISIBLE_KINDS and row.id not in appealed,
                appealed=row.id in appealed,
            )
        )
    return out


async def _own_decision(
    session: AsyncSession, identity: PortalIdentity, decision_id: str
) -> tuple[DecisionRecord, SubjectRef]:
    row = await get_record(session, decision_id)
    if row is None or row.workflow != WORKFLOW or row.kind not in SHOWN_KINDS:
        raise NotFoundError("decision not found")
    apps = await own_applications(session, identity)
    if row.entity_type == APPLICATION_ENTITY and row.entity_id in {str(a.id) for a in apps}:
        return row, SubjectRef(APPLICATION_ENTITY, row.entity_id)
    documents = {str(a.cv_document_id) for a in apps if a.cv_document_id is not None}
    if row.entity_type == "document" and row.entity_id in documents:
        return row, SubjectRef("document", row.entity_id)
    raise NotFoundError("decision not found")


async def receipt(
    session: AsyncSession, identity: PortalIdentity, decision_id: str
) -> dict[str, Any]:
    """The candidate-facing receipt of one of their decisions (``subject_receipt``)."""
    row, subject = await _own_decision(session, identity, decision_id)
    problem = await verify_record(session, row)
    sensitive = None if problem == "decryption_failed" else open_sensitive(row)
    data = subject_receipt(
        row,
        ReceiptContext(
            sensitive=sensitive,
            intact=problem is None,
            history=tuple(await history(session, row)),
            superseded_by=await superseded_by(session, row.id),
        ),
        subject,
    )
    await _audit(
        session,
        identity,
        "hire.portal.receipt_viewed",
        entity_type="decision_record",
        entity_id=row.id,
    )
    return data


async def appeal(
    session: AsyncSession, identity: PortalIdentity, decision_id: str, reason: str
) -> AppealView:
    """Ask a person to review one AI-assisted step (brief 2.3.3). The engine's
    workflow-scoped appeal creation, as the candidate (``hire.appeal.create``)."""
    row, _ = await _own_decision(session, identity, decision_id)
    if row.kind not in VISIBLE_KINDS:
        raise ConflictError("this step was already a review")
    existing = await session.scalar(
        select(Appeal.id).where(
            Appeal.decision_id == row.id, Appeal.requester_id == requester_id(identity)
        )
    )
    if existing is not None:
        raise ConflictError("you already asked for a review of this step")
    return await create_appeal(
        session,
        identity.actor(),
        decision_id=row.id,
        requester_type=RequesterType.CANDIDATE,
        requester_id=requester_id(identity),
        reason=reason,
    )


async def appeals(session: AsyncSession, identity: PortalIdentity) -> list[AppealView]:
    rows = await session.execute(
        select(Appeal, DecisionRecord.workflow)
        .join(DecisionRecord, DecisionRecord.id == Appeal.decision_id)
        .where(Appeal.requester_id == requester_id(identity))
        .order_by(Appeal.created_at.desc())
        .limit(100)
    )
    return [AppealView.of(a, workflow) for a, workflow in rows]


async def request_deletion(
    session: AsyncSession, identity: PortalIdentity, note: str | None
) -> tuple[HireDataRequest, bool]:
    """A deletion request for the organisation's admins. Never deletes anything itself.
    Returns the request and whether it is new (an open one is returned as it is)."""
    open_row = await session.scalar(
        select(HireDataRequest).where(
            HireDataRequest.candidate_id == identity.candidate_id,
            HireDataRequest.kind == DataRequestKind.DELETION.value,
            HireDataRequest.status == DataRequestStatus.OPEN.value,
        )
    )
    if open_row is not None:
        return open_row, False
    now = datetime.now(UTC)
    row = HireDataRequest(
        id=uuid.uuid4(),
        org_id=identity.org_id,
        candidate_id=identity.candidate_id,
        kind=DataRequestKind.DELETION.value,
        status=DataRequestStatus.OPEN.value,
        created_at=now,
    )
    cleaned = clean_text(note, "a note")[:MAX_DELETION_NOTE] if note and note.strip() else None
    if cleaned:
        sealed = seal(HireDataRequest, "note", cleaned, org_id=row.org_id, row_id=row.id)
        setattr(row, "note", sealed)  # noqa: B010 (typed column, sealed value)
    session.add(row)
    await session.flush()
    await _audit(
        session,
        identity,
        "hire.data_request.created",
        entity_type="hire.data_request",
        entity_id=row.id,
        payload={"kind": row.kind, "with_note": cleaned is not None},
    )
    return row, True


async def data_requests(session: AsyncSession, identity: PortalIdentity) -> list[HireDataRequest]:
    return list(
        await session.scalars(
            select(HireDataRequest)
            .where(HireDataRequest.candidate_id == identity.candidate_id)
            .order_by(HireDataRequest.created_at.desc())
        )
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def export_data(session: AsyncSession, identity: PortalIdentity) -> dict[str, Any]:
    """Everything the organisation holds about the candidate that the candidate gave or
    was sent, as JSON. Never another person's data, and never what reviewers wrote or
    the screening computed (scores, notes, tags, flags). Audited."""
    candidate = await candidate_row(session, identity)
    org_name = await session.scalar(
        select(Organisation.name).where(Organisation.id == identity.org_id)
    )
    prefs = await prefs_for(session, identity.candidate_id)
    views = {v.id: v for v in await applications(session, identity)}
    apps = await own_applications(session, identity)
    self_id = await subject_answers(session, [a.id for a in apps], locale=candidate.locale)
    out_apps: list[dict[str, Any]] = []
    for app in apps:
        view = views.get(app.id)
        answers = unseal(app, "answers")
        contact = unseal(app, "contact")
        out_apps.append(
            {
                "reference": app.reference,
                "job_title": view.job_title if view else "",
                "submitted_at": _iso(app.submitted_at),
                "status": view.timeline.current.value if view else None,
                "history": [
                    {"step": e.step.value, "at": _iso(e.at)}
                    for e in (view.timeline.history if view else ())
                ],
                "consent": {
                    "notice_version": app.consent_version,
                    "at": _iso(app.consent_at),
                    "language": app.consent_locale,
                },
                "contact_as_sent": contact if isinstance(contact, dict) else {},
                "answers": answers if isinstance(answers, dict) else {},
                "files": sorted(["cv", *(k for k in (app.files or {}) if k != "cv")]),
                "messages": [
                    {
                        "from": m.direction,
                        "subject": m.subject,
                        "text": m.text,
                        "at": _iso(m.at),
                    }
                    for m in await messages(session, identity, app.id)
                ],
                "document_requests": [
                    {"label": r.label, "status": r.status, "requested_at": _iso(r.created_at)}
                    for r in await document_requests(session, identity, app.id)
                ],
                "self_identification": self_id.get(app.id, []),
            }
        )
    data: dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "generated_at": datetime.now(UTC).isoformat(),
        "organisation": str(org_name or ""),
        "candidate": {
            "full_name": unseal(candidate, "full_name"),
            "email": unseal(candidate, "email"),
            "phone": unseal(candidate, "phone"),
            "location": unseal(candidate, "location"),
            "profile_url": unseal(candidate, "profile_url"),
            "language": candidate.locale,
            "first_seen_at": _iso(candidate.created_at),
        },
        "preferences": {"updates_email": prefs.updates_email, "talent_pool": prefs.talent_pool},
        "applications": out_apps,
        "decisions": [
            {"decision_id": d.decision_id, "kind": d.kind, "decided_at": _iso(d.decided_at)}
            for d in await decisions(session, identity)
        ],
        "reviews_requested": [
            {
                "decision_id": a.decision_id,
                "status": a.status,
                "requested_at": _iso(a.created_at),
                "your_reason": a.reason,
                "outcome": a.outcome,
                "outcome_text": a.outcome_text,
            }
            for a in await appeals(session, identity)
        ],
        "data_requests": [
            {
                "kind": r.kind,
                "status": r.status,
                "requested_at": _iso(r.created_at),
                "handled_at": _iso(r.handled_at),
            }
            for r in await data_requests(session, identity)
        ],
        "not_included": NOT_EXPORTED,
    }
    await _audit(
        session,
        identity,
        "hire.portal.data_exported",
        entity_type="hire.candidate",
        entity_id=identity.candidate_id,
        payload={"applications": len(out_apps)},
    )
    return data
