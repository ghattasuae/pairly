# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Requests between the hiring team and candidates (WP2.9), for staff.

* ``POST /v1/hire/pipeline/applications/{id}/document-requests`` (``hire.message.create``,
  people only): ask the candidate for a file (a label in English and optionally Arabic).
  The candidate sees it in the portal and uploads it there. Audited.
* ``GET  /v1/hire/pipeline/applications/{id}/document-requests`` (``hire.candidate.read``):
  the requests and their status. While the application is blind the uploaded file's id
  is left out (a file can name the person), like every other file.
* ``GET  /v1/hire/data-requests`` (``hire.settings.manage``): candidates' data requests
  (deletion) for the admins, open ones first.
* ``POST /v1/hire/data-requests/{id}/resolve`` (``hire.settings.manage``, second factor):
  ``{"status": "completed" | "declined", "outcome"}``. Records what the admin did; it
  never deletes anything itself (hard rule 1: people delete data, agents never do).
* ``GET  /v1/hire/data-requests/{id}`` (``hire.settings.manage``): one request.
* ``POST /v1/hire/data-requests/{id}/erase`` (``hire.settings.manage``, second factor,
  people only): ``{"reason"}``. The admin's manual erasure of the candidate's personal
  data for an open deletion request (``pairly_hire.erasure``): the database part commits
  first, then the stored files are deleted and the request completes with the reason as
  its outcome. When the files cannot be deleted yet, the request stays open with
  ``erasure_pending`` and ``erasure_error`` (the ``hire_erasure_objects`` job retries;
  asking again retries now). Audited. Never automatic.

Every data request carries ``due_at``, its deadline (``portal.notices.SLA_DAYS``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import case, select

from pairly_core.audit import ActorType, audit_actor_action
from pairly_core.crypto.types import seal, unseal
from pairly_core.errors import ConflictError, ForbiddenError, NotFoundError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_hire.api.common import web_url
from pairly_hire.erasure import erase_for_request, finish_erasure
from pairly_hire.models import HireCandidate
from pairly_hire.permissions import CANDIDATE_READ, MESSAGE_CREATE, SETTINGS_MANAGE
from pairly_hire.pipeline.records import hidden_identities, load_record, organisation_name
from pairly_hire.portal.models import (
    DataRequestStatus,
    DocumentRequestStatus,
    HireDataRequest,
    HireDocumentRequest,
)
from pairly_hire.portal.notices import SLA_DAYS, notify_file_requested
from pairly_hire.sealing import read_sealed

router = APIRouter(tags=["hire"])

WORKFLOW = "hire"


def _person(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can do this")


class HireDocumentRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)]
    label_ar: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] = ""


class HireDocumentRequestOut(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    label: str
    label_ar: str
    status: str
    document_id: uuid.UUID | None
    created_at: datetime
    fulfilled_at: datetime | None


class HireDataRequestOut(BaseModel):
    id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_name: str
    candidate_email: str
    kind: str
    status: str
    note: str | None
    outcome: str | None
    created_at: datetime
    due_at: datetime
    handled_at: datetime | None
    # Erasure started and the stored files are not all removed yet (request still open).
    erasure_pending: bool = False
    erasure_error: str | None = None


class HireDataRequestEraseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=2000)]


class HireDataRequestResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "declined"]
    outcome: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=2000)]


def _document_out(row: HireDocumentRequest, hidden: bool) -> HireDocumentRequestOut:
    return HireDocumentRequestOut(
        id=row.id,
        application_id=row.application_id,
        label=row.label,
        label_ar=row.label_ar,
        status=row.status,
        document_id=None if hidden else row.document_id,
        created_at=row.created_at,
        fulfilled_at=row.fulfilled_at,
    )


@router.post(
    "/pipeline/applications/{application_id}/document-requests",
    status_code=201,
    response_model=HireDocumentRequestOut,
)
async def create_document_request(
    application_id: uuid.UUID,
    body: HireDocumentRequestIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
) -> HireDocumentRequestOut:
    authorize(actor, MESSAGE_CREATE)
    _person(actor)
    record = await load_record(session, application_id)
    now = datetime.now(UTC)
    row = HireDocumentRequest(
        id=uuid.uuid4(),
        org_id=record.app.org_id,
        application_id=record.app.id,
        candidate_id=record.candidate.id,
        label=body.label,
        label_ar=body.label_ar,
        status=DocumentRequestStatus.OPEN.value,
        requested_by=actor.id,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        "hire.document_request.created",
        entity_type="hire.document_request",
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={"application_id": str(record.app.id)},
    )
    # The candidate hears of it by email, with the way into the portal.
    await notify_file_requested(
        session,
        request_id=row.id,
        email=record.email,
        locale=record.candidate.locale,
        org_name=await organisation_name(session, record.app.org_id),
        job_title=(record.job.title_ar if record.candidate.locale == "ar" else "")
        or record.job.title,
        label=(body.label_ar if record.candidate.locale == "ar" else None) or body.label,
        portal_url=f"{web_url(request)}/{record.candidate.locale}/portal/login",
    )
    hidden = record.app.id in await hidden_identities(session, [record.app])
    return _document_out(row, hidden)


