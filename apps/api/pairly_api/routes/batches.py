# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Batch API: many documents in one request, and a webhook when the batch is done.

* ``POST /v1/documents/batch?workflow=<id>`` (202). Either ``multipart/form-data`` with
  up to 50 ``file`` parts plus optional text fields ``urls`` (a JSON array) and
  ``metadata`` (a JSON object), or ``application/json`` ``{"urls": [...],
  "metadata": {...}}``. At most 50 items in all; ``metadata`` is the client's own labels
  (4 KiB of JSON), returned as given and in the webhook. Safe to retry with an
  ``Idempotency-Key``.
* ``GET /v1/batches/{id}``: the batch, its items and their outcomes. The creator may
  read it, and so may anyone holding ``core.document.read``.

Each item becomes a document exactly as a single upload does (sniffed type, the
workflow's size limit, per-workflow dedupe, virus scan queued, an audit event per
outcome). A workflow with a ``document_intake`` (Pairly Pay) also makes its entity of
each document and starts its run after the commit; its permission (``pay.invoice.upload``)
is then the one required, else ``<workflow>.document.upload`` or ``core.document.upload``.

Files are taken in the request. URLs are fetched after the response, with SSRF
protection (``pairly_core.ingestion.fetch``: https only, public addresses only, the
checked address is the one connected to, no redirects, the size limit while reading).
When every item has an outcome the batch is ``completed`` and the ``batch.completed``
webhook event is written in that same transaction. If this process stops while fetching,
the worker's ``batch_url_sweep`` fails the URL items still pending 15 minutes after the
batch was created (reason ``timed_out``) and completes the batch; submit them again.

Rate limits: the per-key request limit applies as usual; on top, each caller (API key or
person) may submit ``PAIRLY_BATCH_ITEMS_PER_MINUTE`` documents per minute (default
300), counted per item (429 with ``Retry-After``).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_api.idempotency import idempotent
from pairly_api.problems import problem_response
from pairly_api.routes.approvals import get_runtime
from pairly_api.uploads import (
    MULTIPART_OVERHEAD,
    DocumentServices,
    UnsupportedBodyError,
    enqueue_scan,
    get_services,
    receive_batch,
)
from pairly_core.api_keys.ratelimit import RateLimiter
from pairly_core.audit import actor_ref
from pairly_core.batches.intake import DocumentIntake
from pairly_core.batches.service import (
    MAX_ITEMS,
    BatchView,
    ItemStatus,
    NewItem,
    complete_if_done,
    create_batch,
    finish_item,
    get_batch,
)
from pairly_core.db import set_tenant
from pairly_core.documents.permissions import authorize_document
from pairly_core.errors import BadRequestError
from pairly_core.identity import Context, CurrentActor, OrgSession
from pairly_core.ingestion.fetch import FetchError, UrlFetcher, check_url
from pairly_core.ingestion.models import DocumentSource
from pairly_core.ingestion.service import (
    AuditBy,
    IngestOutcome,
    RejectReason,
    SpooledFile,
    ingest_spooled,
    record_rejection,
)
from pairly_core.ingestion.settings import IntakePolicy
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_core.webhooks.ssrf import UnsafeWebhookUrlError
from pairly_core.workflow.runtime import WorkflowRuntime

log = structlog.get_logger(__name__)

router = APIRouter(tags=["batches"])

WORKFLOW_PATTERN: Final = r"^[a-z][a-z0-9_]{0,31}$"
MAX_JSON_BYTES: Final = 256 * 1024
MAX_FIELD_BYTES: Final = 200 * 1024
URL_CONCURRENCY: Final = 4


# ------------------------------------------------------------------ schemas


class BatchJsonIn(BaseModel):
    urls: Annotated[
        list[Annotated[str, Field(min_length=8, max_length=2048)]], Field(max_length=MAX_ITEMS)
    ]
    metadata: dict[str, Any] | None = None


class BatchItemOut(BaseModel):
    id: uuid.UUID
    position: int
    source: str
    url: str | None
    filename: str | None
    status: str
    reason: str | None
    document_id: uuid.UUID | None
    entity_type: str | None
    entity_id: str | None
    completed_at: datetime | None


