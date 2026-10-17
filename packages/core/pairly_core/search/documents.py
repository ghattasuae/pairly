# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Index parsed documents for search (kind ``document``).

Only while the organisation's ``search.documents`` flag is on (default off), and only:

* documents that parsed successfully and do not need review;
* visible body text (``policy.chunking``: hidden text, off-page boxes, headers and
  footers are dropped);
* after PII redaction (``guardrails.pii.PiiRedactor``: emails, phone numbers, IBANs,
  card numbers, Emirates ID and passport numbers become placeholders). The redacted
  text is what is stored and what is sent to the embedding provider; the original text
  stays in the encrypted object store only.

Turning the flag off stops searching the kind at once (``search.sources``) and queues
``purge_document_search`` (``PURGE_JOB``, run by the worker; the flags route purges in
the request when the queue is unavailable), which deletes every document chunk of the
organisation: an organisation that opts out keeps no searchable copy of its documents.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.audit import ActorType, append_audit
from pairly_core.db import TenantError, current_org, set_tenant
from pairly_core.documents.model import NormalizedDocument
from pairly_core.flags.registry import SEARCH_DOCUMENTS
from pairly_core.flags.service import is_enabled
from pairly_core.guardrails.pii import PiiRedactor
from pairly_core.ingestion.models import Document, DocumentStatus
from pairly_core.ingestion.storage import ObjectStore, StorageError
from pairly_core.policy.chunking import chunk_document
from pairly_core.retrieval.embeddings import Embedder, EmbeddingUnavailable
from pairly_core.retrieval.models import SearchChunk
from pairly_core.retrieval.search import ChunkInput, SearchableKind, index_chunks

DOCUMENT_KIND: Final = SearchableKind("document", "Parsed documents (PII redacted)")
PURGE_JOB: Final = "purge_document_search"
PURGED_ACTION: Final = "core.search.documents_purged"
MAX_NORMALIZED_BYTES: Final = 64 * 1024 * 1024
# A very long document is indexed up to this many chunks (about 240 pages of text).
MAX_DOCUMENT_CHUNKS: Final = 200

_redactor = PiiRedactor()


@dataclass(frozen=True, slots=True)
class DocumentIndexOutcome:
    # indexed | skipped (flag off, not parsed, needs review) | failed
    status: str
    reason: str | None = None
    chunks: int = 0


def redact_chunks(chunks: list[ChunkInput]) -> list[ChunkInput]:
    """The chunks with PII replaced by placeholders (empty results dropped)."""
    out: list[ChunkInput] = []
    for chunk in chunks:
        text = _redactor.redact_text(chunk.text).strip()
        if text:
            out.append(
                ChunkInput(
                    text=text,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    section=_redactor.redact_text(chunk.section) if chunk.section else None,
                    meta=dict(chunk.meta),
                )
            )
    return out


async def index_document(
    session: AsyncSession, store: ObjectStore, *, document_id: uuid.UUID, embedder: Embedder
) -> DocumentIndexOutcome:
    """Index one parsed document of the session's organisation (see the module doc)."""
    if current_org(session) is None:
        raise TenantError("document indexing needs a tenant context")
    if not await is_enabled(session, SEARCH_DOCUMENTS):
        return DocumentIndexOutcome("skipped", "flag_off")
    document = await session.scalar(select(Document).where(Document.id == document_id))
    if document is None:
        return DocumentIndexOutcome("skipped", "not_found")
    if document.status != DocumentStatus.PARSED.value or not document.parse_result_key:
        return DocumentIndexOutcome("skipped", "not_parsed")
    if document.needs_review:
        return DocumentIndexOutcome("skipped", "needs_review")
    try:
        raw = await store.get_bytes(document.parse_result_key, max_bytes=MAX_NORMALIZED_BYTES)
        normalized = NormalizedDocument.model_validate_json(raw)
    except (StorageError, ValidationError, ValueError):
        return DocumentIndexOutcome("failed", "parse_result_unavailable")
    chunks = redact_chunks(chunk_document(normalized))[:MAX_DOCUMENT_CHUNKS]
    for chunk in chunks:
        chunk.meta.update({"workflow": document.workflow})
    try:
        count = await index_chunks(
            session,
            kind=DOCUMENT_KIND,
            source_id=document.id,
            source_version=1,
            chunks=chunks,
            embedder=embedder,
        )
    except EmbeddingUnavailable:
        return DocumentIndexOutcome("failed", "embedding_unavailable")
    return DocumentIndexOutcome("indexed", chunks=count)


async def index_document_after_parse(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    embedder: Embedder,
) -> DocumentIndexOutcome:
    """Worker hook after a parse: index the document if its organisation opted in."""
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            return await index_document(session, store, document_id=document_id, embedder=embedder)


async def remove_document(session: AsyncSession, document_id: uuid.UUID) -> int:
    """Drop a document's chunks from the index. Returns how many were removed."""
    org = current_org(session)
    if org is None:
        raise TenantError("document indexing needs a tenant context")
    result = await session.execute(
        delete(SearchChunk).where(
            SearchChunk.org_id == org,
            SearchChunk.kind == DOCUMENT_KIND.key,
            SearchChunk.source_id == document_id,
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def purge_documents(
    session: AsyncSession, *, actor_type: ActorType, actor_id: str
) -> int | None:
    """Delete every document chunk of the session's organisation, if the flag is off.

    Returns how many were deleted (audited when any were), or None when the flag is on
    again (someone turned it back on before the purge ran: nothing is deleted).
    """
    org = current_org(session)
    if org is None:
        raise TenantError("document indexing needs a tenant context")
    if await is_enabled(session, SEARCH_DOCUMENTS):
        return None
    result = await session.execute(
        delete(SearchChunk).where(SearchChunk.org_id == org, SearchChunk.kind == DOCUMENT_KIND.key)
    )
    count = int(getattr(result, "rowcount", 0) or 0)
    if count:
        await append_audit(
            session,
            action=PURGED_ACTION,
            actor_type=actor_type,
            actor_id=actor_id,
            entity_type="search_index",
            entity_id=DOCUMENT_KIND.key,
            payload={"chunks": count, "reason": "flag_off"},
        )
    return count
