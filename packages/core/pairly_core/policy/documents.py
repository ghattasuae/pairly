# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Tenant policy documents for RAG: register, index, archive, pick the active versions.

A policy document is a file ingested like any other document (workflow ``policy``:
hashed, virus scanned, parsed in the sandbox) plus a ``policy_documents`` row naming it
(``name``, for example ``spend_policy``) and numbering it (``version``, counting up per
name). Uploading a new file under the same name creates the next version; nothing is
edited in place.

Indexing runs once the document is parsed: the guardrails scan the normalized document
first (``pairly_core.policy.guard``), every removed or flagged span is cut out, the rest
is chunked by headings and paragraphs (``pairly_core.policy.chunking``), embedded and
stored in ``search_chunks`` (kind ``policy``), with page and section on every chunk. The
findings are stored on the policy document as warnings for admins. A high-severity
instruction finding (or a guardrail detector that failed) sets ``needs_review``: nothing
is indexed and the version is not active. An admin either uploads a corrected file, or
overrides the guardrails with a written reason (``request_override``; a second admin
must approve when a finding is high severity). An overridden version indexes with every
flagged span still cut out.

Fail closed: a document that failed its scan or parse, or that the parser flagged for
review, is never indexed (status ``failed`` with the reason). Policy checks only use
the newest ``indexed`` version of each name; ``archived`` versions are never used.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.audit import ActorType, append_audit
from pairly_core.db import TenantError, current_org, set_tenant
from pairly_core.documents.model import NormalizedDocument
from pairly_core.errors import BadRequestError, ConflictError
from pairly_core.ingestion.models import Document, DocumentStatus
from pairly_core.ingestion.settings import IngestionSettings, IntakePolicy
from pairly_core.ingestion.sniff import DOCX, PDF, TEXT
from pairly_core.ingestion.storage import ObjectStore, StorageError
from pairly_core.policy.chunking import chunk_document
from pairly_core.policy.guard import NEEDS_REVIEW_ERROR, guard_policy_document
from pairly_core.policy.models import PolicyDocument, PolicyDocumentStatus
from pairly_core.policy_versions import PolicyError
from pairly_core.retrieval.embeddings import Embedder, EmbeddingUnavailable
from pairly_core.retrieval.search import SearchableKind, index_chunks

POLICY_WORKFLOW: Final = "policy"
POLICY_KIND: Final = SearchableKind("policy", "Tenant policy documents")
MAX_NORMALIZED_BYTES: Final = 64 * 1024 * 1024
MAX_CHUNKS: Final = 5000


class PolicyDocumentAction:
    REGISTERED: Final = "core.policy.document_registered"
    INDEXED: Final = "core.policy.document_indexed"
    INDEX_FAILED: Final = "core.policy.document_index_failed"
    NEEDS_REVIEW: Final = "core.policy.document_needs_review"
    ARCHIVED: Final = "core.policy.document_archived"
    OVERRIDE_REQUESTED: Final = "core.policy.document_override_requested"
    OVERRIDDEN: Final = "core.policy.document_overridden"


class PolicyDocumentNotFoundError(PolicyError):
    pass


class OverrideNotAllowedError(ConflictError):
    code = "override_not_allowed"
    title = "This policy document cannot be overridden"


class SecondAdminRequiredError(ConflictError):
    code = "second_admin_required"
    title = "A second admin must approve this override"


MIN_OVERRIDE_REASON: Final = 10
MAX_OVERRIDE_REASON: Final = 2000


POLICY_MEDIA_TYPES: Final = frozenset({PDF, DOCX, TEXT})


def policy_intake(settings: IngestionSettings) -> IntakePolicy:
    """The ``policy`` workflow's intake policy, limited to PDF, DOCX and plain text."""
    base = settings.policy_for(POLICY_WORKFLOW)
    allowed = base.allowed_types & POLICY_MEDIA_TYPES or frozenset({PDF, DOCX})
    return IntakePolicy(allowed_types=allowed, max_bytes=base.max_bytes)


@dataclass(frozen=True, slots=True)
class IndexOutcome:
    status: str  # indexed | failed | needs_review | pending
    reason: str | None = None
    chunks: int = 0
    warnings: int = 0


