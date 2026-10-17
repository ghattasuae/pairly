# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``/v1/audit``: read, export and verify the organisation's hash-chained audit log.

* ``GET /v1/audit/events``  filters plus cursor pagination (``core.audit.read``).
* ``GET /v1/audit/export``  the whole (filtered) log as CSV or JSON, streamed
  (``core.audit.export``). Each export is itself recorded in the audit log.
* ``GET /v1/audit/verify``  recompute the chain and report the first broken ``seq``
  (``core.audit.read``), checked against the anchored heads (WP1.10b). A chain longer
  than the services' ``large_chain_events`` is verified by the worker instead: 202 with
  a job id, then ``GET /v1/audit/verify/{job_id}`` for the result.
* ``GET /v1/audit/anchors``  the organisation's anchored heads, verified
  (``core.audit.read``).
* ``GET /v1/audit/anchor-key``  the Ed25519 public key that signs the anchors. Public
  (no sign-in): anyone holding an export can check the anchors with it.

Everything is read only: there is no way to change or delete audit events.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from pairly_api.decision_services import DecisionServices, get_decision_services
from pairly_core.audit import (
    CSV_COLUMNS,
    ActorType,
    AuditAction,
    AuditFilter,
    AuditRecord,
    actor_ref,
    audit_actor_action,
    canonical_json,
    csv_row,
    format_timestamp,
    select_records,
)
from pairly_core.audit_anchor.anchoring import load_anchors, public_keys
from pairly_core.audit_anchor.signing import AnchorError, encode_key, key_id, public_bytes
from pairly_core.audit_anchor.store import AnchorStoreError
from pairly_core.audit_anchor.verification import (
    chain_length,
    get_verification,
    request_verification,
    verify_with_anchors,
)
from pairly_core.db import set_tenant
from pairly_core.decisions.jobs import VERIFY_AUDIT_CHAIN_JOB
from pairly_core.errors import BadRequestError, NotFoundError, ServiceUnavailableError
from pairly_core.identity import AppContext, Context, OrgSession
from pairly_core.models import AuditEvent
from pairly_core.rbac import Actor, require

router = APIRouter(prefix="/audit", tags=["audit"])

EXPORT_FORMAT_ID = "pairly.audit.v1"
HASH_DEFINITION = "sha256(bytes.fromhex(prev_hash) || canonical_event_json(event))"
_EXPORT_CHUNK_ROWS = 200

ReadActor = Annotated[Actor, Depends(require("core.audit.read"))]
ExportActor = Annotated[Actor, Depends(require("core.audit.export"))]
Services = Annotated[DecisionServices, Depends(get_decision_services)]


class Order(StrEnum):
    ASC = "asc"
    DESC = "desc"


class ExportFormat(StrEnum):
    CSV = "csv"
    JSON = "json"


class AuditEventOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    seq: int
    occurred_at: str
    actor_type: str
    actor_id: str | None
    action: str
    entity_type: str | None
    entity_id: str | None
    workflow: str | None
    payload: dict[str, Any]
    request_id: str | None
    prev_hash: str
    hash: str


class AuditPage(BaseModel):
    items: list[AuditEventOut]
    next_cursor: str | None


class VerifyOut(BaseModel):
    org_id: uuid.UUID
    ok: bool
    checked: int
    last_seq: int
    last_hash: str
    broken_seq: int | None
    reason: str | None
    anchors_checked: bool = False
    anchored_seq: int | None = None


class VerifyJobOut(BaseModel):
    job_id: uuid.UUID
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None


class AnchorOut(BaseModel):
    seq: int
    hash: str
    anchored_at: str
    key_id: str


class AnchorKeyOut(BaseModel):
    algorithm: str
    key_id: str
    public_key: str


def _aware(value: datetime | None, name: str) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise BadRequestError(f"{name} must include a timezone offset")
    return value


