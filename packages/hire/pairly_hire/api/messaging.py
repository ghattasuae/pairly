# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Messaging routes (WP2.7): templates, drafts, AI drafts, preview, send, submit for
approval, bulk updates, the SLA setting and the candidates' own preferences.

Signed in (``hire.message.create`` for writes, ``hire.candidate.read`` for reads):

* ``GET  /messages/templates``                      the library, per language
* ``POST /pipeline/applications/{id}/messages``     a new draft ``{"template_key", "values"?}``
* ``GET  /messages/{id}``                            a draft with its exact preview
* ``PATCH /messages/{id}``                           ``{"values"}``: the person's values
* ``POST /messages/{id}/ai-draft``                  the AI's personal paragraph (and, for a
                                                     rejection with evidence, a reason)
* ``POST /messages/{id}/send``                      non-sensitive: queue the email
* ``POST /messages/{id}/submit``                    sensitive: to the approval queue
* ``POST /messages/{id}/cancel``                    drop a draft
* ``POST /jobs/{job_id}/messages/bulk/preview``     what a bulk update would do
* ``POST /jobs/{job_id}/messages/bulk/send``        send it (non-sensitive templates only)
* ``GET|PUT /settings/messaging``                   the SLA days (``hire.settings.manage``)

Public, no sign-in, rate limited (the candidate's signed link, ``hp1`` token in the
body, never in a URL):

* ``POST /public/preferences/read``   the candidate's preferences
* ``POST /public/preferences``        change them
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Request
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
from pairly_core.identity import CurrentActor, OrgSession, RequestSession
from pairly_core.llm.errors import ModelUnavailable
from pairly_core.models import Organisation
from pairly_core.notifications.templates import resolve_template
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_hire.antispam import SlidingWindowLimiter, client_ip
from pairly_hire.api.common import model_router
from pairly_hire.api.pipeline import links
from pairly_hire.links import links_from_settings, read_prefs_token
from pairly_hire.messaging import compose, library
from pairly_hire.messaging.drafting import AI_TEMPLATES, DraftRefusedError, ai_draft
from pairly_hire.messaging.graph import GRAPH
from pairly_hire.messaging.models import HireMessage, MessagePurpose
from pairly_hire.messaging.prefs import prefs_for, prefs_for_many, set_prefs
from pairly_hire.messaging.sla import save_sla_days, sla_days
from pairly_hire.models import ApplicationStatus, HireApplication, HireCandidate, HireJob
from pairly_hire.permissions import (
    CANDIDATE_READ,
    JOB_READ,
    MESSAGE_CREATE,
    SETTINGS_MANAGE,
)
from pairly_hire.pipeline.hooks import application_blind, screening_summary
from pairly_hire.pipeline.records import AppRecord, load_record

log = structlog.get_logger("pairly_hire.api.messaging")

router = APIRouter(tags=["hire"])

WORKFLOW: Final = "hire"
MAX_BULK: Final = 200
PREFS_LIMIT: Final = (30, 3600.0)


def _writer(actor: Actor) -> None:
    authorize(actor, MESSAGE_CREATE)
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can write to candidates")


def _author(actor: Actor) -> compose.Author:
    kind, actor_id = actor_ref(actor)
    return compose.Author(kind, actor_id)


# ------------------------------------------------------------------ schemas


class HireTemplateFieldOut(BaseModel):
    name: str
    type: str
    required: bool
    ai: bool


class HireTemplateOut(BaseModel):
    key: str
    label: str
    label_ar: str
    sensitive: bool
    transactional: bool
    rejection: bool
    ai_available: bool
    suggested_for: list[str]
    fields: list[HireTemplateFieldOut]


class HireDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_key: Annotated[str, StringConstraints(max_length=96)]
    values: dict[str, Any] = Field(default_factory=dict, max_length=20)
    locale: Annotated[str, StringConstraints(pattern=r"^(en|ar)$")] | None = None


class HireValuesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] = Field(default_factory=dict, max_length=20)


class HireAiDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_reason: bool = False


class HirePreviewOut(BaseModel):
    ok: bool
    subject: str | None
    text: str | None
    missing: list[str]
    error: str | None
    # Blind mode: the name in the preview is replaced; the email carries it.
    masked: bool


