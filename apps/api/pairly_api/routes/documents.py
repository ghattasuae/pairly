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

import asyncio
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy import select

from pairly_api.problems import problem_response
from pairly_core.audit import audit_actor_action
from pairly_core.documents.permissions import authorize_document
from pairly_core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from pairly_core.identity import Context, CurrentActor, OrgSession
from pairly_core.ingestion.models import CONTENT_AVAILABLE, Document, DocumentSource
from pairly_core.ingestion.queue import SCAN_JOB, JobQueue
from pairly_core.ingestion.service import (
    AuditBy,
    DocumentAction,
    RejectReason,
    SpooledFile,
    SpoolWriter,
    TooLargeError,
    ingest_spooled,
    record_rejection,
)
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import ObjectStore

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

FILE_FIELD = "file"
# Room for the multipart framing around the file part.
MULTIPART_OVERHEAD = 64 * 1024
MAX_PARTS = 8
MAX_FIELD_BYTES = 4096
WORKFLOW_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"


@dataclass(frozen=True, slots=True)
class DocumentServices:
    """Storage, queue and limits for the document routes (``app.state.documents``)."""

    settings: IngestionSettings
    store: ObjectStore
    queue: JobQueue
    tmp_dir: Path | None = None


def get_services(request: Request) -> DocumentServices:
    services = getattr(request.app.state, "documents", None)
    if not isinstance(services, DocumentServices):
        raise ServiceUnavailableError("document storage is not configured")
    return services


# ------------------------------------------------------------------ schemas


class DocumentOut(BaseModel):
    id: uuid.UUID
    workflow: str
    source: str
    original_filename: str | None
    media_type: str
    size_bytes: int
    sha256: str
    status: str
    status_reason: str | None
    needs_review: bool
    submitted_by: uuid.UUID | None
    inbound_email_id: uuid.UUID | None
    page_count: int | None
    parser: str | None
    parse_warnings: list[str]
    created_at: datetime
    updated_at: datetime
    scanned_at: datetime | None
    parsed_at: datetime | None

    @classmethod
    def of(cls, doc: Document) -> DocumentOut:
        return cls(
            id=doc.id,
            workflow=doc.workflow,
            source=doc.source,
            original_filename=doc.original_filename,
            media_type=doc.media_type,
            size_bytes=doc.size_bytes,
            sha256=doc.sha256,
            status=doc.status,
            status_reason=doc.status_reason,
            needs_review=doc.needs_review,
            submitted_by=doc.submitted_by,
            inbound_email_id=doc.inbound_email_id,
            page_count=doc.page_count,
            parser=doc.parser,
            parse_warnings=list(doc.parse_warnings or []),
            created_at=doc.created_at,
            updated_at=doc.updated_at,
            scanned_at=doc.scanned_at,
            parsed_at=doc.parsed_at,
        )


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


# ------------------------------------------------------------------ multipart


class _Receiver:
    """Callbacks for the streaming multipart parser: one ``file`` part, spooled."""

    def __init__(self, tmp: Path, max_bytes: int) -> None:
        self.tmp = tmp
        self.max_bytes = max_bytes
        self.writer: SpoolWriter | None = None
        self.filename: str | None = None
        self.parts = 0
        self.files = 0
        self._headers: dict[bytes, bytes] = {}
        self._field = b""
        self._value = b""
        self._in_file = False
        self._field_bytes = 0

    def callbacks(self) -> dict[str, Callable[..., None]]:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
        }

    def _part_begin(self) -> None:
        self.parts += 1
        if self.parts > MAX_PARTS:
            raise BadRequestError("too many form parts")
        self._headers = {}
        self._in_file = False

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def _header_end(self) -> None:
        self._headers[self._field.lower()] = self._value
        self._field = self._value = b""

    def _headers_finished(self) -> None:
        _, params = parse_options_header(self._headers.get(b"content-disposition", b""))
        name = params.get(b"name", b"").decode("utf-8", errors="replace")
        if name != FILE_FIELD:
            return
        self.files += 1
        if self.files > 1:
            raise BadRequestError("send exactly one file part")
        raw_name = params.get(b"filename")
        self.filename = raw_name.decode("utf-8", errors="replace") if raw_name else None
        self.writer = SpoolWriter(self.tmp, max_bytes=self.max_bytes)
        self._in_file = True

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if self._in_file and self.writer is not None:
            self.writer.write(data[start:end])
            return
        self._field_bytes += end - start
        if self._field_bytes > MAX_FIELD_BYTES:
            raise BadRequestError("form fields too large")

    def _part_end(self) -> None:
        self._in_file = False

    def abort(self) -> None:
        if self.writer is not None:
            self.writer.abort()


