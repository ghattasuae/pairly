# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/compliance``: compliance pack exports (brief 2.3.6).

* ``POST /v1/compliance/export``                 request a pack for a period and
  workflows. 202 with the export id; the worker builds it.
* ``GET  /v1/compliance/export/{id}``            status, and the manifest when ready.
* ``POST /v1/compliance/export/{id}/download``   a short-lived signed URL (audited).

Permission ``core.compliance.export`` (admin and auditor; an ``export`` action, so the
sign-in needs a second factor). Every step is in the audit log.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from pairly_api.decision_services import DecisionServices, get_decision_services
from pairly_api.uploads import DocumentServices, get_services
from pairly_core.decisions.compliance import download_url, get_export, request_export
from pairly_core.decisions.jobs import COMPLIANCE_EXPORT_JOB
from pairly_core.decisions.models import ComplianceExport
from pairly_core.decisions.permissions import COMPLIANCE_EXPORT
from pairly_core.errors import NotFoundError
from pairly_core.identity import OrgSession
from pairly_core.rbac import Actor, require

router = APIRouter(prefix="/compliance", tags=["compliance"])

ExportActor = Annotated[Actor, Depends(require(COMPLIANCE_EXPORT))]
Services = Annotated[DecisionServices, Depends(get_decision_services)]
Storage = Annotated[DocumentServices, Depends(get_services)]
WORKFLOW_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"


class ExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start: datetime = Field(alias="from")
    period_end: datetime = Field(alias="to")
    workflows: list[Annotated[str, Field(pattern=WORKFLOW_PATTERN)]] = Field(
        default_factory=list, max_length=20
    )


class ExportOut(BaseModel):
    id: uuid.UUID
    status: str
    period_start: datetime
    period_end: datetime
    workflows: list[str]
    sha256: str | None
    manifest_sha256: str | None
    size_bytes: int | None
    manifest: dict[str, Any] | None
    error: str | None
    created_at: datetime | None
    completed_at: datetime | None


class DownloadOut(BaseModel):
    url: str
    expires_in: int
    sha256: str | None


def _out(row: ComplianceExport) -> ExportOut:
    return ExportOut(
        id=row.id,
        status=row.status,
        period_start=row.period_start,
        period_end=row.period_end,
        workflows=list(row.workflows),
        sha256=row.sha256,
        manifest_sha256=row.manifest_sha256,
        size_bytes=row.size_bytes,
        manifest=row.manifest,
        error=row.error,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


@router.post("/export", status_code=202, response_model=ExportOut)
async def export(
    body: ExportIn, actor: ExportActor, session: OrgSession, services: Services
) -> JSONResponse:
    row = await request_export(
        session,
        actor,
        period_start=body.period_start,
        period_end=body.period_end,
        workflows=body.workflows,
    )
    await session.commit()
    # After the commit, so the worker always finds the row. A lost enqueue is picked up
    # by the worker's sweeper.
    await services.queue.enqueue(
        COMPLIANCE_EXPORT_JOB, str(actor.org_id), str(row.id), job_id=f"compliance:{row.id}"
    )
    refreshed = await get_export(session, row.id)
    assert refreshed is not None  # noqa: S101
    return JSONResponse(status_code=202, content=_out(refreshed).model_dump(mode="json"))


@router.get("/export/{export_id}", response_model=ExportOut)
async def export_status(export_id: uuid.UUID, actor: ExportActor, session: OrgSession) -> ExportOut:
    row = await get_export(session, export_id)
    if row is None:
        raise NotFoundError("export not found")
    return _out(row)


@router.post("/export/{export_id}/download", response_model=DownloadOut)
async def export_download(
    export_id: uuid.UUID, actor: ExportActor, session: OrgSession, storage: Storage
) -> DownloadOut:
    ttl = storage.settings.signed_url_ttl_seconds
    url = await download_url(session, actor, storage.store, export_id, expires_in=ttl)
    row = await get_export(session, export_id)
    return DownloadOut(url=url, expires_in=ttl, sha256=row.sha256 if row else None)
