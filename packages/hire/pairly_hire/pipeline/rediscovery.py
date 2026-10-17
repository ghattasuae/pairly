# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Talent rediscovery (brief 3.4.3): search past applicants for new roles.

The engine's search (``pairly_core.search``) gets one more kind, ``hire_candidate``
(``SEARCH_SOURCES``, registered by the plugin). A candidate is searchable only when:

* they agreed to be considered for future roles (``hire_candidate_prefs.talent_pool``;
  consent is never assumed), and
* at least one of their applications is submitted and inside its retention period
  (``retention_until`` empty or in the future).

Both rules hold twice: the index only ever holds such candidates (``index_candidate``
removes the others, and a candidate who withdraws consent is removed at once), and the
search scope (``_scope``) filters on them again at query time, so a stale index entry
can never surface. Searching needs ``hire.candidate.read``.

What is indexed per candidate, PII redacted (``search.documents.redact_chunks``): the
job titles they applied for, their tags, and the text of their latest parsed CV when
the document store has it. Contact details are never indexed. Hits are labelled
"Candidate <reference>"; the rediscovery route names the person only where blind mode
allows it.
"""

from __future__ import annotations

import functools
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from pydantic import ValidationError
from sqlalchemy import Select, delete, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.db import set_tenant
from pairly_core.documents.model import NormalizedDocument
from pairly_core.ingestion.models import Document, DocumentStatus
from pairly_core.ingestion.storage import ObjectStore, StorageError
from pairly_core.jobs.registry import JobContext
from pairly_core.policy.chunking import chunk_document
from pairly_core.rbac import Actor, RoleRegistry
from pairly_core.retrieval.embeddings import Embedder, EmbeddingUnavailable
from pairly_core.retrieval.models import SearchChunk
from pairly_core.retrieval.search import ChunkInput, SearchableKind, chunk_hash, index_chunks
from pairly_core.search.documents import redact_chunks
from pairly_core.search.sources import SearchSource, SourceInfo, allowed
from pairly_hire.messaging.models import HireCandidatePrefs
from pairly_hire.models import ApplicationStatus, HireApplication, HireJob
from pairly_hire.permissions import CANDIDATE_READ
from pairly_hire.pipeline.models import HirePipelineEntry

log = structlog.get_logger("pairly_hire.rediscovery")

KIND: Final = SearchableKind("hire_candidate", "Past applicants who agreed to be considered")
MAX_CHUNKS: Final = 40
MAX_NORMALIZED_BYTES: Final = 64 * 1024 * 1024
JOB_NAME: Final = "hire_rediscovery_index"


def _eligible_apps(now: Any = None) -> Any:
    moment = now if now is not None else func.now()
    return (
        HireApplication.status != ApplicationStatus.INCOMPLETE.value,
        or_(HireApplication.retention_until.is_(None), HireApplication.retention_until > moment),
    )


def eligible_candidates() -> Select[Any]:
    """Candidate ids that consented and have an application inside its retention."""
    return (
        select(HireCandidatePrefs.candidate_id)
        .where(HireCandidatePrefs.talent_pool.is_(True))
        .where(
            exists().where(
                HireApplication.candidate_id == HireCandidatePrefs.candidate_id,
                *_eligible_apps(),
            )
        )
    )


def _may(actor: Actor, registry: RoleRegistry) -> bool:
    return CANDIDATE_READ in registry and allowed(actor, CANDIDATE_READ)


async def _scope(session: AsyncSession, actor: Actor, registry: RoleRegistry) -> Select[Any]:
    return eligible_candidates()


async def _describe(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, SourceInfo]:
    rows = await session.execute(
        select(HireApplication.candidate_id, HireApplication.reference)
        .where(HireApplication.candidate_id.in_(ids), *_eligible_apps())
        .order_by(HireApplication.candidate_id, HireApplication.submitted_at.desc())
    )
    out: dict[uuid.UUID, SourceInfo] = {}
    for row in rows:
        out.setdefault(
            row.candidate_id, SourceInfo(title=f"Candidate {row.reference}", workflow="hire")
        )
    return out


SEARCH_SOURCES: Final[tuple[SearchSource, ...]] = (SearchSource(KIND, _may, _scope, _describe),)


async def candidate_applications(
    session: AsyncSession, candidate_id: uuid.UUID
) -> list[dict[str, Any]]:
    """A consenting candidate's applications inside retention, newest first (for hits).
    Empty when the candidate is not eligible (consent withdrawn, retention passed)."""
    from pairly_hire.models import HireCandidate
    from pairly_hire.pipeline.records import AppRecord
    from pairly_hire.pipeline.stages import stages_for_jobs

    consent = await session.scalar(
        select(HireCandidatePrefs.talent_pool).where(
            HireCandidatePrefs.candidate_id == candidate_id
        )
    )
    if not consent:
        return []
    rows = (
        await session.execute(
            select(HireApplication, HireCandidate, HireJob, HirePipelineEntry)
            .join(HireCandidate, HireCandidate.id == HireApplication.candidate_id)
            .join(HireJob, HireJob.id == HireApplication.job_id)
            .outerjoin(HirePipelineEntry, HirePipelineEntry.application_id == HireApplication.id)
            .where(HireApplication.candidate_id == candidate_id, *_eligible_apps())
            .order_by(HireApplication.submitted_at.desc())
        )
    ).all()
    stages = await stages_for_jobs(session, list({r[2].id for r in rows}))
    out: list[dict[str, Any]] = []
    for app, candidate, job, entry in rows:
        record = AppRecord(app, candidate, job, entry, stages[job.id])
        out.append(
            {
                "application_id": str(app.id),
                "job_id": str(job.id),
                "job_title": job.title,
                "reference": app.reference,
                "stage_key": record.stage_key,
                "stage_label": record.stage.label,
                "submitted_at": app.submitted_at.isoformat() if app.submitted_at else None,
                "name": record.full_name,
                "revealed": record.revealed,
            }
        )
    return out


# ------------------------------------------------------------------ indexing


async def remove_candidate(session: AsyncSession, candidate_id: uuid.UUID) -> int:
    result = await session.execute(
        delete(SearchChunk).where(
            SearchChunk.kind == KIND.key, SearchChunk.source_id == candidate_id
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def _cv_chunks(
    session: AsyncSession, store: ObjectStore | None, document_id: uuid.UUID | None
) -> list[ChunkInput]:
    if store is None or document_id is None:
        return []
    document = await session.scalar(select(Document).where(Document.id == document_id))
    if (
        document is None
        or document.status != DocumentStatus.PARSED.value
        or not document.parse_result_key
        or document.needs_review
    ):
        return []
    try:
        raw = await store.get_bytes(document.parse_result_key, max_bytes=MAX_NORMALIZED_BYTES)
        normalized = NormalizedDocument.model_validate_json(raw)
    except (StorageError, ValidationError, ValueError):
        return []
    return chunk_document(normalized)


async def candidate_chunks(
    session: AsyncSession, candidate_id: uuid.UUID, store: ObjectStore | None
) -> list[ChunkInput]:
    """What is indexed for one eligible candidate (redacted), or [] when not eligible."""
    rows = (
        await session.execute(
            select(HireApplication, HireJob.title, HirePipelineEntry.tags)
            .join(HireJob, HireJob.id == HireApplication.job_id)
            .outerjoin(HirePipelineEntry, HirePipelineEntry.application_id == HireApplication.id)
            .where(HireApplication.candidate_id == candidate_id, *_eligible_apps())
            .order_by(HireApplication.submitted_at.desc())
        )
    ).all()
    if not rows:
        return []
    titles = sorted({r.title for r in rows})
    tags = sorted({t for r in rows for t in (r.tags or [])})
    summary = f"Applied for: {', '.join(titles)}."
    if tags:
        summary += f" Tags: {', '.join(tags)}."
    chunks = [ChunkInput(text=summary[:3900], section="summary")]
    chunks += await _cv_chunks(session, store, rows[0][0].cv_document_id)
    redacted = redact_chunks(chunks)[:MAX_CHUNKS]
    for chunk in redacted:
        chunk.meta.update({"workflow": "hire"})
    return redacted


async def index_candidate(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    embedder: Embedder,
    store: ObjectStore | None,
) -> str:
    """Index or remove one candidate. Returns ``indexed``, ``unchanged`` or ``removed``."""
    consent = await session.scalar(
        select(HireCandidatePrefs.talent_pool).where(
            HireCandidatePrefs.candidate_id == candidate_id
        )
    )
    chunks = await candidate_chunks(session, candidate_id, store) if consent else []
    if not chunks:
        removed = await remove_candidate(session, candidate_id)
        return "removed" if removed else "unchanged"
    current = list(
        await session.scalars(
            select(SearchChunk.content_hash)
            .where(SearchChunk.kind == KIND.key, SearchChunk.source_id == candidate_id)
            .order_by(SearchChunk.chunk_index)
        )
    )
    if current == [chunk_hash(c.text) for c in chunks]:
        return "unchanged"
    await index_chunks(
        session,
        kind=KIND,
        source_id=candidate_id,
        source_version=1,
        chunks=chunks,
        embedder=embedder,
    )
    return "indexed"


async def sync_org(
    session: AsyncSession, *, embedder: Embedder, store: ObjectStore | None
) -> dict[str, int]:
    """Bring the organisation's index in line with consent and retention."""
    counts = {"indexed": 0, "unchanged": 0, "removed": 0, "failed": 0}
    wanted = set(await session.scalars(eligible_candidates()))
    present = set(
        await session.scalars(
            select(SearchChunk.source_id).where(SearchChunk.kind == KIND.key).distinct()
        )
    )
    for candidate_id in sorted(wanted | present, key=str):
        try:
            async with session.begin_nested():
                outcome = await index_candidate(
                    session, candidate_id, embedder=embedder, store=store
                )
        except EmbeddingUnavailable:
            counts["failed"] += 1
            continue
        counts[outcome] += 1
    return counts