class HireMessageOut(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    template_key: str | None
    label: str
    locale: str
    status: str
    status_reason: str | None
    sensitive: bool
    transactional: bool
    purpose: str
    values: dict[str, Any]
    fields: list[HireTemplateFieldOut]
    ai_fields: list[str]
    ai_available: bool
    reason_source: str | None
    approval_id: uuid.UUID | None
    candidate_opted_out: bool
    preview: HirePreviewOut


class HireAiDraftOut(BaseModel):
    message: HireMessageOut
    model: str
    prompt_version: str
    from_recording: bool


class HireBulkMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_BULK)
    template_key: Annotated[str, StringConstraints(max_length=96)]


class HireBulkMessageSummaryOut(BaseModel):
    template_key: str
    label: str
    total: int
    will_send: int
    opted_out: int
    not_found: int
    sensitive: bool
    missing_fields: list[str]


class HireBulkMessageOut(BaseModel):
    sent: int
    suppressed: int
    message_ids: list[uuid.UUID]


class HireMessagingSettingsOut(BaseModel):
    sla_days: int


class HireMessagingSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sla_days: Annotated[int, Field(ge=1, le=60)]


class HirePrefsTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, StringConstraints(min_length=10, max_length=200)]


class HirePrefsIn(HirePrefsTokenIn):
    updates_email: bool | None = None
    talent_pool: bool | None = None


class HirePrefsOut(BaseModel):
    organisation: str
    updates_email: bool
    talent_pool: bool
    jobs: list[str]


# ------------------------------------------------------------------ helpers


async def _fields(session: AsyncSession, key: str, locale: str) -> list[HireTemplateFieldOut]:
    template = await compose.template_for(session, key, locale)
    specs = compose.specs_of(template)
    return [
        HireTemplateFieldOut(
            name=n,
            type=specs[n].type,
            required=specs[n].required,
            ai=n in library.AI_VARIABLES and key in AI_TEMPLATES,
        )
        for n in compose.editable_names(specs)
    ]


async def _message_out(
    session: AsyncSession, message: HireMessage, record: AppRecord, request: Request
) -> HireMessageOut:
    preview = await compose.preview(session, message, record, links(request))
    blind = await application_blind(session, record.app.id)
    masked = record.identity_hidden(blind)
    subject, text = preview.subject, preview.text
    if masked and record.full_name:
        label = record.label(blind)
        subject = subject.replace(record.full_name, label) if subject else subject
        text = text.replace(record.full_name, label) if text else text
    entry = library.entry(message.template_key or "")
    prefs = await prefs_for(session, record.candidate.id)
    return HireMessageOut(
        id=message.id,
        application_id=message.application_id,
        template_key=message.template_key,
        label=entry.label if entry else (message.template_key or ""),
        locale=message.locale,
        status=message.status,
        status_reason=message.status_reason,
        sensitive=message.sensitive,
        transactional=entry.transactional if entry else True,
        purpose=message.purpose,
        values=compose.person_values(
            compose.specs_of(
                await compose.template_for(session, message.template_key or "", message.locale)
            ),
            compose.stored_values(message),
        )
        if message.template_key
        else {},
        fields=await _fields(session, message.template_key or "", message.locale)
        if message.template_key
        else [],
        ai_fields=list(message.ai_fields or []),
        ai_available=(message.template_key or "") in AI_TEMPLATES,
        reason_source=message.reason_source,
        approval_id=message.approval_id,
        candidate_opted_out=not prefs.updates_email,
        preview=HirePreviewOut(
            ok=preview.ok,
            subject=subject,
            text=text,
            missing=list(preview.missing),
            error=preview.error,
            masked=masked,
        ),
    )


async def _loaded(
    session: AsyncSession, message_id: uuid.UUID, *, lock: bool = False
) -> tuple[HireMessage, AppRecord]:
    message = await compose.get_message(session, message_id, lock=lock)
    record = await load_record(session, message.application_id, lock=lock)
    return message, record


# ------------------------------------------------------------------ templates and drafts


