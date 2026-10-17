# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Requisition routes (M3, brief 3.1.1). Signed in; every change audited.

* ``GET  /requisitions``                 the list (``hire.requisition.read``), newest first,
                                          ``?status=`` and ``?mine=true`` filters
* ``POST /requisitions``                 request a role (``hire.requisition.create``, a
                                          person); its approval run starts after the response
* ``GET  /requisitions/{id}``            one requisition, its approval and its job
* ``POST /requisitions/{id}/cancel``     the requester withdraws it while it waits
* ``POST /requisitions/{id}/restart``    start the approval run again if it never started
* ``POST /requisitions/{id}/job``        open a draft job from an approved requisition
                                          (``hire.job.write``), 201

Approvers decide in the approvals inbox (``/v1/approvals/{id}/decision``), with the
request's details as the approval's context. The requester can never approve it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select

from pairly_api.idempotency import idempotent
from pairly_core.approvals.models import Approval, ApprovalStep
from pairly_core.errors import ConflictError, ForbiddenError, ServiceUnavailableError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_hire.api.jobs import HireJobOut
from pairly_hire.permissions import (
    JOB_WRITE,
    REQUISITION_CREATE,
    REQUISITION_READ,
)
from pairly_hire.requisitions import service
from pairly_hire.requisitions.graph import ENTITY, GRAPH
from pairly_hire.requisitions.models import HireRequisition, RequisitionStatus

log = structlog.get_logger("pairly_hire.api.requisitions")
router = APIRouter(tags=["hire"])

MAX_LIST: Final = 200
_Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
_Team = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
_OptShort = Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)]
_Long = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]
_Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class HireRequisitionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: _Short
    title_ar: _OptShort = ""
    team: _Team
    budget: Annotated[Decimal, Field(gt=0, le=Decimal("999999999999"), decimal_places=2)]
    currency: _Currency = "AED"
    headcount: Annotated[int, Field(ge=1, le=100)]
    location: _Short
    work_model: Literal["onsite", "hybrid", "remote"]
    start_date: date
    justification: _Long = ""


class HireRequisitionStepOut(BaseModel):
    name: str
    status: str


class HireRequisitionOut(BaseModel):
    id: uuid.UUID
    reference: str
    title: str
    title_ar: str
    team: str
    budget: Decimal
    currency: str
    headcount: int
    location: str
    work_model: str
    start_date: date
    justification: str
    status: str
    status_reason: str | None
    requested_by: uuid.UUID
    mine: bool
    approval_id: uuid.UUID | None
    chain: str | None = None
    steps: list[HireRequisitionStepOut] = Field(default_factory=list)
    job_id: uuid.UUID | None = None
    can_open_job: bool = False
    can_cancel: bool = False
    decided_at: datetime | None
    created_at: datetime


class HireRequisitionList(BaseModel):
    items: list[HireRequisitionOut]