class BatchOut(BaseModel):
    id: uuid.UUID
    workflow: str
    status: str
    item_count: int
    metadata: dict[str, Any]
    counts: dict[str, int]
    items: list[BatchItemOut]
    created_at: datetime
    completed_at: datetime | None

    @classmethod
    def of(cls, view: BatchView) -> BatchOut:
        batch = view.batch
        return cls(
            id=batch.id,
            workflow=batch.workflow,
            status=batch.status,
            item_count=batch.item_count,
            metadata=batch.metadata_,
            counts=view.counts(),
            items=[
                BatchItemOut(
                    id=i.id,
                    position=i.position,
                    source=i.source,
                    url=i.url,
                    filename=i.filename,
                    status=i.status,
                    reason=i.reason,
                    document_id=i.document_id,
                    entity_type=i.entity_type,
                    entity_id=i.entity_id,
                    completed_at=i.completed_at,
                )
                for i in view.items
            ],
            created_at=batch.created_at,
            completed_at=batch.completed_at,
        )


# ------------------------------------------------------------------ plumbing


def intake_registry(request: Request) -> dict[str, DocumentIntake]:
    registry = getattr(request.app.state, "intake_registry", None)
    return registry if isinstance(registry, dict) else {}


def batch_limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "batch_limiter", None)
    if not isinstance(limiter, RateLimiter):
        limiter = RateLimiter()
        request.app.state.batch_limiter = limiter
    return limiter


def fetcher_factory(request: Request) -> Callable[[], UrlFetcher]:
    factory = getattr(request.app.state, "url_fetcher_factory", None)
    if callable(factory):
        return factory  # type: ignore[no-any-return]
    settings = getattr(request.app.state, "webhook_settings", None)
    allow_http = bool(getattr(settings, "webhook_allow_http", False))
    allow_private = bool(getattr(settings, "webhook_allow_private_targets", False))
    return lambda: UrlFetcher(allow_http=allow_http, allow_private=allow_private)


def _allow_http(request: Request) -> bool:
    settings = getattr(request.app.state, "webhook_settings", None)
    return bool(getattr(settings, "webhook_allow_http", False))


AfterCommit = Callable[[], Awaitable[None]]