def audit_filter(
    action: Annotated[str | None, Query(max_length=128)] = None,
    actor_type: ActorType | None = None,
    actor_id: Annotated[str | None, Query(max_length=128)] = None,
    entity_type: Annotated[str | None, Query(max_length=64)] = None,
    entity_id: Annotated[str | None, Query(max_length=128)] = None,
    workflow: Annotated[str | None, Query(max_length=32)] = None,
    request_id: Annotated[str | None, Query(max_length=128)] = None,
    occurred_from: Annotated[datetime | None, Query(alias="from")] = None,
    occurred_to: Annotated[datetime | None, Query(alias="to")] = None,
) -> AuditFilter:
    """Query filters. ``from`` is inclusive, ``to`` exclusive (ISO 8601 with offset)."""
    return AuditFilter(
        action=action,
        actor_type=actor_type.value if actor_type is not None else None,
        actor_id=actor_id,
        entity_type=entity_type,
        entity_id=entity_id,
        workflow=workflow,
        request_id=request_id,
        occurred_from=_aware(occurred_from, "from"),
        occurred_to=_aware(occurred_to, "to"),
    )


Filters = Annotated[AuditFilter, Depends(audit_filter)]


# ------------------------------------------------------------------ cursors


def _encode_cursor(seq: int, order: Order) -> str:
    raw = f"v1:{order.value}:{seq}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, order: Order) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        version, cursor_order, seq = (
            base64.urlsafe_b64decode(padded.encode("ascii")).decode().split(":")
        )
        value = int(seq)
    except (ValueError, UnicodeError, binascii.Error):
        raise BadRequestError("invalid cursor") from None
    if version != "v1" or cursor_order != order.value or value < 0:
        raise BadRequestError("invalid cursor")
    return value


def _out(record: AuditRecord) -> AuditEventOut:
    return AuditEventOut.model_validate(record.to_json())


# ------------------------------------------------------------------ routes


@router.get("/events", response_model=AuditPage)
async def list_events(
    actor: ReadActor,
    session: OrgSession,
    filters: Filters,
    order: Order = Order.DESC,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=64)] = None,
) -> AuditPage:
    """Audit events of the current organisation, newest first by default."""
    query = select_records().where(AuditEvent.org_id == actor.org_id, *filters.clauses())
    if cursor is not None:
        after = _decode_cursor(cursor, order)
        query = query.where(
            AuditEvent.seq > after if order is Order.ASC else AuditEvent.seq < after
        )
    seq_order = AuditEvent.seq.asc() if order is Order.ASC else AuditEvent.seq.desc()
    rows = (await session.execute(query.order_by(seq_order).limit(limit + 1))).all()
    records = [AuditRecord.from_row(row) for row in rows[:limit]]
    next_cursor = _encode_cursor(records[-1].seq, order) if len(rows) > limit else None
    return AuditPage(items=[_out(r) for r in records], next_cursor=next_cursor)


@router.get(
    "/verify",
    response_model=VerifyOut,
    responses={202: {"model": VerifyJobOut, "description": "Large chain: queued"}},
)
async def verify(actor: ReadActor, session: OrgSession, services: Services) -> Any:
    """Recompute every hash of the organisation's chain, against its anchored heads."""
    if await chain_length(session) > services.large_chain_events:
        kind, actor_id = actor_ref(actor)
        job_id = await request_verification(
            session, org_id=actor.org_id, actor_type=kind, actor_id=actor_id
        )
        await session.commit()
        await services.queue.enqueue(
            VERIFY_AUDIT_CHAIN_JOB, str(actor.org_id), str(job_id), job_id=f"verify:{job_id}"
        )
        return JSONResponse(
            status_code=202,
            content=VerifyJobOut(job_id=job_id, status="pending").model_dump(mode="json"),
        )
    result, info = await verify_with_anchors(session, actor.org_id, services.anchor_loader())
    return VerifyOut.model_validate(result.to_json() | info)


@router.get("/verify/{job_id}", response_model=VerifyJobOut)
async def verify_job(job_id: uuid.UUID, actor: ReadActor, session: OrgSession) -> VerifyJobOut:
    row = await get_verification(session, job_id)
    if row is None:
        raise NotFoundError("verification not found")
    return VerifyJobOut(job_id=row.id, status=row.status, result=row.result, error=row.error)


@router.get("/anchors", response_model=list[AnchorOut])
async def anchors(actor: ReadActor, services: Services) -> list[AnchorOut]:
    """The organisation's anchored chain heads, each signature verified."""
    store, key = services.anchor_store, services.anchor_public_key
    if store is None or key is None:
        raise ServiceUnavailableError("audit anchors are not configured")
    try:
        heads = await load_anchors(store, actor.org_id, public_keys(key))
    except AnchorError:
        raise ServiceUnavailableError("an audit anchor does not verify") from None
    except AnchorStoreError:
        raise ServiceUnavailableError("the anchor store is unavailable") from None
    return [
        AnchorOut(seq=h.seq, hash=h.hash, anchored_at=h.anchored_at, key_id=h.key_id) for h in heads
    ]


