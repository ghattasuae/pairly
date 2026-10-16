# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/decisions`` and ``/v1/appeals``: decision records, receipts, replay, appeals.

* ``GET  /v1/decisions``                        records of one subject, newest first.
* ``GET  /v1/decisions/{id}/receipt``           role-redacted receipt (JSON).
* ``GET  /v1/decisions/{id}/receipt.pdf``       the same as a PDF (``lang=en|ar``).
* ``POST /v1/decisions/{id}/replay``            re-run the decision, store the result.
* ``GET  /v1/decisions/{id}/replays``           replay results, newest first.
* ``POST /v1/decisions/{id}/appeals``           file an appeal (staff on behalf).
* ``GET  /v1/appeals``                          the appeals queue (by SLA due date;
  ``?decision_id=`` for one record's appeals, ``?status=`` for open or resolved).
* ``GET  /v1/appeals/{id}``                     one appeal.
* ``POST /v1/appeals/{id}/resolve``             close it with a written outcome.

Scoped per workflow (``pairly_core.decisions.scope``), like the approval inbox: an
actor sees the records, receipts, replays and appeals of the workflows where they hold
that workflow's read permission (``pay.invoice.read``), or of every workflow with a
core-wide grant (``core.decision.read``, ``core.appeal.read``: admin and auditor).
Replay also needs ``core.decision.replay``; filing an appeal ``core.appeal.create`` (or
the record's workflow's own ``<workflow>.appeal.create``);
resolving one the workflow's decide permission (``pay.invoice.approve``,
``hire.candidate.decide``) or ``core.appeal.decide``, with step-up. Decrypted personal
data needs ``core.decision_personal.read``. A record or appeal outside the actor's
workflows, like another organisation's, is a 404; no workflow at all is an audited 403.
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
    appeal_scope,
    create_appeal,
    get_appeal,
    list_appeals,
    resolve_appeal,
)
from pairly_core.decisions.models import AppealOutcome, AppealStatus, DecisionRecord, RequesterType
from pairly_core.decisions.permissions import (
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
from pairly_core.decisions.scope import (
    WorkflowScope,
    decide_permission,
    read_scope,
    require_scope,
)
from pairly_core.decisions.service import (
    get_record,
    history,
    open_sensitive,
    superseded_by,
    verify_record,
)
from pairly_core.errors import NotFoundError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import Actor, require

router = APIRouter(tags=["decisions"])

ReplayActor = Annotated[Actor, Depends(require(DECISION_REPLAY))]


def _record_scope(actor: CurrentActor) -> WorkflowScope:
    return require_scope(actor, read_scope(actor, DECISION_READ), DECISION_READ)


def _appeal_scope(actor: CurrentActor) -> WorkflowScope:
    return require_scope(actor, appeal_scope(actor), APPEAL_READ)


RecordScope = Annotated[WorkflowScope, Depends(_record_scope)]
AppealScope = Annotated[WorkflowScope, Depends(_appeal_scope)]
Services = Annotated[DecisionServices, Depends(get_decision_services)]
DECISION_ID_PATTERN = r"^dec_[0-9A-HJKMNP-TV-Z]{26}$"
DecisionId = Annotated[str, Path(pattern=DECISION_ID_PATTERN)]
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


async def _record(session: OrgSession, decision_id: str, scope: WorkflowScope) -> DecisionRecord:
    row = await get_record(session, decision_id)
    # Outside the actor's workflows the record does not exist for them.
    if row is None or not scope.allows(row.workflow):
        raise NotFoundError("decision not found")
    return row


async def _receipt(session: OrgSession, actor: Actor, row: DecisionRecord) -> dict[str, Any]:
    problem = await verify_record(session, row)
    audience = audience_for(actor, workflow_reader=True)
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
    scope: RecordScope,
    session: OrgSession,
    entity_type: Annotated[str, Query(min_length=1, max_length=64)],
    entity_id: Annotated[str, Query(min_length=1, max_length=128)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DecisionSummary]:
    rows = await session.scalars(
        select(DecisionRecord)
        .where(DecisionRecord.entity_type == entity_type, DecisionRecord.entity_id == entity_id)
        .where(scope.clause(DecisionRecord.workflow))
        .order_by(DecisionRecord.created_at.desc(), DecisionRecord.id.desc())
        .limit(limit)
    )
    return [_summary(r) for r in rows]


@router.get("/decisions/{decision_id}/receipt")
async def get_receipt(
    decision_id: DecisionId, actor: CurrentActor, scope: RecordScope, session: OrgSession
) -> dict[str, Any]:
    return await _receipt(session, actor, await _record(session, decision_id, scope))


@router.get(
    "/decisions/{decision_id}/receipt.pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def get_receipt_pdf(
    decision_id: DecisionId,
    actor: CurrentActor,
    scope: RecordScope,
    session: OrgSession,
    lang: Lang = Lang.EN,
) -> Response:
    data = await _receipt(session, actor, await _record(session, decision_id, scope))
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
    scope: RecordScope,
    session: OrgSession,
    services: Services,
) -> ReplayOut:
    row = await _record(session, decision_id, scope)
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
    decision_id: DecisionId, scope: RecordScope, session: OrgSession
) -> list[ReplayOut]:
    await _record(session, decision_id, scope)
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
    can_read = actor.has(DECISION_PERSONAL_READ) or (
        view.workflow is not None and decide_permission(actor, view.workflow) is not None
    )
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
    decision_id: DecisionId, body: AppealIn, actor: CurrentActor, session: OrgSession
) -> AppealOut:
    # core.appeal.create, or the record's workflow's own <workflow>.appeal.create
    # (checked by create_appeal once the record is known and readable).
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
    actor: CurrentActor,
    scope: AppealScope,
    session: OrgSession,
    status: AppealStatus | None = AppealStatus.OPEN,
    decision_id: Annotated[str | None, Query(pattern=DECISION_ID_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[AppealOut]:
    views = await list_appeals(
        session, scope=scope, status=status, decision_id=decision_id, limit=limit
    )
    return [_appeal_out(v, actor) for v in views]


@router.get("/appeals/{appeal_id}", response_model=AppealOut)
async def get_one_appeal(
    appeal_id: uuid.UUID, actor: CurrentActor, scope: AppealScope, session: OrgSession
) -> AppealOut:
    view = await get_appeal(session, appeal_id, scope)
    if view is None:
        raise NotFoundError("appeal not found")
    return _appeal_out(view, actor)


@router.post("/appeals/{appeal_id}/resolve", response_model=AppealOut)
async def resolve(
    appeal_id: uuid.UUID, body: ResolveIn, actor: CurrentActor, session: OrgSession
) -> AppealOut:
    # resolve_appeal checks the workflow's decide permission and the second factor.
    view = await resolve_appeal(
        session, actor, appeal_id, outcome=body.outcome, outcome_text=body.outcome_text
    )
    return _appeal_out(view, actor)
