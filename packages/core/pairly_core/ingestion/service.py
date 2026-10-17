# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The ingestion path shared by uploads, the API and inbound email.

``spool`` receives bytes into a private temp file, computing SHA-256 and the size while
they arrive and stopping as soon as the limit is passed. ``ingest_spooled`` then sniffs
the type, applies the intake policy, dedupes, stores the bytes and inserts the
``documents`` row, all inside the caller's tenant-bound transaction, with an audit
event for every outcome (received, duplicate, rejected).

The bytes are stored before the row is inserted, so a row never points at a missing
object. If the transaction then rolls back, an unreferenced object may remain; it is
content addressed (a later identical upload reuses it) and a cleanup job can remove it.
TODO(WP1.12): orphan object cleanup job.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import structlog
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, actor_ref, append_audit
from pairly_core.db import TenantError, current_org, current_portal_subject
from pairly_core.errors import ConflictError
from pairly_core.ingestion.models import Document, DocumentSource, DocumentStatus
from pairly_core.ingestion.settings import IntakePolicy
from pairly_core.ingestion.sniff import SNIFF_BYTES, ZIP, active_content, sniff
from pairly_core.ingestion.storage import ObjectStore, blob_key
from pairly_core.rbac import Actor
from pairly_core.webhooks.events import DOCUMENT_RECEIVED, publish_event

log = structlog.get_logger(__name__)

CHUNK_BYTES: Final = 256 * 1024
MAX_FILENAME_BYTES: Final = 255


class DocumentAction:
    """Audit actions written by ingestion, scanning and parsing."""

    RECEIVED: Final = "core.document.received"
    DUPLICATE: Final = "core.document.duplicate"
    REJECTED: Final = "core.document.rejected"
    SCAN_CLEAN: Final = "core.document.scan_clean"
    SCAN_INFECTED: Final = "core.document.scan_infected"
    SCAN_FAILED: Final = "core.document.scan_failed"
    PARSED: Final = "core.document.parsed"
    PARSE_FAILED: Final = "core.document.parse_failed"
    CONTENT_URL_ISSUED: Final = "core.document.content_url_issued"
    EMAIL_RECEIVED: Final = "core.email.received"
    EMAIL_DUPLICATE: Final = "core.email.duplicate"


class RejectReason(StrEnum):
    TOO_LARGE = "too_large"
    EMPTY = "empty"
    TYPE_NOT_ALLOWED = "type_not_allowed"
    # A Word document with macros, ActiveX or a relationship outside the package (a
    # remote attached template, an external image or object): ``sniff.active_content``.
    ACTIVE_CONTENT = "active_content"
    TOO_MANY_ATTACHMENTS = "too_many_attachments"