@dataclass(frozen=True, slots=True)
class _Services:
    embedder: Embedder


@functools.cache
def _embedder_for(sessionmaker: Any) -> _Services:
    from pairly_core.llm.settings import load_llm_settings
    from pairly_core.retrieval.metered import build_metered_embedder

    return _Services(build_metered_embedder(load_llm_settings(), sessionmaker))


async def org_ids(ctx: JobContext) -> list[uuid.UUID]:
    async with ctx.sessionmaker() as session, session.begin():
        rows = await session.execute(text("SELECT org_id FROM hire_pipeline_org_ids() AS org_id"))
        return [uuid.UUID(str(v)) for v in rows.scalars()]


async def run_index_job(ctx: JobContext) -> dict[str, Any]:
    """``hire_rediscovery_index``: every 15 minutes, per organisation with applicants."""
    embedder = _embedder_for(ctx.sessionmaker).embedder
    store = ctx.runtime.services.store
    totals = {"orgs": 0, "indexed": 0, "removed": 0, "failed": 0}
    for org_id in await org_ids(ctx):
        session = ctx.sessionmaker()
        set_tenant(session, org_id, None)
        try:
            async with session, session.begin():
                counts = await sync_org(session, embedder=embedder, store=store)
        except Exception as exc:
            log.warning("rediscovery_index_failed", org_id=str(org_id), error=type(exc).__name__)
            totals["failed"] += 1
            continue
        totals["orgs"] += 1
        totals["indexed"] += counts["indexed"]
        totals["removed"] += counts["removed"]
        totals["failed"] += counts["failed"]
    totals["at"] = int(datetime.now(UTC).timestamp())
    return totals
