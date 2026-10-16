# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Document batches: create, record item outcomes, complete, read.

* ``create_batch`` writes the batch and its pending items (one audit event).
* ``finish_item`` records one item's outcome.
* ``complete_if_done`` locks the batch row; when no item is pending any more it marks
  the batch completed and writes the ``batch.completed`` webhook event and an audit
  event in the same transaction, exactly once (the lock serialises concurrent items).
* ``get_batch`` reads a batch and its items (404 when RLS hides it). A person or key
  only reads the batches they created, unless they hold ``core.document.read``.
* ``fail_stale_url_items`` (the worker's ``batch_url_sweep``): URL items still pending
  ``STALE_AFTER_MINUTES`` after their batch was created are failed with the reason
  ``timed_out``, one audit event per batch, and their batches complete. They are failed,
  not retried: the URL was fetched by the API process under the submitter's authority
  (person or API key), which the worker does not hold, and running the workflow intake
  again as the system would act for someone who is no longer there. The client sees the
  ``timed_out`` outcome in ``batch.completed`` and submits the URL again.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import actor_ref, append_audit, audit_actor_action
from pairly_core.batches.models import DocumentBatch, DocumentBatchItem
from pairly_core.errors import BadRequestError, NotFoundError
from pairly_core.rbac import Actor
from pairly_core.webhooks.events import BATCH_COMPLETED, publish_event

MAX_ITEMS: Final = 50
MAX_METADATA_BYTES: Final = 4096
BATCH_ENTITY: Final = "document_batch"
SYSTEM_ACTOR: Final = "pairly.batches"
SWEEPER_ACTOR: Final = "pairly-worker.batch_url_sweep"
# Long past any batch's fetches (50 URLs, a few at a time, each with its own timeout).
STALE_AFTER_MINUTES: Final = 15
TIMED_OUT: Final = "timed_out"


class BatchAction:
    CREATED: Final = "core.batch.created"
    COMPLETED: Final = "core.batch.completed"
    ITEMS_TIMED_OUT: Final = "core.batch.items_timed_out"


class ItemStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    FAILED = "failed"


class BatchStatus(StrEnum):
    PROCESSING = "processing"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class NewItem:
    source: str  # "file" or "url"
    url: str | None = None
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class BatchView:
    batch: DocumentBatch
    items: list[DocumentBatchItem]

    def counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in ItemStatus}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts


def check_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Plain JSON of at most ``MAX_METADATA_BYTES``, or ``BadRequestError``."""
    if metadata is None:
        return {}
    try:
        raw = json.dumps(dict(metadata), allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise BadRequestError("metadata must be a JSON object") from None
    if len(raw.encode("utf-8")) > MAX_METADATA_BYTES:
        raise BadRequestError(f"metadata must be at most {MAX_METADATA_BYTES} bytes of JSON")
    clean: dict[str, Any] = json.loads(raw)
    return clean


async def create_batch(
    session: AsyncSession,
    actor: Actor,
    *,
    workflow: str,
    items: Sequence[NewItem],
    metadata: Mapping[str, Any] | None = None,
) -> BatchView:
    if not 1 <= len(items) <= MAX_ITEMS:
        raise BadRequestError(f"a batch has 1 to {MAX_ITEMS} files or URLs")
    clean = check_metadata(metadata)
    by_type, by_id = actor_ref(actor)
    batch = DocumentBatch(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        workflow=workflow,
        status=BatchStatus.PROCESSING.value,
        item_count=len(items),
        metadata_=clean,
        created_by_type=by_type.value,
        created_by_id=by_id,
    )
    session.add(batch)
    await session.flush()
    rows = [
        DocumentBatchItem(
            id=uuid.uuid4(),
            org_id=actor.org_id,
            batch_id=batch.id,
            position=index,
            source=item.source,
            url=item.url,
            filename=item.filename,
            status=ItemStatus.PENDING.value,
        )
        for index, item in enumerate(items)
    ]
    session.add_all(rows)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        BatchAction.CREATED,
        entity_type=BATCH_ENTITY,
        entity_id=batch.id,
        workflow=workflow,
        payload={
            "items": len(rows),
            "files": sum(1 for r in rows if r.source == "file"),
            "urls": sum(1 for r in rows if r.source == "url"),
        },
    )
    return BatchView(batch=batch, items=rows)


async def finish_item(
    session: AsyncSession,
    item_id: uuid.UUID,
    status: ItemStatus,
    *,
    reason: str | None = None,
    document_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> DocumentBatchItem:
    if status is ItemStatus.PENDING:
        raise ValueError("an outcome cannot be pending")
    item = await session.scalar(
        select(DocumentBatchItem).where(DocumentBatchItem.id == item_id).with_for_update()
    )
    if item is None:  # pragma: no cover - the caller created it
        raise NotFoundError("batch item not found")
    if item.status != ItemStatus.PENDING.value:
        return item
    # Read the time first: the query would autoflush a half-changed row.
    now = await session.scalar(select(func.now()))
    item.status = status.value
    item.reason = reason[:64] if reason else None
    item.document_id = document_id
    item.entity_type = entity_type
    item.entity_id = entity_id
    item.completed_at = now
    await session.flush()
    return item


def item_out(item: DocumentBatchItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "position": item.position,
        "source": item.source,
        "status": item.status,
        "reason": item.reason,
        "document_id": str(item.document_id) if item.document_id else None,
        "entity_type": item.entity_type,
        "entity_id": item.entity_id,
    }


async def complete_if_done(session: AsyncSession, batch_id: uuid.UUID) -> bool:
    """Complete the batch if no item is pending. True when this call completed it."""
    batch = await session.scalar(
        select(DocumentBatch).where(DocumentBatch.id == batch_id).with_for_update()
    )
    if batch is None or batch.status == BatchStatus.COMPLETED.value:
        return False
    items = list(
        await session.scalars(
            select(DocumentBatchItem)
            .where(DocumentBatchItem.batch_id == batch_id)
            .order_by(DocumentBatchItem.position)
        )
    )
    if any(i.status == ItemStatus.PENDING.value for i in items):
        return False
    now: datetime | None = await session.scalar(select(func.now()))
    batch.status = BatchStatus.COMPLETED.value
    batch.completed_at = now
    batch.updated_at = now or batch.updated_at
    await session.flush()
    view = BatchView(batch=batch, items=items)
    counts = view.counts()
    await publish_event(
        session,
        BATCH_COMPLETED,
        {
            "batch_id": str(batch.id),
            "workflow": batch.workflow,
            "status": batch.status,
            "metadata": batch.metadata_,
            "counts": counts,
            "items": [item_out(i) for i in items],
        },
    )
    await append_audit(
        session,
        action=BatchAction.COMPLETED,
        actor_type="system",
        actor_id=SYSTEM_ACTOR,
        entity_type=BATCH_ENTITY,
        entity_id=batch.id,
        workflow=batch.workflow,
        payload={"counts": counts},
    )
    return True


async def get_batch(session: AsyncSession, actor: Actor, batch_id: uuid.UUID) -> BatchView:
    batch = await session.scalar(select(DocumentBatch).where(DocumentBatch.id == batch_id))
    by_type, by_id = actor_ref(actor)
    own = batch is not None and (batch.created_by_type, batch.created_by_id) == (
        by_type.value,
        by_id,
    )
    if batch is None or not (own or actor.has("core.document.read")):
        raise NotFoundError("batch not found")
    items = list(
        await session.scalars(
            select(DocumentBatchItem)
            .where(DocumentBatchItem.batch_id == batch_id)
            .order_by(DocumentBatchItem.position)
        )
    )
    return BatchView(batch=batch, items=items)


@dataclass(frozen=True, slots=True)
class SweepResult:
    items: int
    batches: int
    completed: int


async def fail_stale_url_items(
    session: AsyncSession, *, older_than_minutes: int = STALE_AFTER_MINUTES
) -> SweepResult:
    """Fail the URL items of this session's organisation left pending too long.

    Items another transaction holds (a fetch finishing right now) are skipped and
    looked at again on the next run.
    """
    if older_than_minutes < 1:
        raise ValueError("older_than_minutes must be at least 1")
    cutoff = func.now() - timedelta(minutes=older_than_minutes)
    stale = list(
        await session.scalars(
            select(DocumentBatchItem)
            .join(DocumentBatch, DocumentBatch.id == DocumentBatchItem.batch_id)
            .where(
                DocumentBatchItem.status == ItemStatus.PENDING.value,
                DocumentBatchItem.source == "url",
                DocumentBatch.created_at < cutoff,
            )
            .order_by(DocumentBatchItem.batch_id, DocumentBatchItem.position)
            .with_for_update(of=DocumentBatchItem, skip_locked=True)
        )
    )
    by_batch: dict[uuid.UUID, list[DocumentBatchItem]] = {}
    for item in stale:
        by_batch.setdefault(item.batch_id, []).append(item)
    completed = 0
    for batch_id, items in by_batch.items():
        workflow = await session.scalar(
            select(DocumentBatch.workflow).where(DocumentBatch.id == batch_id)
        )
        for item in items:
            await finish_item(session, item.id, ItemStatus.FAILED, reason=TIMED_OUT)
        await append_audit(
            session,
            action=BatchAction.ITEMS_TIMED_OUT,
            actor_type="system",
            actor_id=SWEEPER_ACTOR,
            entity_type=BATCH_ENTITY,
            entity_id=batch_id,
            workflow=workflow,
            payload={
                "items": len(items),
                "positions": [i.position for i in items],
                "reason": TIMED_OUT,
                "older_than_minutes": older_than_minutes,
            },
        )
        if await complete_if_done(session, batch_id):
            completed += 1
    return SweepResult(items=len(stale), batches=len(by_batch), completed=completed)