@router.get("/messages/templates", response_model=list[HireTemplateOut])
async def templates(
    actor: CurrentActor, session: OrgSession, locale: str = "en"
) -> list[HireTemplateOut]:
    authorize(actor, CANDIDATE_READ)
    lang = locale if locale in {"en", "ar"} else "en"
    out: list[HireTemplateOut] = []
    for entry in library.LIBRARY:
        template = await resolve_template(session, key=entry.key, channel="email", locale=lang)
        if template is None:
            continue
        out.append(
            HireTemplateOut(
                key=entry.key,
                label=entry.label,
                label_ar=entry.label_ar,
                sensitive=template.requires_approval,
                transactional=entry.transactional,
                rejection=entry.rejection,
                ai_available=entry.key in AI_TEMPLATES,
                suggested_for=list(entry.suggested_for),
                fields=await _fields(session, entry.key, lang),
            )
        )
    return out


@router.post(
    "/pipeline/applications/{application_id}/messages",
    status_code=201,
    response_model=HireMessageOut,
)
async def create_message(
    application_id: uuid.UUID,
    body: HireDraftIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
) -> HireMessageOut:
    _writer(actor)
    record = await load_record(session, application_id)
    message = await compose.create_draft(
        session,
        record,
        _author(actor),
        template_key=body.template_key,
        values=body.values,
        links=links(request),
        locale=body.locale,
    )
    return await _message_out(session, message, record, request)


@router.get("/messages/{message_id}", response_model=HireMessageOut)
async def get_message(
    message_id: uuid.UUID, request: Request, actor: CurrentActor, session: OrgSession
) -> HireMessageOut:
    authorize(actor, CANDIDATE_READ)
    message, record = await _loaded(session, message_id)
    return await _message_out(session, message, record, request)


@router.patch("/messages/{message_id}", response_model=HireMessageOut)
async def edit_message(
    message_id: uuid.UUID,
    body: HireValuesIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
) -> HireMessageOut:
    _writer(actor)
    message, record = await _loaded(session, message_id, lock=True)
    await compose.update_draft(
        session, message, record, _author(actor), values=body.values, links=links(request)
    )
    return await _message_out(session, message, record, request)


@router.post("/messages/{message_id}/ai-draft", response_model=HireAiDraftOut)
async def ai_draft_message(
    message_id: uuid.UUID,
    body: HireAiDraftIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
) -> HireAiDraftOut:
    _writer(actor)
    message, record = await _loaded(session, message_id, lock=True)
    if message.template_key is None:
        raise ConflictError("this message has no template")
    entry = library.entry(message.template_key)
    summary = (
        await screening_summary(session, record.app.id)
        if body.include_reason and entry is not None and entry.rejection
        else None
    )
    router_ = model_router(request)
    try:
        drafted = await ai_draft(
            router_,
            actor.org_id,
            template_key=message.template_key,
            job_title=(record.job.title_ar if message.locale == "ar" else "") or record.job.title,
            locale=message.locale,
            summary=summary,
            include_reason=body.include_reason,
            actor_id=actor.id,
            message_id=message.id,
        )
    except ModelUnavailable as exc:
        raise ModelUnavailable(
            "AI drafting is not available right now. Write the message yourself."
        ) from exc
    except DraftRefusedError as exc:
        log.info("hire_message_draft_refused", message_id=str(message.id), problems=exc.problems)
        raise BadRequestError(
            "The AI draft did not pass the checks, so it was not used. Write the message yourself."
        ) from exc
    values = compose.stored_values(message) | drafted.values
    if "reason" not in drafted.values and "reason" in message.ai_fields:
        # An earlier AI reason, and no evidence for one now: drop it.
        values.pop("reason", None)
    await compose.update_draft(
        session,
        message,
        record,
        _author(actor),
        values=values,
        links=links(request),
        ai_fields=sorted(drafted.values),
        model_call_id=drafted.call_id,
        reason_source=drafted.reason_source,
    )
    return HireAiDraftOut(
        message=await _message_out(session, message, record, request),
        model=drafted.model,
        prompt_version=drafted.prompt_version,
        from_recording=drafted.from_recording,
    )


@router.post(
    "/messages/{message_id}/send",
    response_model=HireMessageOut,
    dependencies=[Depends(idempotent, scope="function")],
)
async def send(
    message_id: uuid.UUID, request: Request, actor: CurrentActor, session: OrgSession
) -> HireMessageOut:
    _writer(actor)
    message, record = await _loaded(session, message_id, lock=True)
    await compose.send_message(session, message, record, _author(actor), links=links(request))
    return await _message_out(session, message, record, request)


