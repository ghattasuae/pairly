# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Offer routes (M3, brief 3.8.3). No e-signature: a PDF plus the candidate's answer in
the portal. Every change audited.

Signed in:

* ``GET  /offer-templates``                    the templates (``hire.offer.read``)
* ``GET  /applications/{id}/offers``           the application's offers, newest first,
                                               with the approval chain; compensation only
                                               with ``hire.offer.compensation``
* ``POST /applications/{id}/offers``           build an offer from a template (201;
                                               ``hire.offer.create`` and compensation)
* ``PUT  /offers/{id}``                        edit a draft (naming the version edited)
* ``POST /offers/{id}/submit``                 submit for approval; the run starts after
                                               the response, as the submitter
* ``POST /offers/{id}/send``                   after approval: the offer email, a
                                               sensitive message with its own approval
* ``POST /offers/{id}/withdraw``               take back an unanswered offer
* ``GET  /offers/{id}/pdf?locale=``            the letter (compensation permission)

Approvers decide in the approvals inbox (``/v1/approvals/{id}/decision``).

The candidate (portal session, see ``api.portal``):

* ``GET  /portal/applications/{id}/offer``     their offer, once its email was sent
* ``GET  /portal/offers/{id}/pdf``             the letter
* ``POST /portal/offers/{id}/accept|decline``  ``{"confirm": true, "note"?}``: once,
                                               a decision record and an audit event
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

import structlog
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select

from pairly_core.approvals.models import Approval, ApprovalStep
from pairly_core.audit import ActorType
from pairly_core.errors import BadRequestError, ForbiddenError, NotFoundError
from pairly_core.identity import CurrentActor, OrgSession, RequestSession
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_hire.api.messaging import _runtime, start_message_run
from pairly_hire.api.pipeline import links
from pairly_hire.api.portal import NO_STORE, Portal, _hit
from pairly_hire.messaging.compose import Author
from pairly_hire.offers import pdf, service
from pairly_hire.offers.graph import ENTITY, GRAPH
from pairly_hire.offers.models import EDITABLE, LIVE, HireOffer, OfferStatus, as_list
from pairly_hire.offers.templates import TEMPLATES
from pairly_hire.permissions import (
    CANDIDATE_READ,
    OFFER_COMPENSATION,
    OFFER_READ,
    OFFER_WRITE,
)
from pairly_hire.pipeline.records import load_record

log = structlog.get_logger("pairly_hire.api.offers")
router = APIRouter(tags=["hire"])

_Amount = Annotated[Decimal, Field(ge=0, le=Decimal("10000000"), decimal_places=2)]
_Condition = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
PDF_MEDIA: Final = "application/pdf"


def _user(actor: Actor) -> uuid.UUID:
    if actor.kind is not ActorKind.USER or actor.id is None:
        raise ForbiddenError("only a person can do this")
    return actor.id


# ------------------------------------------------------------------ schemas


class HireCompensationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = "AED"
    base_monthly: Annotated[Decimal, Field(gt=0, le=Decimal("10000000"), decimal_places=2)]
    housing_monthly: _Amount = Decimal("0")
    transport_monthly: _Amount = Decimal("0")
    other_monthly: _Amount = Decimal("0")
    bonus_target_pct: Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)] = Decimal("0")


class HireCompensationOut(BaseModel):
    currency: str
    base_monthly: str
    housing_monthly: str
    transport_monthly: str
    other_monthly: str
    bonus_target_pct: str
    annual_total: str


class HireOfferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: Literal["standard", "fixed_term"] = "standard"
    start_date: date
    compensation: HireCompensationIn
    conditions: Annotated[list[_Condition], Field(max_length=12)] | None = None
    job_title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)] | None = (
        None
    )
    locale: Literal["en", "ar"] = "en"


class HireOfferEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    start_date: date | None = None
    compensation: HireCompensationIn | None = None
    conditions: Annotated[list[_Condition], Field(max_length=12)] | None = None
    job_title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)] | None = (
        None
    )


class HireOfferStepOut(BaseModel):
    name: str
    status: str


