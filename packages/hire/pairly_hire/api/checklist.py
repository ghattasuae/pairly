# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pre-employment checklist routes (M3, brief 3.8.4). HR (recruiters) and admins only;
every change and every file opened is audited.

* ``GET|PUT /jobs/{id}/checklist``           the documents to collect for the job and the
                                             retention after hire (1 to 365 days, default 90)
* ``GET  /applications/{id}/checklist``      the candidate's checklist and its status
* ``POST /applications/{id}/checklist``      request the documents through the portal's
                                             file requests (only what is missing)
* ``POST /checklist-items/{id}/review``      ``{"outcome": "verified" | "waived"}``
* ``GET  /checklist-items/{id}/file``        the decrypted file (attachment, no-store)

The candidate uploads through ``POST /portal/document-requests/{id}/file`` as for any
requested file; a checklist document is stored encrypted on its item instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from pairly_api.uploads import get_services
from pairly_core.errors import ForbiddenError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.ingestion.storage import content_disposition
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_hire.jobs import get_job
from pairly_hire.onboarding import service
from pairly_hire.onboarding.models import HireChecklistItem
from pairly_hire.permissions import CHECKLIST_READ, CHECKLIST_WRITE
from pairly_hire.pipeline.records import load_record

router = APIRouter(tags=["hire"])


def _user(actor: Actor) -> uuid.UUID:
    if actor.kind is not ActorKind.USER or actor.id is None:
        raise ForbiddenError("only a person can do this")
    return actor.id


class HireChecklistDocIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)]
    label_ar: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] = ""


class HireChecklistConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: Annotated[list[HireChecklistDocIn], Field(min_length=1, max_length=12)]
    retention_days: Annotated[int, Field(ge=1, le=365)] = 90


class HireChecklistDocOut(BaseModel):
    key: str
    label: str
    label_ar: str


class HireChecklistConfigOut(BaseModel):
    job_id: uuid.UUID
    items: list[HireChecklistDocOut]
    retention_days: int
    saved: bool


class HireChecklistItemOut(BaseModel):
    id: uuid.UUID
    key: str
    label: str
    label_ar: str
    status: str
    has_file: bool
    content_type: str | None
    size_bytes: int | None
    received_at: datetime | None
    reviewed_at: datetime | None
    retention_until: datetime | None
    purged_at: datetime | None


class HireChecklistOut(BaseModel):
    application_id: uuid.UUID
    job_id: uuid.UUID
    retention_days: int
    items: list[HireChecklistItemOut]
    missing: list[HireChecklistDocOut]
    can_write: bool


class HireChecklistReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["verified", "waived"]


def _item_out(row: HireChecklistItem) -> HireChecklistItemOut:
    return HireChecklistItemOut(
        id=row.id,
        key=row.key,
        label=row.label,
        label_ar=row.label_ar,
        status=row.status,
        has_file=row.object_key is not None and row.purged_at is None,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        received_at=row.received_at,
        reviewed_at=row.reviewed_at,
        retention_until=row.retention_until,
        purged_at=row.purged_at,
    )


async def _checklist_out(
    session: OrgSession, actor: Actor, application_id: uuid.UUID
) -> HireChecklistOut:
    record = await load_record(session, application_id)
    config = await service.config_for(session, record.job.id)
    rows = await service.items_for(session, application_id)
    have = {r.key for r in rows}
    return HireChecklistOut(
        application_id=application_id,
        job_id=record.job.id,
        retention_days=config.retention_days,
        items=[_item_out(r) for r in rows],
        missing=[HireChecklistDocOut(**i) for i in config.items if i["key"] not in have],
        can_write=actor.has(CHECKLIST_WRITE),
    )


@router.get("/jobs/{job_id}/checklist", response_model=HireChecklistConfigOut)
async def get_config(
    job_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> HireChecklistConfigOut:
    authorize(actor, CHECKLIST_READ)
    job = await get_job(session, job_id)
    config = await service.config_for(session, job.id)
    return HireChecklistConfigOut(
        job_id=job.id,
        items=[HireChecklistDocOut(**i) for i in config.items],
        retention_days=config.retention_days,
        saved=config.saved,
    )


@router.put("/jobs/{job_id}/checklist", response_model=HireChecklistConfigOut)
async def save_config(
    job_id: uuid.UUID, body: HireChecklistConfigIn, actor: CurrentActor, session: OrgSession
) -> HireChecklistConfigOut:
    authorize(actor, CHECKLIST_WRITE)
    user = _user(actor)
    job = await get_job(session, job_id)
    config = await service.save_config(
        session,
        job,
        user,
        items=[i.model_dump() for i in body.items],
        retention_days=body.retention_days,
    )
    return HireChecklistConfigOut(
        job_id=job.id,
        items=[HireChecklistDocOut(**i) for i in config.items],
        retention_days=config.retention_days,
        saved=True,
    )


@router.get("/applications/{application_id}/checklist", response_model=HireChecklistOut)
async def get_checklist(
    application_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> HireChecklistOut:
    authorize(actor, CHECKLIST_READ)
    return await _checklist_out(session, actor, application_id)


@router.post("/applications/{application_id}/checklist", response_model=HireChecklistOut)
async def request_checklist(
    application_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> HireChecklistOut:
    authorize(actor, CHECKLIST_READ)
    authorize(actor, CHECKLIST_WRITE)
    await service.request_checklist(session, _user(actor), application_id)
    return await _checklist_out(session, actor, application_id)


@router.post("/checklist-items/{item_id}/review", response_model=HireChecklistOut)
async def review_item(
    item_id: uuid.UUID, body: HireChecklistReviewIn, actor: CurrentActor, session: OrgSession
) -> HireChecklistOut:
    authorize(actor, CHECKLIST_READ)
    authorize(actor, CHECKLIST_WRITE)
    item = await service.get_item(session, item_id, lock=True)
    await service.review(session, item, _user(actor), outcome=body.outcome)
    return await _checklist_out(session, actor, item.application_id)


@router.get("/checklist-items/{item_id}/file", response_class=Response)
async def item_file(
    item_id: uuid.UUID, request: Request, actor: CurrentActor, session: OrgSession
) -> Response:
    authorize(actor, CHECKLIST_READ)
    item = await service.get_item(session, item_id)
    opened = await service.read_file(
        session, get_services(request).store, item, actor_id=_user(actor)
    )
    return Response(
        opened.data,
        media_type=opened.content_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": content_disposition(opened.file_name, f"{item.key}"),
            "X-Content-Type-Options": "nosniff",
        },
    )
