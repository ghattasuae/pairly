# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology

"""The pre-employment checklist (brief 3.8.4).

* ``config_for`` / ``save_config``: the documents to collect for a job and the retention
  (days after hire, 1 to 365, default 90). Without a saved list: ``DEFAULT_ITEMS``.
* ``request_checklist``: one item per document for an application, each requested
  through the candidate portal's file request flow (a ``hire_document_requests`` row
  pointing at the item). Requesting again adds only what is missing.
* ``store_file``: the candidate's upload for an item. Only PDF, JPEG and PNG, at most
  ``MAX_BYTES``. The bytes are encrypted with the envelope scheme (bound to the
  organisation, this table and the item) and stored under their own object key; the
  file name is encrypted in the row. Never parsed, indexed or shown to a model.
* ``read_file``: decrypts a file for HR or an admin (the API checks); audited.
* ``review``: verified or waived, by HR or an admin, audited.
* Retention: ``set_retention`` (the ``hire_checklist_retention`` job) sets
  ``retention_until`` = hired at + the item's retention days, once the candidate is
  hired. The ``hire.checklist.documents`` retention rule counts files past it, and
  ``purge_due`` removes them (object deleted, item ``purged_at``), audited.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.envelope import Aad, decrypt, encrypt
from pairly_core.crypto.types import seal
from pairly_core.db import set_tenant
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.ingestion.storage import ObjectStore, StorageError, sealed_key
from pairly_core.jobs.registry import JobContext
from pairly_hire.models import HireJob
from pairly_hire.onboarding.models import (
    DEFAULT_RETENTION_DAYS,
    ITEMS,
    ChecklistStatus,
    HireChecklistConfig,
    HireChecklistItem,
)
from pairly_hire.pipeline.models import HirePipelineEntry
from pairly_hire.pipeline.records import load_record
from pairly_hire.pipeline.stages import HIRED
from pairly_hire.portal.models import DocumentRequestStatus, HireDocumentRequest
from pairly_hire.sealing import read_sealed

log = structlog.get_logger("pairly_hire.onboarding")

WORKFLOW: Final = "hire"
ENTITY: Final = "hire.checklist_item"
JOB_NAME: Final = "hire_checklist_retention"
JOB_ACTOR: Final = "pairly.hire.checklist_retention"
MAX_BYTES: Final = 10 * 1024 * 1024
MAX_ITEMS: Final = 12
ALLOWED_TYPES: Final = {
    b"%PDF-": "application/pdf",
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
}
DEFAULT_ITEMS: Final[tuple[dict[str, str], ...]] = (
    {
        "key": "id_document",
        "label": "Passport or Emirates ID",
        "label_ar": "جواز السفر أو الهوية الإماراتية",
    },
    {"key": "degree", "label": "Degree certificate", "label_ar": "شهادة المؤهل العلمي"},
    {
        "key": "visa",
        "label": "Visa page or residence permit",
        "label_ar": "صفحة التأشيرة أو الإقامة",
    },
)


def _audit_payload(item: HireChecklistItem, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "application_id": str(item.application_id),
        "key": item.key,
        "status": item.status,
    } | (extra or {})


async def _audit(
    session: AsyncSession,
    item: HireChecklistItem,
    action: str,
    *,
    actor_type: ActorType,
    actor_id: str,
    extra: dict[str, Any] | None = None,
) -> None:
    await append_audit(
        session,
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type=ENTITY,
        entity_id=item.id,
        workflow=WORKFLOW,
        payload=_audit_payload(item, extra),
    )


# ------------------------------------------------------------------ configuration


@dataclass(frozen=True, slots=True)
class ChecklistConfig:
    items: list[dict[str, str]]
    retention_days: int
    saved: bool


def clean_items(raw: list[Any]) -> list[dict[str, str]]:
    if not 1 <= len(raw) <= MAX_ITEMS:
        raise BadRequestError(f"list 1 to {MAX_ITEMS} documents")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise BadRequestError("each document needs a key and a label")
        key = str(item.get("key") or "").strip()
        label = " ".join(str(item.get("label") or "").split())
        label_ar = " ".join(str(item.get("label_ar") or "").split())
        if (
            not key
            or not key[0].isalpha()
            or not all(c.islower() or c.isdigit() or c == "_" for c in key)
            or len(key) > 32
            or not key.isascii()
        ):
            raise BadRequestError("a document key is lower case letters, digits and _")
        if key in seen:
            raise BadRequestError("document keys must be unique")
        if not 2 <= len(label) <= 120 or len(label_ar) > 120:
            raise BadRequestError("a document label has 2 to 120 characters")
        seen.add(key)
        out.append({"key": key, "label": label, "label_ar": label_ar})
    return out


async def config_for(session: AsyncSession, job_id: uuid.UUID) -> ChecklistConfig:
    row = await session.scalar(
        select(HireChecklistConfig).where(HireChecklistConfig.job_id == job_id)
    )
    if row is None:
        return ChecklistConfig([dict(i) for i in DEFAULT_ITEMS], DEFAULT_RETENTION_DAYS, False)
    return ChecklistConfig([dict(i) for i in row.items], int(row.retention_days), True)


async def save_config(
    session: AsyncSession,
    job: HireJob,
    actor_id: uuid.UUID,
    *,
    items: list[Any],
    retention_days: int,
) -> ChecklistConfig:
    clean = clean_items(items)
    if not 1 <= retention_days <= 365:
        raise BadRequestError("keep files 1 to 365 days after hire")
    row = await session.scalar(
        select(HireChecklistConfig).where(HireChecklistConfig.job_id == job.id).with_for_update()
    )
    now = datetime.now(UTC)
    if row is None:
        row = HireChecklistConfig(
            id=uuid.uuid4(),
            org_id=job.org_id,
            job_id=job.id,
            items=clean,
            retention_days=retention_days,
            updated_by=actor_id,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.items = clean
        row.retention_days = retention_days
        row.updated_by = actor_id
        row.updated_at = now
    await session.flush()
    await append_audit(
        session,
        action="hire.checklist.configured",
        actor_type=ActorType.USER,
        actor_id=str(actor_id),
        entity_type="hire.job",
        entity_id=job.id,
        workflow=WORKFLOW,
        payload={"keys": [i["key"] for i in clean], "retention_days": retention_days},
    )
    return ChecklistConfig(clean, retention_days, True)


# ------------------------------------------------------------------ items


async def items_for(session: AsyncSession, application_id: uuid.UUID) -> list[HireChecklistItem]:
    return list(
        await session.scalars(
            select(HireChecklistItem)
            .where(HireChecklistItem.application_id == application_id)
            .order_by(HireChecklistItem.created_at, HireChecklistItem.key)
        )
    )


async def get_item(
    session: AsyncSession, item_id: uuid.UUID, *, lock: bool = False
) -> HireChecklistItem:
    query = select(HireChecklistItem).where(HireChecklistItem.id == item_id)
    if lock:
        query = query.with_for_update()
    row = await session.scalar(query)
    if row is None:
        raise NotFoundError("checklist item not found")
    return row


async def request_checklist(
    session: AsyncSession, actor_id: uuid.UUID, application_id: uuid.UUID
) -> list[HireChecklistItem]:
    """Ask the candidate, through the portal, for every configured document they have
    not been asked for yet."""
    record = await load_record(session, application_id, lock=True)
    if record.candidate.erased_at is not None:
        raise ConflictError("this candidate's data was erased")
    config = await config_for(session, record.job.id)
    existing = {i.key for i in await items_for(session, application_id)}
    now = datetime.now(UTC)
    added: list[HireChecklistItem] = []
    for n, spec in enumerate(config.items):
        if spec["key"] in existing:
            continue
        item = HireChecklistItem(
            id=uuid.uuid4(),
            org_id=record.app.org_id,
            application_id=record.app.id,
            candidate_id=record.candidate.id,
            job_id=record.job.id,
            key=spec["key"],
            label=spec["label"],
            label_ar=spec.get("label_ar", ""),
            status=ChecklistStatus.REQUESTED.value,
            retention_days=config.retention_days,
            requested_by=actor_id,
            created_at=now + timedelta(microseconds=n),
            updated_at=now,
        )
        session.add(item)
        await session.flush()
        request = HireDocumentRequest(
            id=uuid.uuid4(),
            org_id=record.app.org_id,
            application_id=record.app.id,
            candidate_id=record.candidate.id,
            label=spec["label"],
            label_ar=spec.get("label_ar", ""),
            status=DocumentRequestStatus.OPEN.value,
            requested_by=actor_id,
            checklist_item_id=item.id,
        )
        session.add(request)
        await session.flush()
        item.document_request_id = request.id
        await _audit(
            session,
            item,
            "hire.checklist.requested",
            actor_type=ActorType.USER,
            actor_id=str(actor_id),
            extra={"document_request_id": str(request.id)},
        )
        added.append(item)
    await session.flush()
    return added


def _aad(item: HireChecklistItem) -> Aad:
    return Aad.for_row(item.org_id, ITEMS, "file", item.id)


def sniff_type(head: bytes) -> str | None:
    for magic, media in ALLOWED_TYPES.items():
        if head.startswith(magic):
            return media
    return None


async def store_file(
    session: AsyncSession,
    store: ObjectStore,
    item: HireChecklistItem,
    *,
    data: bytes,
    filename: str | None,
    actor_type: ActorType,
    actor_id: str,
) -> HireChecklistItem:
    """Encrypt and store the candidate's file for ``item`` (a new upload replaces one
    not yet verified)."""
    if item.status not in (ChecklistStatus.REQUESTED.value, ChecklistStatus.RECEIVED.value):
        raise ConflictError("this document was already reviewed")
    if item.purged_at is not None:
        raise ConflictError("this document is no longer kept")
    if not data:
        raise BadRequestError("the file is empty")
    if len(data) > MAX_BYTES:
        raise BadRequestError("the file is too large (10 MB at most)")
    media = sniff_type(data[:16])
    if media is None:
        raise BadRequestError("send a PDF, JPEG or PNG file")
    token = encrypt(data, _aad(item))
    key = sealed_key(item.org_id, uuid.uuid4())
    await store.put_bytes(key, token.encode("ascii"), content_type="application/octet-stream")
    old = item.object_key
    now = datetime.now(UTC)
    item.object_key = key
    item.content_type = media
    item.size_bytes = len(data)
    item.sha256 = hashlib.sha256(data).hexdigest()
    name = " ".join((filename or "").split())[:200] or None
    item.file_name = (
        seal(HireChecklistItem, "file_name", name, org_id=item.org_id, row_id=item.id)  # type: ignore[assignment]
        if name
        else None
    )
    item.status = ChecklistStatus.RECEIVED.value
    item.received_at = now
    item.updated_at = now
    await session.flush()
    await _audit(
        session,
        item,
        "hire.checklist.received",
        actor_type=actor_type,
        actor_id=actor_id,
        extra={"size_bytes": len(data), "content_type": media, "sha256": item.sha256},
    )
    if old and old != key:
        try:
            await store.delete(old)
        except StorageError:
            log.warning("checklist_old_file_not_deleted", item_id=str(item.id))
    return item


@dataclass(frozen=True, slots=True)
class ChecklistFile:
    data: bytes
    content_type: str
    file_name: str


async def read_file(
    session: AsyncSession, store: ObjectStore, item: HireChecklistItem, *, actor_id: uuid.UUID
) -> ChecklistFile:
    if item.object_key is None or item.purged_at is not None:
        raise NotFoundError("no file for this document")
    raw = await store.get_bytes(item.object_key, max_bytes=MAX_BYTES * 2)
    data = decrypt(raw.decode("ascii"), _aad(item))
    await _audit(
        session,
        item,
        "hire.checklist.file_opened",
        actor_type=ActorType.USER,
        actor_id=str(actor_id),
    )
    name = read_sealed(item, "file_name") or f"{item.key}"
    return ChecklistFile(data, item.content_type or "application/octet-stream", str(name))


async def review(
    session: AsyncSession, item: HireChecklistItem, actor_id: uuid.UUID, *, outcome: str
) -> HireChecklistItem:
    if outcome == ChecklistStatus.VERIFIED.value:
        if item.status != ChecklistStatus.RECEIVED.value:
            raise ConflictError("only a received document can be verified")
    elif outcome == ChecklistStatus.WAIVED.value:
        if item.status not in (ChecklistStatus.REQUESTED.value, ChecklistStatus.RECEIVED.value):
            raise ConflictError("this document was already reviewed")
    else:
        raise BadRequestError("verify or waive")
    now = datetime.now(UTC)
    item.status = outcome
    item.reviewed_by = actor_id
    item.reviewed_at = now
    item.updated_at = now
    if outcome == ChecklistStatus.WAIVED.value and item.document_request_id is not None:
        request = await session.scalar(
            select(HireDocumentRequest)
            .where(HireDocumentRequest.id == item.document_request_id)
            .with_for_update()
        )
        if request is not None and request.status == DocumentRequestStatus.OPEN.value:
            request.status = DocumentRequestStatus.CANCELLED.value
    await session.flush()
    await _audit(
        session,
        item,
        f"hire.checklist.{outcome}",
        actor_type=ActorType.USER,
        actor_id=str(actor_id),
    )
    return item


# ------------------------------------------------------------------ retention


async def set_retention(session: AsyncSession, now: datetime | None = None) -> int:
    """``retention_until`` for the items of hired candidates that have none yet."""
    now = now or datetime.now(UTC)
    rows = (
        await session.execute(
            select(HireChecklistItem, HirePipelineEntry.stage_entered_at)
            .join(
                HirePipelineEntry,
                HirePipelineEntry.application_id == HireChecklistItem.application_id,
            )
            .where(
                HireChecklistItem.retention_until.is_(None),
                HirePipelineEntry.stage_key == HIRED,
            )
            .with_for_update(of=HireChecklistItem)
        )
    ).all()
    for item, hired_at in rows:
        item.retention_until = hired_at + timedelta(days=int(item.retention_days))
        item.updated_at = now
    await session.flush()
    return len(rows)


async def purge_due(
    session: AsyncSession, store: ObjectStore | None, now: datetime | None = None
) -> int:
    """Remove the files past their retention (object first, then the row's pointer)."""
    now = now or datetime.now(UTC)
    if store is None:
        return 0
    count = 0
    for item in await session.scalars(
        select(HireChecklistItem)
        .where(
            HireChecklistItem.retention_until.is_not(None),
            HireChecklistItem.retention_until < now,
            HireChecklistItem.purged_at.is_(None),
        )
        .with_for_update()
    ):
        if item.object_key is not None:
            try:
                await store.delete(item.object_key)
            except StorageError:
                log.warning("checklist_purge_failed", item_id=str(item.id))
                continue
        item.object_key = None
        item.file_name = None
        item.purged_at = now
        item.updated_at = now
        count += 1
        await _audit(
            session, item, "hire.checklist.purged", actor_type=ActorType.SYSTEM, actor_id=JOB_ACTOR
        )
    await session.flush()
    return count


async def run_retention_job(ctx: JobContext) -> dict[str, Any]:
    """``hire_checklist_retention``: hourly, per organisation with applicants."""
    from pairly_hire.pipeline.rediscovery import org_ids

    store = ctx.runtime.services.store
    totals: dict[str, Any] = {"orgs": 0, "scheduled": 0, "purged": 0, "failed": 0}
    for org_id in await org_ids(ctx):
        session = ctx.sessionmaker()
        set_tenant(session, org_id, None)
        try:
            async with session, session.begin():
                totals["scheduled"] += await set_retention(session)
                totals["purged"] += await purge_due(session, store)
        except Exception as exc:
            log.warning("checklist_retention_failed", org_id=str(org_id), error=type(exc).__name__)
            totals["failed"] += 1
            continue
        totals["orgs"] += 1
    return totals