def _person(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can do this")


def _runtime(request: Request) -> WorkflowRuntime:
    runtime = getattr(request.app.state, "workflow_runtime", None)
    if not isinstance(runtime, WorkflowRuntime):
        raise ServiceUnavailableError("approvals are not available here; try again later")
    return runtime


async def start_requisition_run(
    runtime: WorkflowRuntime, actor: Actor, requisition_id: uuid.UUID
) -> None:
    """After the response: start the approval graph as the requester. A failure leaves
    the requisition pending without an approval; ``restart`` starts it again."""
    try:
        await runtime.start_run(
            actor,
            workflow="hire",
            graph=GRAPH,
            input={"requisition_id": str(requisition_id)},
            entity_type=ENTITY,
            entity_id=str(requisition_id),
        )
    except Exception as exc:
        log.warning(
            "hire_requisition_run_deferred",
            requisition_id=str(requisition_id),
            error=type(exc).__name__,
        )


async def cancel_requisition_run(runtime: WorkflowRuntime, actor: Actor, run_id: uuid.UUID) -> None:
    try:
        await runtime.cancel_run(actor, run_id)
    except Exception as exc:
        log.warning(
            "hire_requisition_cancel_deferred", run_id=str(run_id), error=type(exc).__name__
        )


async def _out(
    session: OrgSession,
    actor: Actor,
    rows: list[HireRequisition],
    *,
    detail: bool = False,
) -> list[HireRequisitionOut]:
    jobs = await service.jobs_of(session, [r.id for r in rows])
    chains: dict[uuid.UUID, str] = {}
    steps: dict[uuid.UUID, list[HireRequisitionStepOut]] = {}
    approval_ids = [r.approval_id for r in rows if r.approval_id is not None]
    if approval_ids:
        for approval_id, chain in (
            await session.execute(
                select(Approval.id, Approval.chain_name).where(Approval.id.in_(approval_ids))
            )
        ).all():
            chains[approval_id] = chain
        if detail:
            for step in await session.scalars(
                select(ApprovalStep)
                .where(ApprovalStep.approval_id.in_(approval_ids))
                .order_by(ApprovalStep.position)
            ):
                steps.setdefault(step.approval_id, []).append(
                    HireRequisitionStepOut(name=step.name, status=str(step.status))
                )
    can_write = actor.has(JOB_WRITE)
    out: list[HireRequisitionOut] = []
    for row in rows:
        job_id = jobs.get(row.id)
        mine = row.requested_by == actor.id
        out.append(
            HireRequisitionOut(
                id=row.id,
                reference=row.reference,
                title=row.title,
                title_ar=row.title_ar,
                team=row.team,
                budget=row.budget,
                currency=row.currency,
                headcount=row.headcount,
                location=row.location,
                work_model=row.work_model,
                start_date=row.start_date,
                justification=row.justification,
                status=row.status,
                status_reason=row.status_reason,
                requested_by=row.requested_by,
                mine=mine,
                approval_id=row.approval_id,
                chain=chains.get(row.approval_id) if row.approval_id else None,
                steps=steps.get(row.approval_id, []) if row.approval_id else [],
                job_id=job_id,
                can_open_job=(
                    can_write and job_id is None and row.status == RequisitionStatus.APPROVED.value
                ),
                can_cancel=mine and row.status == RequisitionStatus.PENDING_APPROVAL.value,
                decided_at=row.decided_at,
                created_at=row.created_at,
            )
        )
    return out


@router.get("/requisitions", response_model=HireRequisitionList)
async def list_requisitions(
    actor: CurrentActor,
    session: OrgSession,
    status: Annotated[RequisitionStatus | None, Query()] = None,
    mine: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 100,
) -> HireRequisitionList:
    authorize(actor, REQUISITION_READ)
    query = select(HireRequisition).order_by(
        HireRequisition.created_at.desc(), HireRequisition.id.desc()
    )
    if status is not None:
        query = query.where(HireRequisition.status == status.value)
    if mine:
        query = query.where(HireRequisition.requested_by == actor.id)
    rows = list(await session.scalars(query.limit(limit)))
    return HireRequisitionList(items=await _out(session, actor, rows))


@router.post(
    "/requisitions",
    status_code=201,
    response_model=HireRequisitionOut,
    dependencies=[Depends(idempotent, scope="function")],
)
async def create_requisition(
    body: HireRequisitionIn,
    request: Request,
    background: BackgroundTasks,
    actor: CurrentActor,
    session: OrgSession,
) -> HireRequisitionOut:
    authorize(actor, REQUISITION_CREATE)
    _person(actor)
    runtime = _runtime(request)
    values: dict[str, Any] = body.model_dump()
    row = await service.create_requisition(session, actor, values)
    background.add_task(start_requisition_run, runtime, actor, row.id)
    return (await _out(session, actor, [row], detail=True))[0]


@router.get("/requisitions/{requisition_id}", response_model=HireRequisitionOut)
async def get_requisition(
    requisition_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> HireRequisitionOut:
    authorize(actor, REQUISITION_READ)
    row = await service.get_requisition(session, requisition_id)
    return (await _out(session, actor, [row], detail=True))[0]


@router.post("/requisitions/{requisition_id}/cancel", response_model=HireRequisitionOut)
async def cancel_requisition(
    requisition_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    actor: CurrentActor,
    session: OrgSession,
) -> HireRequisitionOut:
    authorize(actor, REQUISITION_CREATE)
    _person(actor)
    runtime = _runtime(request)
    row = await service.get_requisition(session, requisition_id, lock=True)
    await service.cancel_requisition(session, actor, row)
    if row.run_id is not None:
        background.add_task(cancel_requisition_run, runtime, actor, row.run_id)
    return (await _out(session, actor, [row], detail=True))[0]


@router.post("/requisitions/{requisition_id}/restart", response_model=HireRequisitionOut)
async def restart_requisition(
    requisition_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    actor: CurrentActor,
    session: OrgSession,
) -> HireRequisitionOut:
    authorize(actor, REQUISITION_CREATE)
    _person(actor)
    runtime = _runtime(request)
    row = await service.get_requisition(session, requisition_id, lock=True)
    if row.requested_by != actor.id:
        raise ForbiddenError("only the person who requested the role can do this")
    if row.status != RequisitionStatus.PENDING_APPROVAL.value or row.run_id is not None:
        raise ConflictError("the approval of this requisition has already started")
    background.add_task(start_requisition_run, runtime, actor, row.id)
    return (await _out(session, actor, [row], detail=True))[0]


@router.post(
    "/requisitions/{requisition_id}/job",
    status_code=201,
    response_model=HireJobOut,
    dependencies=[Depends(idempotent, scope="function")],
)
async def open_job(
    requisition_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> HireJobOut:
    authorize(actor, JOB_WRITE)
    _person(actor)
    row = await service.get_requisition(session, requisition_id, lock=True)
    job = await service.open_job(session, actor, row)
    return HireJobOut.of(job)