def _runtime(request: Request) -> WorkflowRuntime:
    runtime = getattr(request.app.state, "workflow_runtime", None)
    if not isinstance(runtime, WorkflowRuntime):
        raise ServiceUnavailableError("approvals are not available here; try again later")
    return runtime


async def start_message_run(runtime: WorkflowRuntime, actor: Actor, message_id: uuid.UUID) -> None:
    """After the response: start the approval graph. A failure is retried by the
    ``hire_sla_drafts`` job (it restarts submitted messages without a run)."""
    try:
        await runtime.start_run(
            actor,
            workflow=WORKFLOW,
            graph=GRAPH,
            input={"message_id": str(message_id)},
            entity_type="hire.message",
            entity_id=str(message_id),
        )
    except Exception as exc:
        log.warning(
            "hire_message_run_deferred", message_id=str(message_id), error=type(exc).__name__
        )


@router.post(
    "/messages/{message_id}/submit",
    response_model=HireMessageOut,
    dependencies=[Depends(idempotent, scope="function")],
)
async def submit(
    message_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    actor: CurrentActor,
    session: OrgSession,
) -> HireMessageOut:
    _writer(actor)
    runtime = _runtime(request)
    message, record = await _loaded(session, message_id, lock=True)
    await compose.submit_message(session, message, record, _author(actor), links=links(request))
    background.add_task(start_message_run, runtime, actor, message.id)
    return await _message_out(session, message, record, request)


@router.post("/messages/{message_id}/cancel", response_model=HireMessageOut)
async def cancel(
    message_id: uuid.UUID, request: Request, actor: CurrentActor, session: OrgSession
) -> HireMessageOut:
    _writer(actor)
    message, record = await _loaded(session, message_id, lock=True)
    await compose.cancel_draft(session, message, _author(actor))
    return await _message_out(session, message, record, request)


# ------------------------------------------------------------------ bulk


async def _bulk_records(
    session: AsyncSession, job_id: uuid.UUID, ids: list[uuid.UUID]
) -> tuple[list[AppRecord], int]:
    records: list[AppRecord] = []
    missing = 0
    for app_id in dict.fromkeys(ids):
        try:
            record = await load_record(session, app_id)
        except NotFoundError:
            missing += 1
            continue
        if record.app.job_id != job_id:
            missing += 1
            continue
        records.append(record)
    return records, missing


@router.post("/jobs/{job_id}/messages/bulk/preview", response_model=HireBulkMessageSummaryOut)
async def bulk_preview(
    job_id: uuid.UUID, body: HireBulkMessageIn, actor: CurrentActor, session: OrgSession
) -> HireBulkMessageSummaryOut:
    _writer(actor)
    entry = library.entry(body.template_key)
    if entry is None:
        raise BadRequestError("this is not a candidate message template")
    template = await compose.template_for(session, body.template_key, "en")
    records, missing = await _bulk_records(session, job_id, body.application_ids)
    prefs = await prefs_for_many(session, [r.candidate.id for r in records])
    opted_out = (
        0
        if entry.transactional
        else sum(1 for r in records if not prefs[r.candidate.id].updates_email)
    )
    required = [
        n
        for n, s in compose.specs_of(template).items()
        if s.required and n in compose.editable_names(compose.specs_of(template))
    ]
    return HireBulkMessageSummaryOut(
        template_key=entry.key,
        label=entry.label,
        total=len(body.application_ids),
        will_send=0 if template.requires_approval or required else len(records) - opted_out,
        opted_out=opted_out,
        not_found=missing,
        sensitive=template.requires_approval,
        missing_fields=required,
    )