async def register_policy_document(
    session: AsyncSession,
    *,
    name: str,
    title: str,
    document: Document,
    actor_type: ActorType,
    actor_id: str,
) -> PolicyDocument:
    """Record ``document`` as the next version of policy ``name`` (status ``pending``).

    Registering the same document again under the same name returns the existing row.
    """
    org = current_org(session)
    if org is None:
        raise TenantError("policy documents need a tenant context")
    if document.workflow != POLICY_WORKFLOW:
        raise PolicyError("the document was not uploaded as a policy document")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"pairly.policy_document:{org}:{name}"},
    )
    existing = await session.scalar(
        select(PolicyDocument).where(
            PolicyDocument.name == name, PolicyDocument.document_id == document.id
        )
    )
    if existing is not None:
        return existing
    latest = await session.scalar(
        select(func.max(PolicyDocument.version)).where(PolicyDocument.name == name)
    )
    row = PolicyDocument(
        org_id=org,
        name=name,
        version=(latest or 0) + 1,
        title=title,
        document_id=document.id,
        status=PolicyDocumentStatus.PENDING.value,
        created_by_type=actor_type.value,
        created_by_id=actor_id,
    )
    session.add(row)
    await session.flush()
    await append_audit(
        session,
        action=PolicyDocumentAction.REGISTERED,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="policy_document",
        entity_id=row.id,
        payload={
            "name": name,
            "version": row.version,
            "document_id": str(document.id),
            "sha256": document.sha256,
        },
    )
    return row