@dataclass
class Processing:
    """What one batch's processing needs, request or background alike."""

    actor: Actor
    workflow: str
    policy: IntakePolicy
    services: DocumentServices
    runtime: WorkflowRuntime
    intake: DocumentIntake | None
    later: list[AfterCommit] = field(default_factory=list)

    @property
    def source(self) -> DocumentSource:
        return DocumentSource.UPLOAD if self.actor.kind is ActorKind.USER else DocumentSource.API

    async def ingest(
        self,
        session: AsyncSession,
        item_id: uuid.UUID,
        spooled: SpooledFile | None,
        filename: str | None,
    ) -> None:
        """Make a document of one item and record its outcome (in ``session``'s
        transaction); queue what must run after the commit on ``later``."""
        by = AuditBy.from_actor(self.actor)
        if spooled is None:
            await record_rejection(
                session,
                by=by,
                workflow=self.workflow,
                source=self.source,
                reason=RejectReason.TOO_LARGE,
            )
            await finish_item(session, item_id, ItemStatus.REJECTED, reason="too_large")
            return
        outcome: IngestOutcome = await ingest_spooled(
            session,
            self.services.store,
            spooled=spooled,
            workflow=self.workflow,
            source=self.source,
            filename=filename,
            policy=self.policy,
            by=by,
            submitted_by=self.actor.id if self.actor.kind is ActorKind.USER else None,
        )
        document = outcome.document
        if outcome.reason is not None or document is None:
            reason = (outcome.reason or RejectReason.EMPTY).value
            await finish_item(session, item_id, ItemStatus.REJECTED, reason=reason)
            return
        entity_type: str | None = None
        entity_id: str | None = None
        if self.intake is not None:
            result = await self.intake.handle(session, self.actor, document)
            entity_type, entity_id = result.entity_type, result.entity_id
            if result.after_commit is not None:
                start, runtime = result.after_commit, self.runtime
                self.later.append(lambda: start(runtime))
        status = ItemStatus.ACCEPTED if outcome.created else ItemStatus.DUPLICATE
        await finish_item(
            session,
            item_id,
            status,
            document_id=document.id,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        if outcome.created and (self.intake is None or self.runtime.services.scanner is None):
            queue, org, doc = self.services.queue, self.actor.org_id, document.id
            # The scan goes first: a run started after it finds the document moving.
            self.later.insert(0, lambda: enqueue_scan(queue, org, doc))

    async def flush_later(self) -> None:
        tasks, self.later = self.later, []
        for task in tasks:
            try:
                await task()
            except Exception as exc:  # the worker's sweepers recover scans and runs
                log.warning("batch_after_commit_failed", error=type(exc).__name__)


async def _process_url(
    sessionmaker: async_sessionmaker[AsyncSession],
    work: Processing,
    fetcher: UrlFetcher,
    batch_id: uuid.UUID,
    item_id: uuid.UUID,
    url: str,
) -> None:
    org = work.actor.org_id
    with tempfile.TemporaryDirectory(prefix="pairly-batch-", dir=work.services.tmp_dir) as tmp:
        fetched = None
        reason: str | None = None
        try:
            fetched = await fetcher.fetch(url, Path(tmp), max_bytes=work.policy.max_bytes)
        except FetchError as exc:
            reason = exc.reason
        session = sessionmaker()
        set_tenant(session, org, None)
        try:
            async with session, session.begin():
                if fetched is None:
                    if reason == "too_large":
                        await work.ingest(session, item_id, None, None)
                    else:
                        await finish_item(session, item_id, ItemStatus.FAILED, reason=reason)
                else:
                    await work.ingest(session, item_id, fetched.spooled, fetched.filename)
                await complete_if_done(session, batch_id)
        except Exception as exc:
            log.error("batch_item_failed", batch_id=str(batch_id), error=type(exc).__name__)
            work.later.clear()
            failed = sessionmaker()
            set_tenant(failed, org, None)
            async with failed, failed.begin():
                await finish_item(failed, item_id, ItemStatus.FAILED, reason="processing_error")
                await complete_if_done(failed, batch_id)
            return
    await work.flush_later()


async def process_urls(
    sessionmaker: async_sessionmaker[AsyncSession],
    work: Processing,
    fetcher: UrlFetcher,
    batch_id: uuid.UUID,
    items: list[tuple[uuid.UUID, str]],
) -> None:
    """Background task: fetch and ingest each URL item, a few at a time."""
    gate = asyncio.Semaphore(URL_CONCURRENCY)

    async def one(item_id: uuid.UUID, url: str) -> None:
        async with gate:
            # Each item gets its own after-commit list (they run concurrently).
            mine = Processing(
                actor=work.actor,
                workflow=work.workflow,
                policy=work.policy,
                services=work.services,
                runtime=work.runtime,
                intake=work.intake,
            )
            await _process_url(sessionmaker, mine, fetcher, batch_id, item_id, url)

    try:
        await asyncio.gather(*(one(i, u) for i, u in items))
    finally:
        await fetcher.aclose()


def _parse_urls(raw: str | None) -> list[str]:
    if raw is None or not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        raise BadRequestError("urls must be a JSON array of URLs") from None
    if not isinstance(value, list) or not all(isinstance(u, str) for u in value):
        raise BadRequestError("urls must be a JSON array of URLs")
    return list(value)


def _parse_metadata(raw: str | None) -> dict[str, Any] | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        raise BadRequestError("metadata must be a JSON object") from None
    if not isinstance(value, dict):
        raise BadRequestError("metadata must be a JSON object")
    return value


async def _json_body(request: Request) -> BatchJsonIn:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_JSON_BYTES:
        raise BadRequestError("the JSON body is too large")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_JSON_BYTES:
            raise BadRequestError("the JSON body is too large")
    try:
        return BatchJsonIn.model_validate_json(bytes(raw))
    except ValidationError:
        raise BadRequestError('send {"urls": [...], "metadata": {...}}') from None


# ------------------------------------------------------------------ routes


_BATCH_BODY: Final[dict[str, Any]] = {
    "required": True,
    "content": {
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "array",
                        "items": {"type": "string", "format": "binary"},
                        "description": f"Up to {MAX_ITEMS} files, each a 'file' part.",
                    },
                    "urls": {"type": "string", "description": "A JSON array of https URLs."},
                    "metadata": {"type": "string", "description": "A JSON object (4 KiB)."},
                },
            }
        },
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["urls"],
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 2048},
                        "maxItems": MAX_ITEMS,
                    },
                    "metadata": {"type": "object", "additionalProperties": True},
                },
            }
        },
    },
}


