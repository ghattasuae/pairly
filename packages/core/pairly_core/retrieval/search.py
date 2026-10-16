# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Hybrid search over ``search_chunks``: Postgres full-text plus pgvector, fused by RRF.

Generic over a ``SearchableKind`` (policy documents now; candidates, documents, vendors
and invoices with WP1.12). A search runs inside the caller's tenant session, so Row
Level Security limits it to the organisation; the query also filters on ``org_id`` and
on the sources the caller allows (for policies: the active document versions).

Two ranked lists are fused with reciprocal rank fusion, ``score = sum 1 / (k + rank)``
with ``k = 60`` (Cormack et al., 2009):

* lexical: rows matching an OR query of the meaningful query words under the
  ``english`` (stemmed) or the ``simple`` (exact words, Arabic) configuration, ranked by
  ``ts_rank_cd`` over both;
* semantic: nearest rows by cosine distance among vectors of the same embedding model
  and version, kept only above the embedder's ``relevance_floor``.

A row is returned only when at least one list matched it, so "nothing relevant" is an
empty result, never a list of the least bad rows.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import ColumnElement, Float, Select, delete, func, literal, or_, select
from sqlalchemy.dialects.postgresql import REGCONFIG, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from pairly_core.db import TenantError, current_org
from pairly_core.retrieval.embeddings import Embedder, Purpose
from pairly_core.retrieval.metered import OrgEmbedder
from pairly_core.retrieval.models import MAX_CHUNK_CHARS, SearchChunk
from pairly_core.retrieval.text import query_words
from pairly_core.retrieval.vector import Vector

# The sources a search may return: a list of ids, or a SELECT of ids (for kinds with many
# sources, such as documents, filtered in SQL rather than listed).
SourceScope = Sequence[uuid.UUID] | Select[Any]

RRF_K: Final = 60
MAX_LIMIT: Final = 50
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass(frozen=True, slots=True)
class SearchableKind:
    """A kind of thing that can be indexed and searched (``policy``, later ``candidate``)."""

    key: str
    description: str = ""

    def __post_init__(self) -> None:
        if not _KIND_RE.fullmatch(self.key):
            raise ValueError("invalid searchable kind")


@dataclass(frozen=True, slots=True)
class ChunkInput:
    text: str
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    source_version: int
    chunk_index: int
    text: str
    page_start: int | None
    page_end: int | None
    section: str | None
    meta: dict[str, Any]
    score: float
    lexical_rank: int | None
    vector_rank: int | None
    similarity: float | None


def chunk_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def embed_texts(
    embedder: Embedder, org_id: uuid.UUID, texts: Sequence[str], *, purpose: Purpose
) -> list[list[float] | None]:
    """Embed for one organisation: billed and recorded when the embedder is metered."""
    if isinstance(embedder, OrgEmbedder):
        return await embedder.embed_for_org(org_id, texts, purpose=purpose)
    return await embedder.embed(texts, purpose=purpose)


async def index_chunks(
    session: AsyncSession,
    *,
    kind: SearchableKind,
    source_id: uuid.UUID,
    source_version: int,
    chunks: Sequence[ChunkInput],
    embedder: Embedder,
) -> int:
    """Replace the indexed chunks of one source. Returns how many were stored.

    Embeds first, so an embedding failure (``EmbeddingUnavailable``) leaves the index
    untouched. Runs in the caller's transaction.
    """
    org = current_org(session)
    if org is None:
        raise TenantError("indexing needs a tenant context")
    for chunk in chunks:
        if not chunk.text.strip() or len(chunk.text) > MAX_CHUNK_CHARS:
            raise ValueError("chunk text must be 1 to 4000 characters")
    vectors = (
        await embed_texts(embedder, org, [c.text for c in chunks], purpose="document")
        if chunks
        else []
    )
    if len(vectors) != len(chunks):
        raise ValueError("the embedder returned the wrong number of vectors")
    await session.execute(
        delete(SearchChunk).where(
            SearchChunk.org_id == org,
            SearchChunk.kind == kind.key,
            SearchChunk.source_id == source_id,
        )
    )
    rows = [
        {
            "org_id": org,
            "kind": kind.key,
            "source_id": source_id,
            "source_version": source_version,
            "chunk_index": index,
            "text": chunk.text,
            "content_hash": chunk_hash(chunk.text),
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "section": chunk.section,
            "meta": chunk.meta,
            "embedding": vector,
            "embedding_model": embedder.model_id if vector is not None else None,
            "embedding_version": embedder.version if vector is not None else None,
        }
        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
    ]
    for start in range(0, len(rows), 200):
        await session.execute(insert(SearchChunk), rows[start : start + 200])
    return len(rows)


def _in_scope(source_ids: SourceScope) -> ColumnElement[bool]:
    if isinstance(source_ids, Select):
        return SearchChunk.source_id.in_(source_ids)
    return SearchChunk.source_id.in_(list(source_ids))


def _empty_scope(source_ids: SourceScope) -> bool:
    return not isinstance(source_ids, Select) and not source_ids


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[uuid.UUID]], *, k: int = RRF_K
) -> dict[uuid.UUID, float]:
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