async def _receive(
    request: Request, tmp: Path, max_bytes: int, timeout: float
) -> tuple[SpooledFile, str | None]:
    content_type = request.headers.get("content-type", "")
    media, params = parse_options_header(content_type)
    boundary = params.get(b"boundary")
    if media != b"multipart/form-data" or not boundary or len(boundary) > 200:
        raise _UnsupportedBodyError()
    receiver = _Receiver(tmp, max_bytes)
    parser = MultipartParser(boundary, receiver.callbacks())  # type: ignore[arg-type]
    try:
        async with asyncio.timeout(timeout):
            async for chunk in request.stream():
                parser.write(chunk)
        parser.finalize()
    except TimeoutError:
        receiver.abort()
        raise BadRequestError("upload took too long") from None
    except MultipartParseError:
        receiver.abort()
        raise BadRequestError("malformed multipart body") from None
    except BaseException:
        receiver.abort()
        raise
    if receiver.writer is None:
        raise BadRequestError(f"a '{FILE_FIELD}' file part is required")
    return receiver.writer.finish(), receiver.filename


class _UnsupportedBodyError(Exception):
    pass


_REJECT_STATUS = {
    RejectReason.TOO_LARGE: (413, "payload_too_large", "File too large"),
    RejectReason.TYPE_NOT_ALLOWED: (415, "unsupported_media_type", "File type not allowed"),
    RejectReason.EMPTY: (400, "empty_file", "File is empty"),
}


def _rejected(request: Request, reason: RejectReason, workflow: str) -> JSONResponse:
    status, code, title = _REJECT_STATUS.get(reason, (400, "rejected", "File rejected"))
    return problem_response(
        request,
        status,
        code,
        title=title,
        detail=f"{title} for workflow {workflow}",
    )


# ------------------------------------------------------------------ routes


async def _enqueue_scan(queue: JobQueue, org_id: uuid.UUID, document_id: uuid.UUID) -> None:
    # After the commit. A lost enqueue is recovered by the worker's sweeper.
    await queue.enqueue(SCAN_JOB, org_id=org_id, document_id=document_id, key="0")


@router.post(
    "",
    status_code=201,
    response_model=UploadOut,
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
        return _rejected(request, RejectReason.TOO_LARGE, workflow)

    # Release the database connection while the body streams in; the next statement
    # opens a new transaction with the same tenant context.
    await session.commit()

    with tempfile.TemporaryDirectory(prefix="pairly-upload-", dir=services.tmp_dir) as tmp:
        try:
            spooled, filename = await _receive(
                request, Path(tmp), policy.max_bytes, services.settings.upload_timeout_seconds
            )
        except _UnsupportedBodyError:
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
            return _rejected(request, RejectReason.TOO_LARGE, workflow)

        outcome = await ingest_spooled(
            session,
            services.store,
            spooled=spooled,
            workflow=workflow,
            source=DocumentSource.UPLOAD,
            filename=filename,
            policy=policy,
            by=by,
            submitted_by=actor.id,
        )

    if outcome.reason is not None or outcome.document is None:
        return _rejected(request, outcome.reason or RejectReason.EMPTY, workflow)
    document = outcome.document
    if outcome.created:
        background.add_task(_enqueue_scan, services.queue, actor.org_id, document.id)
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