@router.post(
    "/documents/batch",
    status_code=202,
    response_model=BatchOut,
    dependencies=[Depends(idempotent, scope="function")],
    openapi_extra={"requestBody": _BATCH_BODY},
    responses={
        400: {"description": "Malformed body, too many items or an unsafe URL"},
        413: {"description": "The body is larger than the batch allows"},
        415: {"description": "Neither multipart/form-data nor application/json"},
        429: {"description": "Over the documents-per-minute limit"},
    },
)
async def submit_batch(
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
    background: BackgroundTasks,
    workflow: Annotated[str, Query(pattern=WORKFLOW_PATTERN)],
) -> Any:
    services = get_services(request)
    runtime = get_runtime(request)
    if workflow not in ctx.registry.workflows:
        raise BadRequestError("unknown workflow")
    intake = intake_registry(request).get(workflow)
    if intake is not None:
        authorize(actor, intake.permission)
    else:
        authorize_document(actor, ctx.registry, workflow=workflow, action="upload")
    policy = intake.policy(services.settings) if intake and intake.policy else None
    policy = policy or services.settings.policy_for(workflow)
    allow_http = _allow_http(request)
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    declared = request.headers.get("content-length")
    limit = MAX_ITEMS * (policy.max_bytes + MULTIPART_OVERHEAD)
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return problem_response(
            request, 413, "payload_too_large", detail="the batch is larger than allowed"
        )
    work = Processing(
        actor=actor,
        workflow=workflow,
        policy=policy,
        services=services,
        runtime=runtime,
        intake=intake,
    )
    # Release the database connection while the body streams in.
    await session.commit()
    with tempfile.TemporaryDirectory(prefix="pairly-batch-", dir=services.tmp_dir) as tmp:
        if media == "application/json":
            body = await _json_body(request)
            parts, urls, metadata = [], body.urls, body.metadata
        else:
            try:
                received = await receive_batch(
                    request,
                    Path(tmp),
                    max_bytes=policy.max_bytes,
                    max_files=MAX_ITEMS,
                    timeout=services.settings.upload_timeout_seconds * 4,
                    text_fields=frozenset({"urls", "metadata"}),
                    max_field_bytes=MAX_FIELD_BYTES,
                )
            except UnsupportedBodyError:
                return problem_response(
                    request,
                    415,
                    "unsupported_media_type",
                    detail="send multipart/form-data with 'file' parts, or JSON with urls",
                )
            parts = received.parts
            urls = _parse_urls(received.fields.get("urls"))
            metadata = _parse_metadata(received.fields.get("metadata"))
        for index, url in enumerate(urls):
            try:
                check_url(url, allow_http=allow_http)
            except UnsafeWebhookUrlError as exc:
                raise BadRequestError(f"urls[{index}]: {exc.detail}") from None
        items = [NewItem(source="file", filename=p.filename) for p in parts]
        items += [NewItem(source="url", url=u) for u in urls]
        if not 1 <= len(items) <= MAX_ITEMS:
            raise BadRequestError(f"a batch has 1 to {MAX_ITEMS} files or URLs")
        kind, ident = actor_ref(actor)
        batch_limiter(request).check(
            f"{kind.value}:{ident}", ctx.settings.batch_items_per_minute, cost=len(items)
        )
        view = await create_batch(session, actor, workflow=workflow, items=items, metadata=metadata)
        for part, item in zip(parts, view.items, strict=False):
            await work.ingest(session, item.id, part.file, part.filename)
        if not urls:
            await complete_if_done(session, view.batch.id)
    batch_id = view.batch.id
    # Commit before anything after it runs: the scans and runs must see the documents.
    await session.commit()
    pending = [(i.id, i.url) for i in view.items if i.source == "url" and i.url is not None]
    background.add_task(work.flush_later)
    if pending:
        background.add_task(
            process_urls,
            ctx.sessionmaker,
            work,
            fetcher_factory(request)(),
            batch_id,
            pending,
        )
    fresh = await get_batch(session, actor, batch_id)
    body_out = BatchOut.of(fresh)
    return JSONResponse(body_out.model_dump(mode="json"), status_code=202)


@router.get("/batches/{batch_id}", response_model=BatchOut)
async def read_batch(batch_id: uuid.UUID, actor: CurrentActor, session: OrgSession) -> BatchOut:
    return BatchOut.of(await get_batch(session, actor, batch_id))
