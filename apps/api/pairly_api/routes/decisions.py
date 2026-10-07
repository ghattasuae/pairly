# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/decisions`` and ``/v1/appeals``: decision records, receipts, replay, appeals.

* ``GET  /v1/decisions``                        records of one subject, newest first.
* ``GET  /v1/decisions/{id}/receipt``           role-redacted receipt (JSON).
* ``GET  /v1/decisions/{id}/receipt.pdf``       the same as a PDF (``lang=en|ar``).
* ``POST /v1/decisions/{id}/replay``            re-run the decision, store the result.
* ``GET  /v1/decisions/{id}/replays``           replay results, newest first.
* ``POST /v1/decisions/{id}/appeals``           file an appeal (staff on behalf).
* ``GET  /v1/appeals``                          the appeals queue (by SLA due date).
* ``GET  /v1/appeals/{id}``                     one appeal.
* ``POST /v1/appeals/{id}/resolve``             close it with a written outcome.

Permissions: ``core.decision.read`` (receipts; decrypted personal data needs
``core.decision_personal.read``), ``core.decision.replay``, ``core.appeal.read``,
``core.appeal.create``, ``core.appeal.decide`` (step-up). Another organisation's record
or appeal is a 404 (Row Level Security hides it).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from pairly_api.decision_services import DecisionServices, get_decision_services
from pairly_core.audit import actor_ref, audit_actor_action
from pairly_core.decisions.appeals import (
    AppealView,
    create_appeal,
    get_appeal,
    list_appeals,
    resolve_appeal,
)
from pairly_core.decisions.models import AppealOutcome, AppealStatus, DecisionRecord, RequesterType
from pairly_core.decisions.permissions import (
    APPEAL_CREATE,
    APPEAL_DECIDE,
    APPEAL_READ,
    DECISION_PERSONAL_READ,
    DECISION_READ,
    DECISION_REPLAY,
)
from pairly_core.decisions.receipts import (
    Audience,
    ReceiptContext,
    audience_for,
    receipt,
    render_pdf,
)
from pairly_core.decisions.replay import list_replays, replay_decision
from pairly_core.decisions.service import (
    get_record,
    history,
    open_sensitive,
    superseded_by,
    verify_record,
)
from pairly_core.errors import NotFoundError
from pairly_core.identity import OrgSession
from pairly_core.rbac import Actor, require

router = APIRouter(tags=["decisions"])

ReadActor = Annotated[Actor, Depends(require(DECISION_READ))]
ReplayActor = Annotated[Actor, Depends(require(DECISION_REPLAY))]
AppealReader = Annotated[Actor, Depends(require(APPEAL_READ))]
AppealCreator = Annotated[Actor, Depends(require(APPEAL_CREATE))]
AppealDecider = Annotated[Actor, Depends(require(APPEAL_DECIDE))]
Services = Annotated[DecisionServices, Depends(get_decision_services)]
DecisionId = Annotated[str, Path(pattern=r"^dec_[0-9A-HJKMNP-TV-Z]{26}$")]
PERSONAL_DATA_VIEWED = "core.decision.personal_data_viewed"


class DecisionSummary(BaseModel):
    id: str
    kind: str
    workflow: str
    entity_type: str
    entity_id: str
    output: dict[str, Any]
    confidence: float | None
    final_action: str | None
    decided_at: datetime
    supersedes: str | None


class ReplayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = Field(default="recorded", pattern=r"^(recorded|live)$")


class ReplayOut(BaseModel):
    id: uuid.UUID
    decision_id: str
    mode: str
    deterministic: bool
    status: str
    reason: str | None
    diff: list[dict[str, Any]]
    details: dict[str, Any]
    created_at: datetime | None = None


class AppealIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requester_type: RequesterType
    requester_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=4000)


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: AppealOutcome
    outcome_text: str = Field(min_length=1, max_length=4000)


class AppealOut(BaseModel):
    id: uuid.UUID
    decision_id: str
    requester_type: str
    requester_id: str
    filed_by: uuid.UUID | None
    reason: str | dict[str, bool]
    status: str
    outcome: str | None
    outcome_text: str | dict[str, bool] | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    outcome_decision_id: str | None
    sla_due_at: datetime
    escalated_at: datetime | None
    created_at: datetime


class Lang(StrEnum):
    EN = "en"
    AR = "ar"


def _summary(row: DecisionRecord) -> DecisionSummary:
    return DecisionSummary(
        id=row.id,
        kind=row.kind,
        workflow=row.workflow,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        output=dict(row.output),
        confidence=row.confidence,
        final_action=row.final_action,
        decided_at=row.decided_at,
        supersedes=row.supersedes,
    )


async def _record(session: OrgSession, decision_id: str) -> DecisionRecord:
    row = await get_record(session, decision_id)
    if row is None:
        raise NotFoundError("decision not found")
    return row


async def _receipt(session: OrgSession, actor: Actor, row: DecisionRecord) -> dict[str, Any]:
    problem = await verify_record(session, row)
    audience = audience_for(actor)
    sensitive = None if problem == "decryption_failed" else open_sensitive(row)
    data = receipt(
        row,
        ReceiptContext(
            sensitive=sensitive,
            intact=problem is None,
            history=tuple(await history(session, row)),
            superseded_by=await superseded_by(session, row.id),
        ),
        audience,
    )
    if audience is Audience.INTERNAL_FULL and sensitive:
        # Decrypted personal data was shown: who saw it is part of the record.
        await audit_actor_action(
            session,
            actor,
            PERSONAL_DATA_VIEWED,
            entity_type="decision_record",
            entity_id=row.id,
            workflow=row.workflow,
        )
    return data