@router.post(
    "/jobs/{job_id}/messages/bulk/send",
    response_model=HireBulkMessageOut,
    dependencies=[Depends(idempotent, scope="function")],
)
async def bulk_send(
    job_id: uuid.UUID,
    body: HireBulkMessageIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
) -> HireBulkMessageOut:
    """Send one non-sensitive message to each candidate (after the summary). A sensitive
    template is refused: each rejection or offer is approved on its own."""
    _writer(actor)
    template = await compose.template_for(session, body.template_key, "en")
    if template.requires_approval:
        raise ConflictError("rejections and offers are approved one by one, never in bulk")
    records, missing = await _bulk_records(session, job_id, body.application_ids)
    if missing:
        raise NotFoundError("some candidates were not found in this job")
    sent = suppressed = 0
    ids: list[uuid.UUID] = []
    for record in records:
        message = await compose.create_draft(
            session,
            record,
            _author(actor),
            template_key=body.template_key,
            purpose=MessagePurpose.BULK,
            links=links(request),
        )
        await compose.send_message(session, message, record, _author(actor), links=links(request))
        ids.append(message.id)
        if message.status == "suppressed":
            suppressed += 1
        else:
            sent += 1
    return HireBulkMessageOut(sent=sent, suppressed=suppressed, message_ids=ids)


# ------------------------------------------------------------------ settings


@router.get("/settings/messaging", response_model=HireMessagingSettingsOut)
async def get_messaging_settings(
    actor: CurrentActor, session: OrgSession
) -> HireMessagingSettingsOut:
    authorize(actor, JOB_READ)
    return HireMessagingSettingsOut(sla_days=await sla_days(session))


@router.put("/settings/messaging", response_model=HireMessagingSettingsOut)
async def put_messaging_settings(
    body: HireMessagingSettingsIn, actor: CurrentActor, session: OrgSession
) -> HireMessagingSettingsOut:
    authorize(actor, SETTINGS_MANAGE)
    return HireMessagingSettingsOut(sla_days=await save_sla_days(session, actor, body.sla_days))


# ------------------------------------------------------------------ public preferences


def _prefs_limiter(request: Request) -> SlidingWindowLimiter:
    limiter = getattr(request.app.state, "hire_prefs_limiter", None)
    if not isinstance(limiter, SlidingWindowLimiter):
        limiter = SlidingWindowLimiter(*PREFS_LIMIT)
        request.app.state.hire_prefs_limiter = limiter
    return limiter


async def _prefs_session(
    request: Request, session: AsyncSession, token: str
) -> tuple[uuid.UUID, uuid.UUID]:
    _prefs_limiter(request).hit(client_ip(request))
    config = links_from_settings(getattr(request.app.state, "notifications", None))
    if not config.secret:
        raise ServiceUnavailableError("preferences are not available here")
    claim = read_prefs_token(config.secret, token)
    if claim is None:
        raise NotFoundError("this link is not valid any more")
    set_tenant(session, claim.org_id, None)
    exists_ = await session.scalar(
        select(HireCandidate.id).where(HireCandidate.id == claim.candidate_id)
    )
    if exists_ is None:
        raise NotFoundError("this link is not valid any more")
    return claim.org_id, claim.candidate_id


async def _prefs_out(
    session: AsyncSession, org_id: uuid.UUID, candidate_id: uuid.UUID
) -> HirePrefsOut:
    prefs = await prefs_for(session, candidate_id)
    name = await session.scalar(select(Organisation.name).where(Organisation.id == org_id))
    titles = await session.scalars(
        select(HireJob.title)
        .join(HireApplication, HireApplication.job_id == HireJob.id)
        .where(
            HireApplication.candidate_id == candidate_id,
            HireApplication.status != ApplicationStatus.INCOMPLETE.value,
        )
        .distinct()
    )
    return HirePrefsOut(
        organisation=str(name or ""),
        updates_email=prefs.updates_email,
        talent_pool=prefs.talent_pool,
        jobs=sorted(titles),
    )


@router.post("/public/preferences/read", response_model=HirePrefsOut, tags=["hire-public"])
async def read_preferences(
    body: HirePrefsTokenIn, request: Request, session: RequestSession
) -> HirePrefsOut:
    org_id, candidate_id = await _prefs_session(request, session, body.token)
    return await _prefs_out(session, org_id, candidate_id)


@router.post("/public/preferences", response_model=HirePrefsOut, tags=["hire-public"])
async def change_preferences(
    body: HirePrefsIn, request: Request, session: RequestSession
) -> HirePrefsOut:
    org_id, candidate_id = await _prefs_session(request, session, body.token)
    await set_prefs(
        session,
        org_id=org_id,
        candidate_id=candidate_id,
        updates_email=body.updates_email,
        talent_pool=body.talent_pool,
    )
    return await _prefs_out(session, org_id, candidate_id)