async def _lexical(
    session: AsyncSession,
    org: uuid.UUID,
    kind: SearchableKind,
    words: list[str],
    source_ids: SourceScope,
    limit: int,
) -> list[uuid.UUID]:
    if not words:
        return []
    query_text = " or ".join(words)
    q_en = func.websearch_to_tsquery(literal("english", REGCONFIG), query_text)
    q_simple = func.websearch_to_tsquery(literal("simple", REGCONFIG), query_text)
    rank = func.ts_rank_cd(SearchChunk.tsv_en, q_en) + func.ts_rank_cd(
        SearchChunk.tsv_simple, q_simple
    )
    rows = await session.execute(
        select(SearchChunk.id)
        .where(
            SearchChunk.org_id == org,
            SearchChunk.kind == kind.key,
            _in_scope(source_ids),
            or_(SearchChunk.tsv_en.op("@@")(q_en), SearchChunk.tsv_simple.op("@@")(q_simple)),
        )
        .order_by(rank.desc(), SearchChunk.chunk_index, SearchChunk.id)
        .limit(limit)
    )
    return [r[0] for r in rows]


async def _semantic(
    session: AsyncSession,
    org: uuid.UUID,
    kind: SearchableKind,
    vector: list[float],
    embedder: Embedder,
    source_ids: SourceScope,
    limit: int,
) -> list[tuple[uuid.UUID, float]]:
    # The HNSW index is shared by every tenant. Without iterative scans, the index could
    # return only other tenants' rows (then removed by RLS and the filters) and the
    # search would look empty. pgvector 0.8 keeps scanning until enough rows pass.
    await session.execute(select(func.set_config("hnsw.iterative_scan", "strict_order", True)))
    query_vec = literal(vector, Vector(embedder.dimensions))
    distance = SearchChunk.embedding.op("<=>", return_type=Float)(query_vec)
    rows = await session.execute(
        select(SearchChunk.id, distance.label("distance"))
        .where(
            SearchChunk.org_id == org,
            SearchChunk.kind == kind.key,
            _in_scope(source_ids),
            SearchChunk.embedding.is_not(None),
            SearchChunk.embedding_model == embedder.model_id,
            SearchChunk.embedding_version == embedder.version,
        )
        .order_by(distance, SearchChunk.id)
        .limit(limit)
    )
    out: list[tuple[uuid.UUID, float]] = []
    for chunk_id, dist in rows:
        similarity = 1.0 - float(dist)
        if similarity >= embedder.relevance_floor:
            out.append((chunk_id, similarity))
    return out


async def hybrid_search(
    session: AsyncSession,
    *,
    kind: SearchableKind,
    query: str,
    source_ids: SourceScope,
    embedder: Embedder,
    limit: int = 8,
    candidates: int = 40,
) -> list[SearchHit]:
    """The ``limit`` best chunks of the allowed sources for ``query``, best first."""
    org = current_org(session)
    if org is None:
        raise TenantError("search needs a tenant context")
    limit = max(1, min(limit, MAX_LIMIT))
    if _empty_scope(source_ids) or not query.strip():
        return []
    words = query_words(query)
    lexical = await _lexical(session, org, kind, words, source_ids, candidates)
    semantic: list[tuple[uuid.UUID, float]] = []
    vectors = await embed_texts(embedder, org, [query], purpose="query")
    if vectors and vectors[0] is not None:
        semantic = await _semantic(session, org, kind, vectors[0], embedder, source_ids, candidates)
    vector_ids = [cid for cid, _ in semantic]
    scores = reciprocal_rank_fusion([lexical, vector_ids])
    if not scores:
        return []
    best = sorted(scores, key=lambda cid: (-scores[cid], str(cid)))[:limit]
    rows = {
        row.id: row
        for row in await session.scalars(
            select(SearchChunk)
            .where(SearchChunk.org_id == org, SearchChunk.id.in_(best))
            .options(
                load_only(
                    SearchChunk.source_id,
                    SearchChunk.source_version,
                    SearchChunk.chunk_index,
                    SearchChunk.text,
                    SearchChunk.page_start,
                    SearchChunk.page_end,
                    SearchChunk.section,
                    SearchChunk.meta,
                )
            )
        )
    }
    lex_rank = {cid: i for i, cid in enumerate(lexical, start=1)}
    vec_rank = {cid: i for i, cid in enumerate(vector_ids, start=1)}
    similarity = dict(semantic)
    hits: list[SearchHit] = []
    for cid in best:
        row = rows.get(cid)
        if row is None:  # pragma: no cover - same transaction, same visibility
            continue
        hits.append(
            SearchHit(
                chunk_id=row.id,
                source_id=row.source_id,
                source_version=row.source_version,
                chunk_index=row.chunk_index,
                text=row.text,
                page_start=row.page_start,
                page_end=row.page_end,
                section=row.section,
                meta=dict(row.meta or {}),
                score=scores[cid],
                lexical_rank=lex_rank.get(cid),
                vector_rank=vec_rank.get(cid),
                similarity=similarity.get(cid),
            )
        )
    return hits