@router.get("/decisions", response_model=list[DecisionSummary])
async def list_decisions(
    actor: ReadActor,
    session: OrgSession,
    entity_type: Annotated[str, Query(min_length=1, max_length=64)],
    entity_id: Annotated[str, Query(min_length=1, max_length=128)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DecisionSummary]:
    rows = await session.scalars(
        select(DecisionRecord)
        .where(DecisionRecord.entity_type == entity_type, DecisionRecord.entity_id == entity_id)
        .order_by(DecisionRecord.created_at.desc(), DecisionRecord.id.desc())
        .limit(limit)
    )
    return [_summary(r) for r in rows]


@router.get("/decisions/{decision_id}/receipt")
async def get_receipt(
    decision_id: DecisionId, actor: ReadActor, session: OrgSession
) -> dict[str, Any]:
    return await _receipt(session, actor, await _record(session, decision_id))


@router.get(
    "/decisions/{decision_id}/receipt.pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def get_receipt_pdf(
    decision_id: DecisionId,
    actor: ReadActor,
    session: OrgSession,
    lang: Lang = Lang.EN,
) -> Response:
    data = await _receipt(session, actor, await _record(session, decision_id))
    pdf = render_pdf(data, lang=lang.value)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="receipt-{decision_id}-{lang.value}.pdf"',
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/decisions/{decision_id}/replay", response_model=ReplayOut, status_code=201)
async def replay(
    decision_id: DecisionId,
    body: ReplayIn,
    actor: ReplayActor,
    session: OrgSession,
    services: Services,
) -> ReplayOut:
    row = await _record(session, decision_id)
    kind, actor_id = actor_ref(actor)
    outcome = await replay_decision(
        session,
        services.router,
        row,
        live=body.mode == "live",
        actor_type=kind,
        actor_id=actor_id,
    )
    return ReplayOut(
        id=outcome.replay_id,
        decision_id=outcome.decision_id,
        mode=outcome.mode.value,
        deterministic=outcome.deterministic,
        status=outcome.status.value,
        reason=outcome.reason,
        diff=outcome.diff,
        details=outcome.details,
    )


@router.get("/decisions/{decision_id}/replays", response_model=list[ReplayOut])
async def get_replays(
    decision_id: DecisionId, actor: ReadActor, session: OrgSession
) -> list[ReplayOut]:
    await _record(session, decision_id)
    return [
        ReplayOut(
            id=r.id,
            decision_id=r.decision_id,
            mode=r.mode,
            deterministic=r.deterministic,
            status=r.status,
            reason=r.reason,
            diff=list(r.diff),
            details=dict(r.details),
            created_at=r.created_at,
        )
        for r in await list_replays(session, decision_id)
    ]


# ------------------------------------------------------------------ appeals


def _appeal_out(view: AppealView, actor: Actor) -> AppealOut:
    # The reason and outcome are people's own words: shown to who decides appeals and
    # to who may read personal data, redacted for the rest of the queue's readers.
    can_read = actor.has(APPEAL_DECIDE) or actor.has(DECISION_PERSONAL_READ)
    hidden = {"redacted": True}
    return AppealOut(
        id=view.id,
        decision_id=view.decision_id,
        requester_type=view.requester_type,
        requester_id=view.requester_id,
        filed_by=view.filed_by,
        reason=view.reason if can_read else hidden,
        status=view.status,
        outcome=view.outcome,
        outcome_text=(view.outcome_text if can_read or view.outcome_text is None else hidden),
        decided_by=view.decided_by,
        decided_at=view.decided_at,
        outcome_decision_id=view.outcome_decision_id,
        sla_due_at=view.sla_due_at,
        escalated_at=view.escalated_at,
        created_at=view.created_at,
    )


@router.post("/decisions/{decision_id}/appeals", response_model=AppealOut, status_code=201)
async def file_appeal(
    decision_id: DecisionId, body: AppealIn, actor: AppealCreator, session: OrgSession
) -> AppealOut:
    view = await create_appeal(
        session,
        actor,
        decision_id=decision_id,
        requester_type=body.requester_type,
        requester_id=body.requester_id,
        reason=body.reason,
    )
    return _appeal_out(view, actor)


@router.get("/appeals", response_model=list[AppealOut])
async def appeals_queue(
    actor: AppealReader,
    session: OrgSession,
    status: AppealStatus | None = AppealStatus.OPEN,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[AppealOut]:
    return [_appeal_out(v, actor) for v in await list_appeals(session, status=status, limit=limit)]


@router.get("/appeals/{appeal_id}", response_model=AppealOut)
async def get_one_appeal(
    appeal_id: uuid.UUID, actor: AppealReader, session: OrgSession
) -> AppealOut:
    row = await get_appeal(session, appeal_id)
    if row is None:
        raise NotFoundError("appeal not found")
    return _appeal_out(AppealView.of(row), actor)


@router.post("/appeals/{appeal_id}/resolve", response_model=AppealOut)
async def resolve(
    appeal_id: uuid.UUID, body: ResolveIn, actor: AppealDecider, session: OrgSession
) -> AppealOut:
    view = await resolve_appeal(
        session, actor, appeal_id, outcome=body.outcome, outcome_text=body.outcome_text
    )
    return _appeal_out(view, actor)