async def _locked(session: AsyncSession, policy_document_id: uuid.UUID) -> PolicyDocument:
    row = await session.scalar(
        select(PolicyDocument)
        .where(PolicyDocument.id == policy_document_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise PolicyDocumentNotFoundError("policy document not found")
    return row


async def _finish(
    session: AsyncSession,
    row: PolicyDocument,
    outcome: IndexOutcome,
    *,
    actor_type: ActorType,
    actor_id: str,
    embedder: Embedder | None = None,
) -> IndexOutcome:
    if outcome.status == "pending":
        return outcome
    if outcome.status == PolicyDocumentStatus.INDEXED:
        row.status = PolicyDocumentStatus.INDEXED.value
        row.status_reason = None
        row.chunk_count = outcome.chunks
        row.indexed_at = datetime.now(UTC)
        if embedder is not None:
            row.embedding_model = embedder.model_id
            row.embedding_version = embedder.version
        action = PolicyDocumentAction.INDEXED
    elif outcome.status == PolicyDocumentStatus.NEEDS_REVIEW:
        row.status = PolicyDocumentStatus.NEEDS_REVIEW.value
        row.status_reason = outcome.reason
        row.chunk_count = 0
        action = PolicyDocumentAction.NEEDS_REVIEW
    else:
        row.status = PolicyDocumentStatus.FAILED.value
        row.status_reason = outcome.reason
        action = PolicyDocumentAction.INDEX_FAILED
    await session.flush()
    await append_audit(
        session,
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="policy_document",
        entity_id=row.id,
        payload={
            "name": row.name,
            "version": row.version,
            "chunks": outcome.chunks,
            "reason": outcome.reason,
            "guardrail_warnings": outcome.warnings,
            "embedding_model": row.embedding_model,
            "embedding_version": row.embedding_version,
        },
    )
    return outcome


async def index_normalized(
    session: AsyncSession,
    *,
    policy_document_id: uuid.UUID,
    normalized: NormalizedDocument,
    embedder: Embedder,
    filename: str | None = None,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: str = "policy-indexer",
) -> IndexOutcome:
    """Scan, chunk, embed and store ``normalized`` as the content of the policy document."""
    row = await _locked(session, policy_document_id)
    if row.status == PolicyDocumentStatus.ARCHIVED:
        return IndexOutcome("failed", "archived")
    if row.status == PolicyDocumentStatus.NEEDS_REVIEW:
        return IndexOutcome("needs_review", row.status_reason)
    guard = guard_policy_document(normalized, filename=filename)
    row.guardrail_findings = guard.warnings
    row.guardrail_version = guard.report.version
    warnings = len(guard.warnings)
    # An admin override (``request_override``) lets a needs_review document index; the
    # flagged and removed spans stay excluded either way (``guard.document``).
    if guard.review_reason is not None and row.overridden_at is None:
        # No chunk of this version may stay searchable (an empty index replaces any).
        await index_chunks(
            session,
            kind=POLICY_KIND,
            source_id=row.id,
            source_version=row.version,
            chunks=[],
            embedder=embedder,
        )
        return await _finish(
            session,
            row,
            IndexOutcome("needs_review", guard.review_reason, warnings=warnings),
            actor_type=actor_type,
            actor_id=actor_id,
        )
    chunks = chunk_document(guard.document)
    if not chunks:
        outcome = IndexOutcome("failed", "no_text")
    elif len(chunks) > MAX_CHUNKS:
        outcome = IndexOutcome("failed", "too_many_chunks")
    else:
        for chunk in chunks:
            chunk.meta.update({"name": row.name, "title": row.title, "version": row.version})
        try:
            count = await index_chunks(
                session,
                kind=POLICY_KIND,
                source_id=row.id,
                source_version=row.version,
                chunks=chunks,
                embedder=embedder,
            )
        except EmbeddingUnavailable:
            outcome = IndexOutcome("failed", "embedding_unavailable")
        else:
            outcome = IndexOutcome("indexed", chunks=count)
    outcome = dataclasses.replace(outcome, warnings=warnings)
    return await _finish(
        session, row, outcome, actor_type=actor_type, actor_id=actor_id, embedder=embedder
    )


_DOC_FAILURES: Final = {
    DocumentStatus.INFECTED.value: "document_infected",
    DocumentStatus.REJECTED.value: "document_rejected",
    DocumentStatus.PARSE_FAILED.value: "document_parse_failed",
}


async def index_policy_document(
    session: AsyncSession,
    store: ObjectStore,
    *,
    policy_document_id: uuid.UUID,
    embedder: Embedder,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: str = "policy-indexer",
) -> IndexOutcome:
    """Index a registered policy document once its file is parsed.

    Returns ``pending`` (nothing changed) while the file is still being scanned or
    parsed; ``failed`` with a reason when it can never be indexed.
    """
    row = await _locked(session, policy_document_id)
    if row.status == PolicyDocumentStatus.ARCHIVED:
        return IndexOutcome("failed", "archived")
    if row.status == PolicyDocumentStatus.NEEDS_REVIEW:
        return IndexOutcome("needs_review", row.status_reason)
    document = await session.scalar(select(Document).where(Document.id == row.document_id))
    if document is None:  # pragma: no cover - foreign key
        raise PolicyDocumentNotFoundError("the policy document's file is missing")

    async def fail(reason: str) -> IndexOutcome:
        return await _finish(
            session,
            row,
            IndexOutcome("failed", reason),
            actor_type=actor_type,
            actor_id=actor_id,
        )

    if document.status in _DOC_FAILURES:
        return await fail(_DOC_FAILURES[document.status])
    if document.status != DocumentStatus.PARSED.value or not document.parse_result_key:
        return IndexOutcome("pending", "document_not_parsed")
    if document.needs_review:
        return await fail("document_needs_review")
    try:
        raw = await store.get_bytes(document.parse_result_key, max_bytes=MAX_NORMALIZED_BYTES)
        normalized = NormalizedDocument.model_validate_json(raw)
    except (StorageError, ValidationError, ValueError):
        return await fail("parse_result_unavailable")
    return await index_normalized(
        session,
        policy_document_id=policy_document_id,
        normalized=normalized,
        embedder=embedder,
        filename=document.original_filename,
        actor_type=actor_type,
        actor_id=actor_id,
    )


def override_needs_two_admins(row: PolicyDocument) -> bool:
    """Whether overriding ``row`` needs a second admin (the two-person rule).

    Yes when any guardrail finding is high severity, and also when the guardrails could
    not run (``guardrails_error``): an unchecked document is treated like the worst case
    (fail closed). The ``policy_documents_guard`` trigger enforces the same rule.
    """
    if row.status_reason == NEEDS_REVIEW_ERROR:
        return True
    return any(
        isinstance(f, dict) and f.get("severity") == "high" for f in row.guardrail_findings or []
    )


@dataclass(frozen=True, slots=True)
class OverrideOutcome:
    # requested: recorded, waiting for a second admin. overridden: the document may index.
    status: str
    row: PolicyDocument


async def request_override(
    session: AsyncSession,
    *,
    policy_document_id: uuid.UUID,
    user_id: uuid.UUID,
    reason: str,
) -> OverrideOutcome:
    """Override the guardrails of a ``needs_review`` document, with a written reason.

    The caller checked ``core.policy.manage`` (a step-up permission) for a person.
    Without high-severity findings one admin is enough. Otherwise the first call records
    the request and a second call, by a different admin, approves it. Either way the
    reasons and the people are stored on the document and in the audit log. After the
    override the document is ``pending`` again; indexing then keeps every flagged span
    out of the chunks.
    """
    text_reason = (reason or "").strip()
    if not MIN_OVERRIDE_REASON <= len(text_reason) <= MAX_OVERRIDE_REASON:
        raise BadRequestError(
            f"the reason must be {MIN_OVERRIDE_REASON} to {MAX_OVERRIDE_REASON} characters"
        )
    row = await _locked(session, policy_document_id)
    if row.status != PolicyDocumentStatus.NEEDS_REVIEW or row.overridden_at is not None:
        raise OverrideNotAllowedError("only a policy document that needs review can be overridden")
    two_person = override_needs_two_admins(row)
    finding_ids = [str(f.get("id")) for f in row.guardrail_findings or [] if isinstance(f, dict)][
        :50
    ]
    now = datetime.now(UTC)
    if two_person and row.override_requested_by is None:
        row.override_requested_by = user_id
        row.override_requested_at = now
        row.override_request_reason = text_reason
        await session.flush()
        await append_audit(
            session,
            action=PolicyDocumentAction.OVERRIDE_REQUESTED,
            actor_type=ActorType.USER,
            actor_id=str(user_id),
            entity_type="policy_document",
            entity_id=row.id,
            payload={
                "name": row.name,
                "version": row.version,
                "reason": text_reason,
                "review_reason": row.status_reason,
                "findings": finding_ids,
                "second_admin_required": True,
            },
        )
        return OverrideOutcome("requested", row)
    if two_person and row.override_requested_by == user_id:
        raise SecondAdminRequiredError(
            "a different admin must approve an override that you requested"
        )
    row.overridden_by = user_id
    row.overridden_at = now
    row.override_reason = text_reason
    row.status = PolicyDocumentStatus.PENDING.value
    row.status_reason = "guardrails_overridden"
    await session.flush()
    await append_audit(
        session,
        action=PolicyDocumentAction.OVERRIDDEN,
        actor_type=ActorType.USER,
        actor_id=str(user_id),
        entity_type="policy_document",
        entity_id=row.id,
        payload={
            "name": row.name,
            "version": row.version,
            "reason": text_reason,
            "requested_by": str(row.override_requested_by) if two_person else None,
            "findings": finding_ids,
            "two_person": two_person,
        },
    )
    return OverrideOutcome("overridden", row)


async def archive_policy_document(
    session: AsyncSession,
    *,
    policy_document_id: uuid.UUID,
    actor_type: ActorType,
    actor_id: str,
) -> PolicyDocument:
    row = await _locked(session, policy_document_id)
    if row.status == PolicyDocumentStatus.ARCHIVED:
        return row
    row.status = PolicyDocumentStatus.ARCHIVED.value
    row.archived_at = datetime.now(UTC)
    await session.flush()
    await append_audit(
        session,
        action=PolicyDocumentAction.ARCHIVED,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="policy_document",
        entity_id=row.id,
        payload={"name": row.name, "version": row.version},
    )
    return row


async def list_policy_documents(
    session: AsyncSession, *, name: str | None = None, limit: int = 200
) -> Sequence[PolicyDocument]:
    query = select(PolicyDocument).order_by(PolicyDocument.name, PolicyDocument.version.desc())
    if name is not None:
        query = query.where(PolicyDocument.name == name)
    return list(await session.scalars(query.limit(limit)))


async def get_policy_document(
    session: AsyncSession, policy_document_id: uuid.UUID
) -> PolicyDocument | None:
    return await session.scalar(
        select(PolicyDocument).where(PolicyDocument.id == policy_document_id)
    )


async def active_policy_documents(
    session: AsyncSession, *, names: Sequence[str] | None = None
) -> list[PolicyDocument]:
    """The newest ``indexed`` version of each policy name (archived ones never count)."""
    query = (
        select(PolicyDocument)
        .where(PolicyDocument.status == PolicyDocumentStatus.INDEXED.value)
        .order_by(PolicyDocument.name, PolicyDocument.version.desc())
    )
    if names is not None:
        query = query.where(PolicyDocument.name.in_(list(names)))
    newest: dict[str, PolicyDocument] = {}
    for row in await session.scalars(query):
        newest.setdefault(row.name, row)
    return list(newest.values())


async def index_after_parse(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    embedder: Embedder,
) -> list[IndexOutcome]:
    """Index every pending policy document backed by ``document_id`` (worker hook).

    Called after a parse finishes. A file that is not a policy document is a no-op.
    """
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            ids = list(
                await session.scalars(
                    select(PolicyDocument.id).where(
                        PolicyDocument.document_id == document_id,
                        PolicyDocument.status == PolicyDocumentStatus.PENDING.value,
                    )
                )
            )
        outcomes: list[IndexOutcome] = []
        for policy_document_id in ids:
            async with session.begin():
                outcomes.append(
                    await index_policy_document(
                        session, store, policy_document_id=policy_document_id, embedder=embedder
                    )
                )
        return outcomes
