# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Requisitions: request a role, follow its approval, open a job from it.

* ``create_requisition``: a person (hiring manager, recruiter or admin) requests a role.
  It starts ``pending_approval``; the API then starts the ``hire_requisition`` run as
  that person, so the approval engine excludes them from approving it.
* ``cancel_requisition``: the requester withdraws a pending request; the API then
  cancels its run (and with it the open approval). An approval decided after that
  changes nothing: the final action refuses a requisition that is not pending.
* ``open_job``: a draft job from an ``approved`` requisition only (one job per
  requisition; its ``headcount`` is the number of people to hire for it). Any other
  status is refused: no job is opened before the chain approved.

Every change is audited (``hire.requisition.*``) in the caller's transaction.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, actor_ref, append_audit
from pairly_core.errors import ConflictError, ForbiddenError, NotFoundError
from pairly_core.rbac import Actor
from pairly_hire.jobs import create_job
from pairly_hire.models import HireJob
from pairly_hire.requisitions.models import HireRequisition, RequisitionStatus

WORKFLOW: Final = "hire"
ENTITY: Final = "hire.requisition"
SYSTEM_ACTOR: Final = "pairly.hire.requisition_graph"
_REF_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_reference() -> str:
    return "REQ-" + "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))


def payload_of(row: HireRequisition) -> dict[str, Any]:
    """What the approval covers (canonical JSON: strings and integers only)."""
    return {
        "requisition_id": str(row.id),
        "reference": row.reference,
        "title": row.title,
        "team": row.team,
        "budget": f"{Decimal(row.budget):.2f}",
        "currency": row.currency,
        "headcount": int(row.headcount),
        "location": row.location,
        "work_model": row.work_model,
        "start_date": row.start_date.isoformat(),
        "requested_by": str(row.requested_by),
    }


async def audit_requisition(
    session: AsyncSession,
    actor: Actor | None,
    action: str,
    row: HireRequisition,
    extra: dict[str, Any] | None = None,
) -> None:
    if actor is None:
        kind, actor_id = ActorType.SYSTEM, SYSTEM_ACTOR
    else:
        kind, actor_id = actor_ref(actor)
    await append_audit(
        session,
        action=action,
        actor_type=kind,
        actor_id=actor_id,
        entity_type=ENTITY,
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={"reference": row.reference, "status": row.status} | (extra or {}),
    )


async def locked(session: AsyncSession, requisition_id: str | uuid.UUID) -> HireRequisition:
    row = await session.scalar(
        select(HireRequisition)
        .where(HireRequisition.id == uuid.UUID(str(requisition_id)))
        .with_for_update()
    )
    if row is None:
        raise NotFoundError("requisition not found")
    return row


async def get_requisition(
    session: AsyncSession, requisition_id: uuid.UUID, *, lock: bool = False
) -> HireRequisition:
    query = select(HireRequisition).where(HireRequisition.id == requisition_id)
    if lock:
        query = query.with_for_update()
    row = await session.scalar(query)
    if row is None:
        raise NotFoundError("requisition not found")
    return row


async def create_requisition(
    session: AsyncSession, actor: Actor, values: dict[str, Any]
) -> HireRequisition:
    if actor.id is None:
        raise ForbiddenError("only a person can request a role")
    now = datetime.now(UTC)
    start = values["start_date"]
    if not isinstance(start, date) or start < now.date():
        raise ConflictError("the start date must be today or later")
    row = HireRequisition(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        reference=new_reference(),
        title=str(values["title"]),
        title_ar=str(values.get("title_ar") or ""),
        team=str(values["team"]),
        budget=Decimal(str(values["budget"])),
        currency=str(values["currency"]).upper(),
        headcount=int(values["headcount"]),
        location=str(values["location"]),
        work_model=str(values["work_model"]),
        start_date=start,
        justification=str(values.get("justification") or ""),
        status=RequisitionStatus.PENDING_APPROVAL.value,
        requested_by=actor.id,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    await audit_requisition(
        session,
        actor,
        "hire.requisition.requested",
        row,
        {"budget": f"{row.budget:.2f}", "currency": row.currency, "headcount": row.headcount},
    )
    return row


async def cancel_requisition(
    session: AsyncSession, actor: Actor, row: HireRequisition
) -> HireRequisition:
    if row.requested_by != actor.id:
        raise ForbiddenError("only the person who requested the role can withdraw it")
    if row.status != RequisitionStatus.PENDING_APPROVAL.value:
        raise ConflictError("only a requisition waiting for approval can be withdrawn")
    now = datetime.now(UTC)
    row.status = RequisitionStatus.CANCELLED.value
    row.status_reason = "withdrawn"
    row.decided_at = now
    row.updated_at = now
    await session.flush()
    await audit_requisition(session, actor, "hire.requisition.cancelled", row)
    return row


async def jobs_of(
    session: AsyncSession, requisition_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """The job opened from each requisition (at most one)."""
    if not requisition_ids:
        return {}
    rows = await session.execute(
        select(HireJob.requisition_id, HireJob.id).where(
            HireJob.requisition_id.in_(requisition_ids)
        )
    )
    return {r: j for r, j in rows.all() if r is not None}


async def open_job(session: AsyncSession, actor: Actor, row: HireRequisition) -> HireJob:
    if row.status != RequisitionStatus.APPROVED.value:
        raise ConflictError("a job can be opened only from an approved requisition")
    existing = await session.scalar(
        select(HireJob.id).where(HireJob.requisition_id == row.id).limit(1)
    )
    if existing is not None:
        raise ConflictError("a job is already open for this requisition")
    job = await create_job(
        session,
        actor,
        {
            "title": row.title,
            "title_ar": row.title_ar,
            "team": row.team,
            "location": row.location,
            "work_model": row.work_model,
            "employment_type": "full_time",
        },
    )
    job.requisition_id = row.id
    await session.flush()
    await audit_requisition(
        session, actor, "hire.requisition.job_opened", row, {"job_id": str(job.id)}
    )
    return job