class TooLargeError(Exception):
    """The incoming bytes passed the size limit; reading stopped early."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"more than {limit} bytes")
        self.limit = limit


@dataclass(frozen=True, slots=True)
class AuditBy:
    """Who an ingestion audit event is attributed to."""

    actor_type: ActorType
    actor_id: str

    @classmethod
    def from_actor(cls, actor: Actor) -> AuditBy:
        kind, actor_id = actor_ref(actor)
        return cls(kind, actor_id)

    @classmethod
    def system(cls, name: str) -> AuditBy:
        return cls(ActorType.SYSTEM, name)


@dataclass(frozen=True, slots=True)
class SpooledFile:
    path: Path
    size: int
    sha256: str
    head: bytes


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """``created``, ``duplicate`` (``document`` is the existing one) or ``rejected``."""

    kind: str
    document: Document | None = None
    reason: RejectReason | None = None
    media_type: str | None = None

    @property
    def created(self) -> bool:
        return self.kind == "created"


# ------------------------------------------------------------------ file names

# Bidirectional overrides and isolates can make "invoice_exe.pdf" display reversed.
_BIDI_CONTROLS = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_SPACES = re.compile(r"\s+")


def sanitize_filename(name: str | None) -> str | None:
    """A display-only name: last path segment, no controls, no bidi tricks, 255 bytes max.

    The result is never used for a path or a storage key; it is shown to people and put
    in the download ``Content-Disposition``.
    """
    if not name:
        return None
    value = unicodedata.normalize("NFKC", name)
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = _BIDI_CONTROLS.sub("", value)
    value = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C")
    value = _SPACES.sub(" ", value).strip().lstrip(".").strip()
    if not value:
        return None
    if len(value.encode("utf-8")) > MAX_FILENAME_BYTES:
        stem, dot, ext = value.rpartition(".")
        ext = f".{ext}" if dot and 0 < len(ext) <= 10 else ""
        stem = stem if ext else value
        budget = MAX_FILENAME_BYTES - len(ext.encode("utf-8"))
        stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip()
        value = stem + ext
    return value or None


# ------------------------------------------------------------------ spooling


class SpoolWriter:
    """Write bytes to a new private file while hashing and counting them.

    ``write`` raises ``TooLargeError`` as soon as more than ``max_bytes`` arrive; call
    ``abort`` (or use ``spool``) to remove the partial file. The file has mode 0600.
    """

    def __init__(self, directory: Path, *, max_bytes: int) -> None:
        self.path = directory / f"upload-{uuid.uuid4().hex}"
        self.max_bytes = max_bytes
        self.size = 0
        self._head = bytearray()
        self._digest = hashlib.sha256()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._file = os.fdopen(fd, "wb")

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.size += len(chunk)
        if self.size > self.max_bytes:
            raise TooLargeError(self.max_bytes)
        if len(self._head) < SNIFF_BYTES:
            self._head += chunk[: SNIFF_BYTES - len(self._head)]
        self._digest.update(chunk)
        self._file.write(chunk)

    def finish(self) -> SpooledFile:
        self._file.close()
        return SpooledFile(
            path=self.path, size=self.size, sha256=self._digest.hexdigest(), head=bytes(self._head)
        )

    def abort(self) -> None:
        self._file.close()
        self.path.unlink(missing_ok=True)


async def spool(chunks: AsyncIterable[bytes], directory: Path, *, max_bytes: int) -> SpooledFile:
    """Spool ``chunks`` into ``directory`` (see ``SpoolWriter``)."""
    writer = SpoolWriter(directory, max_bytes=max_bytes)
    try:
        async for chunk in chunks:
            writer.write(chunk)
    except BaseException:
        writer.abort()
        raise
    return writer.finish()


async def _iterate(data: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(data), CHUNK_BYTES):
        yield data[start : start + CHUNK_BYTES]


async def spool_bytes(data: bytes, directory: Path, *, max_bytes: int) -> SpooledFile:
    return await spool(_iterate(data), directory, max_bytes=max_bytes)


# ------------------------------------------------------------------ ingest


async def ingest_spooled(
    session: AsyncSession,
    store: ObjectStore,
    *,
    spooled: SpooledFile,
    workflow: str,
    source: DocumentSource,
    filename: str | None,
    policy: IntakePolicy,
    by: AuditBy,
    submitted_by: uuid.UUID | None = None,
    inbound_email_id: uuid.UUID | None = None,
    media_type: str | None = None,
    dedupe: bool = True,
) -> IngestOutcome:
    """Check, dedupe, store and record one spooled file in the session's organisation.

    ``media_type`` overrides sniffing only for content Pairly produced itself (an email
    body); never pass a client-declared type.

    ``dedupe=False``: the file always gets its own row, even when the workflow already
    holds the same bytes, and no later upload ever links to it (``Document.dedupe``).
    For a submission that belongs to one person, such as a candidate's CV: linking it
    to another candidate's row would show them that row's file name and metadata, and
    tie the two together for erasure (security review 2026-10, L-09). The stored bytes
    are shared all the same.
    """
    org = current_org(session)
    if org is None:
        raise TenantError("ingestion needs a tenant context")
    display_name = sanitize_filename(filename)
    sniffed = media_type or sniff(spooled.head, spooled.path)

    reason: RejectReason | None = None
    if spooled.size == 0:
        reason = RejectReason.EMPTY
    elif spooled.size > policy.max_bytes:
        reason = RejectReason.TOO_LARGE
    elif sniffed not in policy.allowed_types:
        reason = RejectReason.TYPE_NOT_ALLOWED
    # A would-be DOCX refused for what it carries says why (fail closed: it is never a
    # DOCX, whatever the policy allows).
    active: str | None = None
    if reason in {None, RejectReason.TYPE_NOT_ALLOWED} and sniffed == ZIP and media_type is None:
        active = active_content(spooled.path)
    if active is not None:
        reason = RejectReason.ACTIVE_CONTENT
    if reason is not None:
        await record_rejection(
            session,
            by=by,
            workflow=workflow,
            source=source,
            reason=reason,
            media_type=sniffed,
            size=spooled.size,
            sha256=spooled.sha256,
            detail=active,
        )
        return IngestOutcome("rejected", reason=reason, media_type=sniffed)

    existing = await _existing(session, workflow, spooled.sha256) if dedupe else None
    if existing is not None:
        await _audit(session, by, DocumentAction.DUPLICATE, existing, {"source": source.value})
        return IngestOutcome("duplicate", document=existing, media_type=existing.media_type)

    key = blob_key(org, spooled.sha256)
    if not await store.exists(key):
        await store.put_file(key, spooled.path, content_type=sniffed, sha256=spooled.sha256)

    doc_id = uuid.uuid4()
    inserted = await session.scalar(
        insert(Document)
        .values(
            id=doc_id,
            org_id=org,
            workflow=workflow,
            source=source.value,
            original_filename=display_name,
            media_type=sniffed,
            size_bytes=spooled.size,
            sha256=spooled.sha256,
            storage_key=key,
            status=DocumentStatus.RECEIVED.value,
            submitted_by=submitted_by,
            inbound_email_id=inbound_email_id,
            dedupe=dedupe,
        )
        .on_conflict_do_nothing(
            index_elements=["org_id", "workflow", "sha256"], index_where=text("dedupe")
        )
        .returning(Document.id)
    )
    if inserted is None:
        # Lost a race with a concurrent upload of the same bytes to this workflow.
        existing = await _existing(session, workflow, spooled.sha256)
        if existing is None and current_portal_subject(session) is not None:
            # The same bytes are someone else's file, which a portal session never sees
            # (portal guard, migration 0022): answer as a duplicate, naming nothing.
            raise ConflictError("this file was already received")
        if existing is None:  # pragma: no cover - the conflicting row must be visible
            raise RuntimeError("duplicate document vanished")
        await _audit(session, by, DocumentAction.DUPLICATE, existing, {"source": source.value})
        return IngestOutcome("duplicate", document=existing, media_type=existing.media_type)

    document = await session.get_one(Document, doc_id)
    await _audit(
        session,
        by,
        DocumentAction.RECEIVED,
        document,
        {
            "source": source.value,
            "media_type": sniffed,
            "size_bytes": spooled.size,
            "sha256": spooled.sha256,
        },
    )
    # Outbound webhook (transactional outbox): emitted only if the document commits.
    await publish_event(
        session,
        DOCUMENT_RECEIVED,
        {
            "document_id": str(document.id),
            "workflow": workflow,
            "source": source.value,
            "media_type": sniffed,
            "size_bytes": spooled.size,
            "sha256": spooled.sha256,
        },
    )
    log.info(
        "document_received",
        document_id=str(document.id),
        workflow=workflow,
        source=source.value,
        media_type=sniffed,
        size=spooled.size,
    )
    return IngestOutcome("created", document=document, media_type=sniffed)


async def _existing(session: AsyncSession, workflow: str, sha256: str) -> Document | None:
    """The document with these bytes in this workflow that takes part in dedupe (dedupe
    never crosses workflows, and never reaches a row kept for one submission)."""
    result = await session.scalar(
        select(Document)
        .where(Document.workflow == workflow, Document.sha256 == sha256, Document.dedupe.is_(True))
        .execution_options(populate_existing=True)
    )
    return result


async def _audit(
    session: AsyncSession,
    by: AuditBy,
    action: str,
    document: Document,
    payload: dict[str, Any],
) -> None:
    await append_audit(
        session,
        action=action,
        actor_type=by.actor_type,
        actor_id=by.actor_id,
        entity_type="document",
        entity_id=document.id,
        workflow=document.workflow,
        payload=payload,
    )


async def record_rejection(
    session: AsyncSession,
    *,
    by: AuditBy,
    workflow: str,
    source: DocumentSource,
    reason: RejectReason,
    media_type: str | None = None,
    size: int | None = None,
    sha256: str | None = None,
    detail: str | None = None,
) -> None:
    """Audit an intake refusal. No row and no stored bytes exist for it. ``detail``
    names what made a document active content (``sniff.active_content``)."""
    payload: dict[str, Any] = {"source": source.value, "reason": reason.value}
    if detail is not None:
        payload["detail"] = detail
    if media_type is not None:
        payload["media_type"] = media_type
    if size is not None:
        payload["size_bytes"] = size
    if sha256 is not None:
        payload["sha256"] = sha256
    await append_audit(
        session,
        action=DocumentAction.REJECTED,
        actor_type=by.actor_type,
        actor_id=by.actor_id,
        entity_type="document",
        workflow=workflow,
        payload=payload,
    )
    log.info("document_rejected", workflow=workflow, source=source.value, reason=reason.value)