class HireOfferOut(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    reference: str
    template: str
    locale: str
    job_title: str
    start_date: date
    conditions: list[str]
    compensation: HireCompensationOut | None
    status: str
    status_reason: str | None
    sent: bool
    version: int
    approval_id: uuid.UUID | None
    chain: str | None
    steps: list[HireOfferStepOut]
    message_id: uuid.UUID | None
    created_by: uuid.UUID
    submitted_by: uuid.UUID | None
    approved_at: datetime | None
    responded_at: datetime | None
    response_record_id: str | None
    created_at: datetime
    can_edit: bool
    can_send: bool
    can_withdraw: bool


class HireOfferListOut(BaseModel):
    application_id: uuid.UUID
    can_write: bool
    sees_compensation: bool
    items: list[HireOfferOut]


class HireOfferTemplateOut(BaseModel):
    key: str
    name: str
    name_ar: str
    employment_type: str
    conditions: list[str]
    conditions_ar: list[str]


async def _offer_out(session: OrgSession, actor: Actor, row: HireOffer) -> HireOfferOut:
    chain: str | None = None
    steps: list[HireOfferStepOut] = []
    if row.approval_id is not None:
        chain = await session.scalar(
            select(Approval.chain_name).where(Approval.id == row.approval_id)
        )
        steps = [
            HireOfferStepOut(name=s.name, status=str(s.status))
            for s in await session.scalars(
                select(ApprovalStep)
                .where(ApprovalStep.approval_id == row.approval_id)
                .order_by(ApprovalStep.position)
            )
        ]
    sees = actor.has(OFFER_COMPENSATION)
    writer = actor.has(OFFER_WRITE) and sees
    comp = service.compensation_of(row) if sees else None
    return HireOfferOut(
        id=row.id,
        application_id=row.application_id,
        reference=row.reference,
        template=row.template_key,
        locale=row.locale,
        job_title=row.job_title,
        start_date=row.start_date,
        conditions=as_list(row.conditions),
        compensation=HireCompensationOut(**comp) if comp else None,
        status=row.status,
        status_reason=row.status_reason,
        sent=await service.visible_to_candidate(session, row),
        version=row.version,
        approval_id=row.approval_id,
        chain=chain,
        steps=steps,
        message_id=row.message_id,
        created_by=row.created_by,
        submitted_by=row.submitted_by,
        approved_at=row.approved_at,
        responded_at=row.responded_at,
        response_record_id=row.response_record_id,
        created_at=row.created_at,
        can_edit=writer and row.status in EDITABLE,
        can_send=writer and row.status == OfferStatus.APPROVED.value,
        can_withdraw=writer and row.status in LIVE,
    )


def _reader(actor: Actor) -> None:
    # Finance controllers approve offers above the threshold: they read offers (never
    # the candidate's application).
    authorize(actor, OFFER_READ)


def _writer(actor: Actor) -> uuid.UUID:
    _reader(actor)
    authorize(actor, CANDIDATE_READ)
    authorize(actor, OFFER_WRITE)
    authorize(actor, OFFER_COMPENSATION)
    return _user(actor)


async def start_offer_run(runtime: WorkflowRuntime, actor: Actor, offer_id: uuid.UUID) -> None:
    """After the response: start the approval graph as the submitter."""
    try:
        await runtime.start_run(
            actor,
            workflow="hire",
            graph=GRAPH,
            input={"offer_id": str(offer_id)},
            entity_type=ENTITY,
            entity_id=str(offer_id),
        )
    except Exception as exc:
        log.warning("hire_offer_run_deferred", offer_id=str(offer_id), error=type(exc).__name__)


async def cancel_offer_run(runtime: WorkflowRuntime, actor: Actor, run_id: uuid.UUID) -> None:
    try:
        await runtime.cancel_run(actor, run_id)
    except Exception as exc:
        log.warning("hire_offer_cancel_deferred", run_id=str(run_id), error=type(exc).__name__)


# ------------------------------------------------------------------ the hiring team


@router.get("/offer-templates", response_model=list[HireOfferTemplateOut])
async def offer_templates(actor: CurrentActor) -> list[HireOfferTemplateOut]:
    authorize(actor, OFFER_READ)
    return [
        HireOfferTemplateOut(
            key=t.key,
            name=t.name,
            name_ar=t.name_ar,
            employment_type=t.employment_type,
            conditions=list(t.conditions),
            conditions_ar=list(t.conditions_ar),
        )
        for t in TEMPLATES
    ]


async def _list_out(
    session: OrgSession, actor: Actor, application_id: uuid.UUID
) -> HireOfferListOut:
    rows = await service.offers_for(session, application_id)
    return HireOfferListOut(
        application_id=application_id,
        can_write=actor.has(OFFER_WRITE) and actor.has(OFFER_COMPENSATION),
        sees_compensation=actor.has(OFFER_COMPENSATION),
        items=[await _offer_out(session, actor, r) for r in rows],
    )


@router.get("/applications/{application_id}/offers", response_model=HireOfferListOut)
async def list_offers(
    application_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> HireOfferListOut:
    _reader(actor)
    await load_record(session, application_id)
    return await _list_out(session, actor, application_id)


@router.post("/applications/{application_id}/offers", response_model=HireOfferOut, status_code=201)
async def create_offer(
    application_id: uuid.UUID, body: HireOfferIn, actor: CurrentActor, session: OrgSession
) -> HireOfferOut:
    user = _writer(actor)
    row = await service.create_offer(
        session,
        user,
        application_id,
        template_key=body.template,
        start_date=body.start_date,
        compensation=body.compensation.model_dump(mode="json"),
        conditions=body.conditions,
        locale=body.locale,
        job_title=body.job_title,
    )
    return await _offer_out(session, actor, row)


@router.put("/offers/{offer_id}", response_model=HireOfferOut)
async def edit_offer(
    offer_id: uuid.UUID, body: HireOfferEditIn, actor: CurrentActor, session: OrgSession
) -> HireOfferOut:
    user = _writer(actor)
    row = await service.get_offer(session, offer_id, lock=True)
    await service.update_offer(
        session,
        row,
        user,
        start_date=body.start_date,
        compensation=body.compensation.model_dump(mode="json") if body.compensation else None,
        conditions=body.conditions,
        job_title=body.job_title,
        expected_version=body.expected_version,
    )
    return await _offer_out(session, actor, row)


@router.post("/offers/{offer_id}/submit", response_model=HireOfferOut)
async def submit_offer(
    offer_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    actor: CurrentActor,
    session: OrgSession,
) -> HireOfferOut:
    user = _writer(actor)
    runtime = _runtime(request)
    row = await service.get_offer(session, offer_id, lock=True)
    await service.submit_offer(session, row, user)
    background.add_task(start_offer_run, runtime, actor, row.id)
    return await _offer_out(session, actor, row)


@router.post("/offers/{offer_id}/send", response_model=HireOfferOut)
async def send_offer(
    offer_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    actor: CurrentActor,
    session: OrgSession,
) -> HireOfferOut:
    user = _writer(actor)
    runtime = _runtime(request)
    row = await service.get_offer(session, offer_id, lock=True)
    record = await load_record(session, row.application_id, lock=True)
    message = await service.send_offer(
        session, row, record, Author(ActorType.USER, str(user)), links=links(request)
    )
    background.add_task(start_message_run, runtime, actor, message.id)
    return await _offer_out(session, actor, row)


@router.post("/offers/{offer_id}/withdraw", response_model=HireOfferOut)
async def withdraw_offer(
    offer_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    actor: CurrentActor,
    session: OrgSession,
) -> HireOfferOut:
    user = _writer(actor)
    runtime = _runtime(request)
    row = await service.get_offer(session, offer_id, lock=True)
    was_pending = row.status == OfferStatus.PENDING_APPROVAL.value
    await service.withdraw(session, row, user)
    if was_pending and row.run_id is not None:
        background.add_task(cancel_offer_run, runtime, actor, row.run_id)
    return await _offer_out(session, actor, row)


def _pdf_response(data: bytes, reference: str) -> Response:
    return Response(
        data,
        media_type=PDF_MEDIA,
        headers={
            **NO_STORE,
            "Content-Disposition": f'attachment; filename="{reference}.pdf"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/offers/{offer_id}/pdf", response_class=Response)
async def offer_pdf(
    offer_id: uuid.UUID,
    actor: CurrentActor,
    session: OrgSession,
    locale: Literal["en", "ar"] | None = None,
) -> Response:
    _reader(actor)
    authorize(actor, OFFER_COMPENSATION)
    row = await service.get_offer(session, offer_id)
    data = await service.letter(session, row)
    if locale is not None and locale != data.locale:
        data = _relocalise(data, locale)
    return _pdf_response(pdf.render_offer_pdf(data), row.reference)


def _relocalise(data: service.LetterData, locale: str) -> service.LetterData:
    t = data.template
    defaults = {*t.conditions, *t.conditions_ar}
    conditions = data.conditions
    if all(c in defaults for c in conditions):
        source = t.conditions if data.locale == "en" else t.conditions_ar
        target = t.conditions_ar if locale == "ar" else t.conditions
        index = {c: i for i, c in enumerate(source)}
        conditions = [target[index[c]] if c in index else c for c in conditions]
    return replace(data, locale=locale, conditions=conditions)


# ------------------------------------------------------------------ the candidate


class HirePortalOfferOut(BaseModel):
    id: uuid.UUID
    reference: str
    company: str
    job_title: str
    start_date: date
    compensation: HireCompensationOut
    conditions: list[str]
    template_name: str
    status: str
    responded_at: datetime | None


class HirePortalOfferAnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None


async def _portal_offer(
    session: RequestSession, identity: Any, application_id: uuid.UUID
) -> HireOffer:
    rows = await service.offers_for(session, application_id)
    for row in rows:
        if row.candidate_id == identity.candidate_id and await service.visible_to_candidate(
            session, row
        ):
            return row
    raise NotFoundError("offer not found")


async def _portal_out(session: RequestSession, row: HireOffer) -> JSONResponse:
    data = await service.letter(session, row)
    out = HirePortalOfferOut(
        id=row.id,
        reference=row.reference,
        company=data.company,
        job_title=row.job_title,
        start_date=row.start_date,
        compensation=HireCompensationOut(**data.compensation),
        conditions=data.conditions,
        template_name=data.template.name_ar if row.locale == "ar" else data.template.name,
        status="sent" if row.status == OfferStatus.SENDING.value else row.status,
        responded_at=row.responded_at,
    )
    return JSONResponse(out.model_dump(mode="json"), headers=NO_STORE)


async def _own_offer(session: RequestSession, identity: Any, offer_id: uuid.UUID) -> HireOffer:
    row = await session.scalar(select(HireOffer).where(HireOffer.id == offer_id).with_for_update())
    if (
        row is None
        or row.candidate_id != identity.candidate_id
        or not await service.visible_to_candidate(session, row)
    ):
        raise NotFoundError("offer not found")
    return row


@router.get("/portal/applications/{application_id}/offer", response_model=HirePortalOfferOut)
async def portal_offer(
    application_id: uuid.UUID, identity: Portal, session: RequestSession
) -> JSONResponse:
    return await _portal_out(session, await _portal_offer(session, identity, application_id))


@router.get("/portal/offers/{offer_id}/pdf", response_class=Response)
async def portal_offer_pdf(
    offer_id: uuid.UUID, identity: Portal, session: RequestSession
) -> Response:
    row = await _own_offer(session, identity, offer_id)
    return _pdf_response(pdf.render_offer_pdf(await service.letter(session, row)), row.reference)


async def _answer(
    request: Request,
    session: RequestSession,
    identity: Any,
    offer_id: uuid.UUID,
    body: HirePortalOfferAnswerIn,
    *,
    accept: bool,
) -> JSONResponse:
    _hit(request, "reply", str(identity.candidate_id))
    if body.confirm is not True:
        raise BadRequestError("confirm your answer")
    row = await _own_offer(session, identity, offer_id)
    await service.respond(session, row, identity.candidate_id, accept=accept, note=body.note)
    return await _portal_out(session, row)


@router.post("/portal/offers/{offer_id}/accept", response_model=HirePortalOfferOut)
async def portal_accept(
    offer_id: uuid.UUID,
    body: HirePortalOfferAnswerIn,
    request: Request,
    identity: Portal,
    session: RequestSession,
) -> JSONResponse:
    return await _answer(request, session, identity, offer_id, body, accept=True)


@router.post("/portal/offers/{offer_id}/decline", response_model=HirePortalOfferOut)
async def portal_decline(
    offer_id: uuid.UUID,
    body: HirePortalOfferAnswerIn,
    request: Request,
    identity: Portal,
    session: RequestSession,
) -> JSONResponse:
    return await _answer(request, session, identity, offer_id, body, accept=False)
