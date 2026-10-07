# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/documents``: upload, read and download documents.

* ``POST /v1/documents?workflow=<id>``  multipart/form-data with one ``file`` part.
  The body is streamed: the file part goes straight to a private temp file while it is
  hashed and counted, and reading stops as soon as the workflow's size limit is passed.
  The type is sniffed from the bytes; the client's type and name are never trusted.
  201 with the new document, 200 when the same bytes were already submitted to this
  workflow (dedupe is per workflow), 413 too large, 415 type not allowed, 400 malformed. Every
  outcome writes an audit event. The virus scan is queued after the commit.
* ``GET /v1/documents/{id}``               the document's record.
* ``GET /v1/documents/{id}/content-url``   a short-lived signed GET URL, only after the
  RBAC check and only once the document passed its virus scan.

Permissions follow ``pairly_core.documents.permissions``: ``<workflow>.document.*`` when
the workflow declares it, else ``core.document.*``. Another organisation's document is
a 404 (Row Level Security hides it).
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from pairly_api.idempotency import idempotent
from pairly_api.problems import problem_response
from pairly_api.uploads import (
    MULTIPART_OVERHEAD,
    DocumentOut,
    UnsupportedBodyError,
    enqueue_scan,
    get_services,
    receive_upload,
    rejected_response,
)
from pairly_core.audit import audit_actor_action
from pairly_core.documents.permissions import authorize_document
from pairly_core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
)
from pairly_core.identity import Context, CurrentActor, OrgSession
from pairly_core.ingestion.models import CONTENT_AVAILABLE, Document, DocumentSource
from pairly_core.ingestion.service import (
    AuditBy,
    DocumentAction,
    RejectReason,
    TooLargeError,
    ingest_spooled,
    record_rejection,
)
from pairly_core.rbac import ActorKind

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

WORKFLOW_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"


# ------------------------------------------------------------------ schemas


class UploadOut(BaseModel):
    document_id: uuid.UUID
    duplicate: bool
    # Dedupe is per workflow, so a duplicate is always a document of the same workflow.
    document: DocumentOut


class ContentUrlOut(BaseModel):
    url: str
    expires_at: datetime
    media_type: str
    filename: str | None


# ------------------------------------------------------------------ routes


@router.post(
    "",
    status_code=201,
    response_model=UploadOut,
    # Safe to retry with the same Idempotency-Key (pairly_api.idempotency).
    dependencies=[Depends(idempotent, scope="function")],
    responses={
        200: {"model": UploadOut, "description": "Already submitted (deduplicated)"},
        400: {"description": "Malformed body or empty file"},
        413: {"description": "File larger than the workflow allows"},
        415: {"description": "File type not allowed, or the body is not multipart"},
    },
)
async def upload_document(
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
    background: BackgroundTasks,
    workflow: Annotated[str, Query(pattern=WORKFLOW_PATTERN)],
) -> Any:
    services = get_services(request)
    if workflow not in ctx.registry.workflows:
        raise BadRequestError("unknown workflow")
    authorize_document(actor, ctx.registry, workflow=workflow, action="upload")
    policy = services.settings.policy_for(workflow)
    by = AuditBy.from_actor(actor)

    declared = request.headers.get("content-length")
    if (
        declared is not None
        and declared.isdigit()
        and int(declared) > policy.max_bytes + MULTIPART_OVERHEAD
    ):
        await record_rejection(
            session,
            by=by,
            workflow=workflow,
            source=DocumentSource.UPLOAD,
            reason=RejectReason.TOO_LARGE,
        )
        return rejected_response(request, RejectReason.TOO_LARGE, workflow)

    # Release the database connection while the body streams in; the next statement
    # opens a new transaction with the same tenant context.
    await session.commit()

    with tempfile.TemporaryDirectory(prefix="pairly-upload-", dir=services.tmp_dir) as tmp:
        try:
            spooled, filename = await receive_upload(
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
            await record_rejection(
                session,
                by=by,
                workflow=workflow,
                source=DocumentSource.UPLOAD,
                reason=RejectReason.TOO_LARGE,
            )
            return rejected_response(request, RejectReason.TOO_LARGE, workflow)

        outcome = await ingest_spooled(
            session,
            services.store,
            spooled=spooled,
            workflow=workflow,
            source=DocumentSource.UPLOAD,
            filename=filename,
            policy=policy,
            by=by,
            # Only a person is a users row; an API key's upload is attributed in the audit
            # log (actor type api_key) instead.
            submitted_by=actor.id if actor.kind is ActorKind.USER else None,
        )

    if outcome.reason is not None or outcome.document is None:
        return rejected_response(request, outcome.reason or RejectReason.EMPTY, workflow)
    document = outcome.document
    if outcome.created:
        background.add_task(enqueue_scan, services.queue, actor.org_id, document.id)
        return UploadOut(
            document_id=document.id, duplicate=False, document=DocumentOut.of(document)
        )
    body = UploadOut(document_id=document.id, duplicate=True, document=DocumentOut.of(document))
    return JSONResponse(body.model_dump(mode="json"), status_code=200)


async def _load(session: OrgSession, document_id: uuid.UUID) -> Document:
    document = await session.scalar(select(Document).where(Document.id == document_id))
    if document is None:
        raise NotFoundError("document not found")
    return document


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: uuid.UUID, actor: CurrentActor, session: OrgSession, ctx: Context
) -> DocumentOut:
    document = await _load(session, document_id)
    authorize_document(
        actor, ctx.registry, workflow=document.workflow, action="read", resource=document
    )
    return DocumentOut.of(document)


@router.get("/{document_id}/content-url", response_model=ContentUrlOut)
async def get_content_url(
    request: Request,
    document_id: uuid.UUID,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
) -> ContentUrlOut:
    services = get_services(request)
    document = await _load(session, document_id)
    authorize_document(
        actor, ctx.registry, workflow=document.workflow, action="read", resource=document
    )
    if document.status not in CONTENT_AVAILABLE:
        # Never hand out bytes that have not passed the virus scan.
        raise ConflictError("content is not available until the document passes its virus scan")
    ttl = services.settings.signed_url_ttl_seconds
    url = services.store.presigned_get_url(
        document.storage_key,
        expires_in=ttl,
        filename=document.original_filename,
        content_type=document.media_type,
    )
    await audit_actor_action(
        session,
        actor,
        DocumentAction.CONTENT_URL_ISSUED,
        entity_type="document",
        entity_id=document.id,
        workflow=document.workflow,
        payload={"expires_in": ttl},
    )
    return ContentUrlOut(
        url=url,
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        media_type=document.media_type,
        filename=document.original_filename,
    )