@router.get(
    "/pipeline/applications/{application_id}/document-requests",
    response_model=list[HireDocumentRequestOut],
)
async def list_document_requests(
    application_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> list[HireDocumentRequestOut]:
    authorize(actor, CANDIDATE_READ)
    record = await load_record(session, application_id)
    hidden = record.app.id in await hidden_identities(session, [record.app])
    rows = await session.scalars(
        select(HireDocumentRequest)
        .where(HireDocumentRequest.application_id == record.app.id)
        .order_by(HireDocumentRequest.created_at)
    )
    return [_document_out(r, hidden) for r in rows]


def _data_out(row: HireDataRequest, candidate: HireCandidate | None) -> HireDataRequestOut:
    note = read_sealed(row, "note")
    outcome = read_sealed(row, "outcome")
    return HireDataRequestOut(
        id=row.id,
        candidate_id=row.candidate_id,
        candidate_name=str(unseal(candidate, "full_name") or "") if candidate else "",
        candidate_email=str(unseal(candidate, "email") or "") if candidate else "",
        kind=row.kind,
        status=row.status,
        note=note if isinstance(note, str) else None,
        outcome=outcome if isinstance(outcome, str) else None,
        created_at=row.created_at,
        due_at=row.created_at + timedelta(days=SLA_DAYS.get(row.kind, 30)),
        handled_at=row.handled_at,
        erasure_pending=row.erasure_started_at is not None
        and row.status == DataRequestStatus.OPEN.value,
        erasure_error=row.erasure_error,
    )


@router.get("/data-requests", response_model=list[HireDataRequestOut])
async def list_data_requests(
    actor: CurrentActor,
    session: OrgSession,
    status: Annotated[Literal["open", "completed", "declined"] | None, Query()] = None,
) -> list[HireDataRequestOut]:
    authorize(actor, SETTINGS_MANAGE)
    query = (
        select(HireDataRequest, HireCandidate)
        .join(HireCandidate, HireCandidate.id == HireDataRequest.candidate_id)
        .order_by(
            case((HireDataRequest.status == DataRequestStatus.OPEN.value, 0), else_=1),
            HireDataRequest.created_at,
        )
        .limit(200)
    )
    if status is not None:
        query = query.where(HireDataRequest.status == status)
    rows = (await session.execute(query)).all()
    await audit_actor_action(
        session,
        actor,
        "hire.data_request.listed",
        entity_type="hire.data_request",
        workflow=WORKFLOW,
        payload={"count": len(rows)},
    )
    return [_data_out(r, c) for r, c in rows]


@router.post("/data-requests/{request_id}/resolve", response_model=HireDataRequestOut)
async def resolve_data_request(
    request_id: uuid.UUID,
    body: HireDataRequestResolveIn,
    actor: CurrentActor,
    session: OrgSession,
) -> HireDataRequestOut:
    authorize(actor, SETTINGS_MANAGE)
    _person(actor)
    row = await session.scalar(
        select(HireDataRequest).where(HireDataRequest.id == request_id).with_for_update()
    )
    if row is None:
        raise NotFoundError("request not found")
    if row.status != DataRequestStatus.OPEN.value:
        raise ConflictError("this request was already resolved")
    if row.erasure_started_at is not None:
        # Only the erasure completes it, once the stored files are gone (fail closed).
        raise ConflictError("this request is being erased")
    row.status = body.status
    row.handled_by = actor.id
    row.handled_at = datetime.now(UTC)
    sealed = seal(HireDataRequest, "outcome", body.outcome, org_id=row.org_id, row_id=row.id)
    setattr(row, "outcome", sealed)  # noqa: B010 (typed column, sealed value)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        "hire.data_request.resolved",
        entity_type="hire.data_request",
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={"status": row.status, "kind": row.kind},
    )
    candidate = await session.scalar(
        select(HireCandidate).where(HireCandidate.id == row.candidate_id)
    )
    return _data_out(row, candidate)


@router.get("/data-requests/{request_id}", response_model=HireDataRequestOut)
async def get_data_request(
    request_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> HireDataRequestOut:
    authorize(actor, SETTINGS_MANAGE)
    found = (
        await session.execute(
            select(HireDataRequest, HireCandidate)
            .join(HireCandidate, HireCandidate.id == HireDataRequest.candidate_id)
            .where(HireDataRequest.id == request_id)
        )
    ).first()
    if found is None:
        raise NotFoundError("request not found")
    await audit_actor_action(
        session,
        actor,
        "hire.data_request.viewed",
        entity_type="hire.data_request",
        entity_id=request_id,
        workflow=WORKFLOW,
    )
    return _data_out(found[0], found[1])


@router.post("/data-requests/{request_id}/erase", response_model=HireDataRequestOut)
async def erase_data_request(
    request_id: uuid.UUID,
    body: HireDataRequestEraseIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
) -> HireDataRequestOut:
    authorize(actor, SETTINGS_MANAGE)
    _person(actor)
    _, candidate, _ = await erase_for_request(session, actor, request_id, reason=body.reason)
    # The rows are erased for good before any file is deleted: a failure from here on
    # leaves the request open with its error, never a file without its erasure.
    await session.commit()
    runtime = getattr(request.app.state, "workflow_runtime", None)
    store = runtime.services.store if isinstance(runtime, WorkflowRuntime) else None
    row = await finish_erasure(
        session, store, request_id, actor_type=ActorType.USER, actor_id=str(actor.id)
    )
    return _data_out(row, candidate)
