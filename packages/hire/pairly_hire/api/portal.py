# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Candidate portal routes (WP2.9), under ``/v1/hire/portal``.

Candidates are not staff: these routes never read ``Authorization`` (a staff JWT or API
key does nothing here), and the portal session never works on a staff route. The web app
keeps the portal session in an HTTP-only cookie scoped to the portal and sends it in the
``X-Pairly-Portal-Session`` header (``pairly_hire.portal.auth``).

Without a session (``Cache-Control: no-store``, rate limited per IP):

* ``POST /portal/login-link`` ``{"org", "email", "locale"}`` (``org``: the careers
  slug or the organisation id an expired link names): 202 whatever the
  address, so nobody learns who applied. When the organisation has a candidate with that
  address, a magic link goes to it (30 minutes, single use). 10 per hour per IP, 5 per
  hour per address.
* ``POST /portal/session`` ``{"token"}``: uses a magic link once; returns the portal
  session token (8 hours). 30 per hour per IP. Any link that is not valid: 404.

With a session (``X-Pairly-Portal-Session``; 401 without a valid one; 120 requests a
minute per candidate; writes have their own limits):

* ``GET  /portal/me``                          organisation, the candidate's name and email
* ``POST /portal/sign-out``                    ends the session
* ``GET  /portal/applications``                every application with its timeline
* ``GET  /portal/applications/{id}``           one, with messages and requested files
* ``POST /portal/applications/{id}/withdraw``  ``{"confirm": true}``; audited, an email
* ``POST /portal/applications/{id}/messages``  ``{"text"}``: a reply to the hiring team
* ``POST /portal/document-requests/{id}/file`` multipart ``file``: a requested file
* ``GET  /portal/jobs``                        other open jobs (apply on the careers page)
* ``GET  /portal/privacy``                     decisions, review requests, data requests
* ``GET  /portal/privacy/export``              the candidate's own data (JSON download)
* ``POST /portal/privacy/deletion-request``    ``{"note"?}``: for the admins, never automatic
* ``GET  /portal/privacy/decisions/{id}``      the candidate-facing decision receipt
* ``POST /portal/privacy/appeals``             ``{"decision_id", "reason"}``: human review
"""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select

from pairly_api.problems import problem_response
from pairly_api.uploads import (
    MULTIPART_OVERHEAD,
    UnsupportedBodyError,
    enqueue_scan,
    get_services,
    receive_upload_with_fields,
    rejected_response,
)
from pairly_core.crypto.blind_index import blind_index, normalize_email
from pairly_core.crypto.types import unseal
from pairly_core.db import set_tenant
from pairly_core.errors import BadRequestError
from pairly_core.identity import RequestSession
from pairly_core.ingestion.models import DocumentSource
from pairly_core.ingestion.service import AuditBy, RejectReason, TooLargeError, ingest_spooled
from pairly_core.models import Organisation
from pairly_core.notifications.service import Recipient, notify
from pairly_hire.antispam import SlidingWindowLimiter, client_ip
from pairly_hire.api.common import web_url
from pairly_hire.api.scheduling import env as scheduling_env
from pairly_hire.careers import CareersOrg, careers_org
from pairly_hire.jobs import org_settings
from pairly_hire.models import HireCandidate
from pairly_hire.offers import service as offer_service
from pairly_hire.onboarding import portal as checklist
from pairly_hire.portal import auth, service
from pairly_hire.portal.models import HireDocumentRequest
from pairly_hire.portal.templates import LOGIN_LINK

log = structlog.get_logger("pairly_hire.api.portal")

router = APIRouter(prefix="/portal", tags=["hire-portal"])

PORTAL_HEADER: Final = "X-Pairly-Portal-Session"
WORKFLOW: Final = "hire"
NO_STORE: Final = {"Cache-Control": "no-store"}
# (requests, seconds): per IP before sign-in, per candidate after.
LIMITS: Final = {
    "link_ip": (10, 3600.0),
    "link_email": (5, 3600.0),
    "session_ip": (30, 3600.0),
    "read": (120, 60.0),
    "reply": (30, 3600.0),
    "upload": (20, 3600.0),
    "privacy": (10, 86400.0),
}


def _limiter(request: Request, kind: str) -> SlidingWindowLimiter:
    store: dict[str, SlidingWindowLimiter] | None = getattr(
        request.app.state, "hire_portal_limiters", None
    )
    if store is None:
        store = {k: SlidingWindowLimiter(n, w) for k, (n, w) in LIMITS.items()}
        request.app.state.hire_portal_limiters = store
    return store[kind]


def _hit(request: Request, kind: str, key: str) -> None:
    _limiter(request, kind).hit(key)


async def portal_identity(request: Request, session: RequestSession) -> auth.PortalIdentity:
    """The candidate behind the ``X-Pairly-Portal-Session`` header (401 otherwise); the
    database session is bound to their organisation and to them (both RLS layers)."""
    identity = await auth.authenticate(session, request.headers.get(PORTAL_HEADER))
    _hit(request, "read", str(identity.candidate_id))
    return identity


Portal = Annotated[auth.PortalIdentity, Depends(portal_identity)]


def _no_store(body: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=NO_STORE)


# ------------------------------------------------------------------ schemas


class HirePortalLinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The careers page slug, or the organisation id (32 hex characters, as an expired
    # magic link names it).
    org: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")]
    email: Annotated[str, StringConstraints(max_length=320)]
    locale: Literal["en", "ar"] = "en"


class HirePortalLinkOut(BaseModel):
    sent: bool


class HirePortalTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, StringConstraints(min_length=20, max_length=200)]


class HirePortalSessionOut(BaseModel):
    session_token: str
    expires_at: datetime
    locale: str


class HirePortalMeOut(BaseModel):
    organisation: str
    org_slug: str
    full_name: str
    email: str
    locale: str


class HirePortalStepOut(BaseModel):
    step: str
    at: datetime


class HirePortalInterviewOut(BaseModel):
    id: uuid.UUID
    status: str
    starts_at: datetime | None
    ends_at: datetime | None
    duration_min: int
    timezone: str
    format: str
    title: str
    join_url: str | None
    location: str | None
    choose_url: str | None
    reschedule_url: str | None
    cancel_url: str | None


class HirePortalApplicationOut(BaseModel):
    id: uuid.UUID
    reference: str
    job_title: str
    job_title_ar: str
    submitted_at: datetime
    status: str
    status_since: datetime
    history: list[HirePortalStepOut]
    final: bool
    can_withdraw: bool
    open_document_requests: int
    interviews: list[HirePortalInterviewOut]


class HirePortalMessageOut(BaseModel):
    id: uuid.UUID
    direction: str
    subject: str | None
    text: str
    at: datetime


class HirePortalDocumentRequestOut(BaseModel):
    id: uuid.UUID
    label: str
    label_ar: str
    status: str
    requested_at: datetime
    received_at: datetime | None


class HirePortalApplicationDetailOut(HirePortalApplicationOut):
    messages: list[HirePortalMessageOut]
    document_requests: list[HirePortalDocumentRequestOut]
    # An offer the candidate can read (its email was sent), see ``api.offers``.
    has_offer: bool = False


class HirePortalWithdrawIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]


class HirePortalReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]


class HirePortalJobOut(BaseModel):
    slug: str
    title: str
    title_ar: str
    location: str
    work_model: str
    employment_type: str
    applied: bool


class HirePortalDecisionOut(BaseModel):
    decision_id: str
    kind: str
    decided_at: datetime
    application_id: uuid.UUID
    reference: str
    job_title: str
    can_appeal: bool
    appealed: bool


class HirePortalAppealOut(BaseModel):
    id: uuid.UUID
    decision_id: str
    status: str
    outcome: str | None
    outcome_text: str | None
    created_at: datetime
    sla_due_at: datetime


class HirePortalDataRequestOut(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    created_at: datetime
    handled_at: datetime | None


class HirePortalPrivacyOut(BaseModel):
    decisions: list[HirePortalDecisionOut]
    appeals: list[HirePortalAppealOut]
    data_requests: list[HirePortalDataRequestOut]


class HirePortalDeletionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None


class HirePortalDeletionOut(BaseModel):
    request: HirePortalDataRequestOut
    created: bool


class HirePortalAppealIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: Annotated[str, StringConstraints(pattern=r"^dec_[0-9A-HJKMNP-TV-Z]{26}$")]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=4000)]


class HirePortalFileOut(BaseModel):
    request: HirePortalDocumentRequestOut


# ------------------------------------------------------------------ helpers


def _application_out(item: service.PortalApplication) -> dict[str, Any]:
    return {
        "id": item.id,
        "reference": item.reference,
        "job_title": item.job_title,
        "job_title_ar": item.job_title_ar,
        "submitted_at": item.submitted_at,
        "status": item.timeline.current.value,
        "status_since": item.timeline.since,
        "history": [HirePortalStepOut(step=e.step.value, at=e.at) for e in item.timeline.history],
        "final": item.timeline.final,
        "can_withdraw": item.can_withdraw,
        "open_document_requests": item.open_document_requests,
        "interviews": [
            HirePortalInterviewOut(
                id=i.id,
                status=i.status,
                starts_at=i.starts_at,
                ends_at=i.ends_at,
                duration_min=i.duration_min,
                timezone=i.timezone,
                format=i.format,
                title=i.title,
                join_url=i.join_url,
                location=i.location,
                choose_url=i.choose_url,
                reschedule_url=i.reschedule_url,
                cancel_url=i.cancel_url,
            )
            for i in item.interviews
        ],
    }


def _message_out(m: service.PortalMessage) -> HirePortalMessageOut:
    return HirePortalMessageOut(
        id=m.id, direction=m.direction, subject=m.subject, text=m.text, at=m.at
    )


def _request_out(r: HireDocumentRequest) -> HirePortalDocumentRequestOut:
    return HirePortalDocumentRequestOut(
        id=r.id,
        label=r.label,
        label_ar=r.label_ar,
        status=r.status,
        requested_at=r.created_at,
        received_at=r.fulfilled_at,
    )


async def _detail(
    request: Request,
    session: RequestSession,
    identity: auth.PortalIdentity,
    application_id: uuid.UUID,
) -> HirePortalApplicationDetailOut:
    item = await service.application(session, identity, application_id, scheduling_env(request))
    return HirePortalApplicationDetailOut(
        **_application_out(item),
        messages=[_message_out(m) for m in await service.messages(session, identity, item.id)],
        document_requests=[
            _request_out(r) for r in await service.document_requests(session, identity, item.id)
        ],
        has_offer=await offer_service.candidate_has_offer(session, identity.candidate_id, item.id),
    )


# ------------------------------------------------------------------ sign-in


async def _portal_org(session: RequestSession, org: str) -> CareersOrg | None:
    """The organisation by careers slug or by id, bound to ``session``; ``None`` for an
    unknown, inactive or hidden one (the caller answers the same either way)."""
    org_id = auth.parse_hex_uuid(org)
    if org_id is None:
        found = await careers_org(session, org)
        await session.commit()
        if found is not None:
            set_tenant(session, found.id, None)
        return found
    set_tenant(session, org_id, None)
    row = await session.scalar(select(Organisation).where(Organisation.id == org_id))
    if row is None or row.status != "active":
        return None
    settings = await org_settings(session)
    if not settings.careers_enabled:
        return None
    return CareersOrg(id=row.id, name=row.name, slug=row.slug)


@dataclass(frozen=True, slots=True)
class LinkJob:
    """What the login-link sender needs, decided before the answer (the same for every
    address: the organisation is public, the address is only hashed)."""

    org_id: uuid.UUID
    org_name: str
    email_bidx: str | None
    locale: str
    web_url: str


async def send_login_link(sessionmaker: Any, job: LinkJob) -> None:
    """After the answer: find the candidate by the address's blind index and, only if
    one exists, issue a single-use link and queue the email. For an unknown address this
    is a no-op. Runs in its own transaction; never raises into the request."""
    try:
        if job.email_bidx is None:
            return
        session = sessionmaker()
        set_tenant(session, job.org_id, None)
        async with session, session.begin():
            candidate = await session.scalar(
                select(HireCandidate).where(HireCandidate.email_bidx == job.email_bidx)
            )
            if candidate is None:
                return
            token, _ = await auth.issue_login_token(
                session,
                org_id=job.org_id,
                candidate_id=candidate.id,
                locale=job.locale,
                purpose="request",
                ttl=auth.REQUEST_TTL,
            )
            await notify(
                session,
                recipient=Recipient.external(str(unseal(candidate, "email")), locale=job.locale),
                template_key=LOGIN_LINK,
                variables={
                    "org_name": job.org_name,
                    "portal_url": f"{job.web_url}/{job.locale}/portal/login#{token}",
                    "expires_minutes": int(auth.REQUEST_TTL.total_seconds() // 60),
                },
                dedupe_key=f"hire.portal.link:{uuid.uuid4()}",
                channels=["email"],
                workflow=WORKFLOW,
            )
    except Exception:
        log.exception("portal_login_link_failed")


@router.post("/login-link", status_code=202, response_model=HirePortalLinkOut)
async def login_link(
    body: HirePortalLinkIn,
    request: Request,
    background: BackgroundTasks,
    session: RequestSession,
) -> JSONResponse:
    """The same answer, after the same work, whether or not anyone applied with this
    address: the request only hashes the address and schedules the sender, which looks
    the candidate up after the answer is sent (``send_login_link``)."""
    ip = client_ip(request)
    _hit(request, "link_ip", ip)
    email = normalize_email(body.email)
    if email is None:
        raise BadRequestError("please give a valid email address")
    _hit(request, "link_email", f"{body.org}|{email}")
    org = await _portal_org(session, body.org)
    if org is not None:
        job = LinkJob(
            org_id=org.id,
            org_name=org.name,
            email_bidx=blind_index("email", email, org.id),
            locale=body.locale,
            web_url=web_url(request),
        )
        background.add_task(LINK_SENDER[0], request.app.state.pairly.sessionmaker, job)
    return _no_store({"sent": True}, 202)


# Swappable for tests (the timing check replaces it with a recorder).
LINK_SENDER: list[Any] = [send_login_link]


@router.post("/session", response_model=HirePortalSessionOut)
async def open_session(
    body: HirePortalTokenIn, request: Request, session: RequestSession
) -> JSONResponse:
    _hit(request, "session_ip", client_ip(request))
    opened = await auth.open_session(session, body.token)
    out = HirePortalSessionOut(
        session_token=opened.token, expires_at=opened.expires_at, locale=opened.locale
    )
    return _no_store(out.model_dump(mode="json"))


@router.get("/me", response_model=HirePortalMeOut)
async def me(identity: Portal, session: RequestSession) -> JSONResponse:
    candidate = await service.candidate_row(session, identity)
    org = await session.scalar(select(Organisation).where(Organisation.id == identity.org_id))
    out = HirePortalMeOut(
        organisation=org.name if org else "",
        org_slug=org.slug if org else "",
        full_name=str(unseal(candidate, "full_name") or ""),
        email=str(unseal(candidate, "email") or ""),
        locale=candidate.locale,
    )
    return _no_store(out.model_dump(mode="json"))


@router.post("/sign-out", status_code=204)
async def sign_out(identity: Portal, session: RequestSession) -> JSONResponse:
    await auth.sign_out(session, identity)
    return JSONResponse(None, status_code=204, headers=NO_STORE)


# ------------------------------------------------------------------ applications


@router.get("/applications", response_model=list[HirePortalApplicationOut])
async def list_applications(
    request: Request, identity: Portal, session: RequestSession
) -> JSONResponse:
    items = await service.applications(session, identity, scheduling_env(request))
    out = [HirePortalApplicationOut(**_application_out(i)) for i in items]
    return _no_store([o.model_dump(mode="json") for o in out])


@router.get("/applications/{application_id}", response_model=HirePortalApplicationDetailOut)
async def get_application(
    application_id: uuid.UUID, request: Request, identity: Portal, session: RequestSession
) -> JSONResponse:
    detail = await _detail(request, session, identity, application_id)
    return _no_store(detail.model_dump(mode="json"))


@router.post(
    "/applications/{application_id}/withdraw", response_model=HirePortalApplicationDetailOut
)
async def withdraw(
    application_id: uuid.UUID,
    body: HirePortalWithdrawIn,
    request: Request,
    identity: Portal,
    session: RequestSession,
) -> JSONResponse:
    del body  # ``confirm: true`` is required by the schema
    _hit(request, "privacy", f"withdraw|{identity.candidate_id}")
    await service.withdraw(session, identity, application_id)
    detail = await _detail(request, session, identity, application_id)
    return _no_store(detail.model_dump(mode="json"))


@router.post(
    "/applications/{application_id}/messages",
    status_code=201,
    response_model=HirePortalMessageOut,
)
async def reply(
    application_id: uuid.UUID,
    body: HirePortalReplyIn,
    request: Request,
    identity: Portal,
    session: RequestSession,
) -> JSONResponse:
    _hit(request, "reply", str(identity.candidate_id))
    message = await service.reply(session, identity, application_id, body.text)
    return _no_store(_message_out(message).model_dump(mode="json"), 201)


@router.post(
    "/document-requests/{request_id}/file",
    status_code=201,
    response_model=HirePortalFileOut,
)
async def upload_requested(
    request_id: uuid.UUID,
    request: Request,
    identity: Portal,
    session: RequestSession,
    background: BackgroundTasks,
) -> Any:
    _hit(request, "upload", str(identity.candidate_id))
    services = get_services(request)
    policy = services.settings.policy_for(WORKFLOW)
    row = await service.open_request(session, identity, request_id)
    declared = request.headers.get("content-length")
    if (
        declared is not None
        and declared.isdigit()
        and int(declared) > policy.max_bytes + MULTIPART_OVERHEAD
    ):
        return rejected_response(request, RejectReason.TOO_LARGE, WORKFLOW)
    by = AuditBy.from_actor(identity.actor())
    with tempfile.TemporaryDirectory(prefix="pairly-portal-", dir=services.tmp_dir) as tmp:
        try:
            received = await receive_upload_with_fields(
                request, Path(tmp), policy.max_bytes, services.settings.upload_timeout_seconds
            )
        except UnsupportedBodyError:
            return problem_response(
                request,
                415,
                "unsupported_media_type",
                detail="send multipart/form-data with a 'file' part",
            )
        except TooLargeError:
            return rejected_response(request, RejectReason.TOO_LARGE, WORKFLOW)
        if row.checklist_item_id is not None:
            # A pre-employment document: stored encrypted on its checklist item, never
            # as a shared document (no parsing, no search, HR and admins only).
            data = received.file.path.read_bytes()
            row = await checklist.attach_checklist_file(
                session, services.store, identity, row, data=data, filename=received.filename
            )
            await session.commit()
            out = HirePortalFileOut(request=_request_out(row))
            return _no_store(out.model_dump(mode="json"), 201)
        outcome = await ingest_spooled(
            session,
            services.store,
            spooled=received.file,
            workflow=WORKFLOW,
            source=DocumentSource.UPLOAD,
            filename=received.filename,
            policy=policy,
            by=by,
        )
    if outcome.reason is not None or outcome.document is None:
        return rejected_response(request, outcome.reason or RejectReason.EMPTY, WORKFLOW)
    row = await service.attach_requested(session, identity, row, outcome.document)
    await session.commit()
    if outcome.created:
        # Scanned and parsed by the worker like every other document.
        background.add_task(enqueue_scan, services.queue, identity.org_id, outcome.document.id)
    return _no_store(HirePortalFileOut(request=_request_out(row)).model_dump(mode="json"), 201)


@router.get("/jobs", response_model=list[HirePortalJobOut])
async def jobs(identity: Portal, session: RequestSession) -> JSONResponse:
    out = [
        HirePortalJobOut(
            slug=j.slug,
            title=j.title,
            title_ar=j.title_ar,
            location=j.location,
            work_model=j.work_model,
            employment_type=j.employment_type,
            applied=j.applied,
        )
        for j in await service.open_jobs(session, identity)
    ]
    return _no_store([o.model_dump(mode="json") for o in out])


# ------------------------------------------------------------------ privacy centre


@router.get("/privacy", response_model=HirePortalPrivacyOut)
async def privacy(identity: Portal, session: RequestSession) -> JSONResponse:
    out = HirePortalPrivacyOut(
        decisions=[
            HirePortalDecisionOut.model_validate(d, from_attributes=True)
            for d in await service.decisions(session, identity)
        ],
        appeals=[
            HirePortalAppealOut(
                id=a.id,
                decision_id=a.decision_id,
                status=a.status,
                outcome=a.outcome,
                outcome_text=a.outcome_text,
                created_at=a.created_at,
                sla_due_at=a.sla_due_at,
            )
            for a in await service.appeals(session, identity)
        ],
        data_requests=[
            HirePortalDataRequestOut(
                id=r.id,
                kind=r.kind,
                status=r.status,
                created_at=r.created_at,
                handled_at=r.handled_at,
            )
            for r in await service.data_requests(session, identity)
        ],
    )
    return _no_store(out.model_dump(mode="json"))


@router.get("/privacy/export")
async def export(request: Request, identity: Portal, session: RequestSession) -> JSONResponse:
    _hit(request, "privacy", f"export|{identity.candidate_id}")
    data = await service.export_data(session, identity)
    return JSONResponse(
        data,
        headers={
            **NO_STORE,
            "Content-Disposition": 'attachment; filename="my-application-data.json"',
        },
    )


@router.post("/privacy/deletion-request", status_code=201, response_model=HirePortalDeletionOut)
async def deletion_request(
    body: HirePortalDeletionIn, request: Request, identity: Portal, session: RequestSession
) -> JSONResponse:
    _hit(request, "privacy", f"delete|{identity.candidate_id}")
    row, created = await service.request_deletion(session, identity, body.note)
    out = HirePortalDeletionOut(
        request=HirePortalDataRequestOut(
            id=row.id,
            kind=row.kind,
            status=row.status,
            created_at=row.created_at,
            handled_at=row.handled_at,
        ),
        created=created,
    )
    return _no_store(out.model_dump(mode="json"), 201 if created else 200)


@router.get("/privacy/decisions/{decision_id}")
async def decision_receipt(
    decision_id: Annotated[str, PathParam(pattern=r"^dec_[0-9A-HJKMNP-TV-Z]{26}$")],
    identity: Portal,
    session: RequestSession,
) -> JSONResponse:
    return _no_store(await service.receipt(session, identity, decision_id))


@router.post("/privacy/appeals", status_code=201, response_model=HirePortalAppealOut)
async def appeal(
    body: HirePortalAppealIn, request: Request, identity: Portal, session: RequestSession
) -> JSONResponse:
    _hit(request, "privacy", f"appeal|{identity.candidate_id}")
    view = await service.appeal(session, identity, body.decision_id, body.reason)
    out = HirePortalAppealOut(
        id=view.id,
        decision_id=view.decision_id,
        status=view.status,
        outcome=view.outcome,
        outcome_text=view.outcome_text,
        created_at=view.created_at,
        sla_due_at=view.sla_due_at,
    )
    return _no_store(out.model_dump(mode="json"), 201)
