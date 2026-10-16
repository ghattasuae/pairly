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
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
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


class BatchAction:
    CREATED: Final = "core.batch.created"
    COMPLETED: Final = "core.batch.completed"


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