@router.get("/anchor-key", response_model=AnchorKeyOut)
async def anchor_key(services: Services) -> AnchorKeyOut:
    """The public key that signs the anchors (no sign-in: it is public by design)."""
    key = services.anchor_public_key
    if key is None:
        raise ServiceUnavailableError("audit anchors are not configured")
    return AnchorKeyOut(
        algorithm="Ed25519", key_id=key_id(key), public_key=encode_key(public_bytes(key))
    )


@router.get(
    "/export",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/csv": {}, "application/json": {}}}},
)
async def export(
    actor: ExportActor,
    session: OrgSession,
    ctx: Context,
    filters: Filters,
    fmt: Annotated[ExportFormat, Query(alias="format")] = ExportFormat.JSON,
) -> StreamingResponse:
    """Stream the log in ``seq`` order. JSON keeps exact values, so a complete (unfiltered)
    JSON export can be re-verified offline with ``pairly_core.audit.verify_records``."""
    await audit_actor_action(
        session,
        actor,
        AuditAction.AUDIT_EXPORTED,
        payload={"format": fmt.value, "filters": _filter_summary(filters)},
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    filename = f"audit-{actor.org_id}-{stamp}.{fmt.value}"
    stream = _csv_stream if fmt is ExportFormat.CSV else _json_stream
    media = "text/csv; charset=utf-8" if fmt is ExportFormat.CSV else "application/json"
    return StreamingResponse(
        stream(ctx, actor, filters),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _filter_summary(filters: AuditFilter) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name in (
        "action",
        "actor_type",
        "actor_id",
        "entity_type",
        "entity_id",
        "workflow",
        "request_id",
    ):
        value = getattr(filters, name)
        if value is not None:
            summary[name] = value
    if filters.occurred_from is not None:
        summary["from"] = format_timestamp(filters.occurred_from)
    if filters.occurred_to is not None:
        summary["to"] = format_timestamp(filters.occurred_to)
    return summary


async def _records(
    ctx: AppContext, actor: Actor, filters: AuditFilter
) -> AsyncIterator[AuditRecord]:
    """Stream rows with a server-side cursor in a session of its own.

    The request session has already committed when the response body is sent, so the
    export opens its own session, bound to the same organisation.
    """
    session = ctx.sessionmaker()
    set_tenant(session, actor.org_id, actor.subject)
    async with session, session.begin():
        query = (
            select_records()
            .where(AuditEvent.org_id == actor.org_id, *filters.clauses())
            .order_by(AuditEvent.seq)
            .execution_options(yield_per=_EXPORT_CHUNK_ROWS)
        )
        result = await session.stream(query)
        async for row in result:
            yield AuditRecord.from_row(row)


async def _csv_stream(ctx: AppContext, actor: Actor, filters: AuditFilter) -> AsyncIterator[bytes]:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    count = 0
    async for record in _records(ctx, actor, filters):
        writer.writerow(csv_row(record))
        count += 1
        if count % _EXPORT_CHUNK_ROWS == 0:
            yield buffer.getvalue().encode("utf-8")
            buffer.seek(0)
            buffer.truncate()
    yield buffer.getvalue().encode("utf-8")


async def _json_stream(ctx: AppContext, actor: Actor, filters: AuditFilter) -> AsyncIterator[bytes]:
    header = {
        "format": EXPORT_FORMAT_ID,
        "org_id": str(actor.org_id),
        "exported_at": format_timestamp(datetime.now(UTC)),
        "hash": HASH_DEFINITION,
        "filters": _filter_summary(filters),
    }
    head = json.dumps(header, separators=(",", ":"))
    yield (head[:-1] + ',"events":[').encode("utf-8")
    first = True
    chunk: list[bytes] = []
    async for record in _records(ctx, actor, filters):
        chunk.append((b"" if first else b",") + canonical_json(record.to_json()))
        first = False
        if len(chunk) >= _EXPORT_CHUNK_ROWS:
            yield b"".join(chunk)
            chunk.clear()
    chunk.append(b"]}")
    yield b"".join(chunk)
