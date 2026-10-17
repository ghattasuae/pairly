# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``search``: one query over every kind the caller may search, best hits first."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.errors import BadRequestError
from pairly_core.flags.service import is_enabled
from pairly_core.rbac import Actor, RoleRegistry
from pairly_core.retrieval.embeddings import Embedder
from pairly_core.retrieval.search import MAX_LIMIT, hybrid_search
from pairly_core.search.sources import SearchRegistry, SearchSource

MAX_QUERY_CHARS: Final = 500


@dataclass(frozen=True, slots=True)
class UnifiedHit:
    kind: str
    source_id: uuid.UUID
    title: str
    workflow: str | None
    version: int | None
    chunk_id: uuid.UUID
    text: str
    page_start: int | None
    page_end: int | None
    section: str | None
    score: float


async def searchable_sources(
    session: AsyncSession, actor: Actor, roles: RoleRegistry, registry: SearchRegistry
) -> list[SearchSource]:
    """The kinds ``actor`` may search now (permission, then the kind's flag)."""
    out: list[SearchSource] = []
    for source in registry.sources:
        if not source.may_search(actor, roles):
            continue
        if source.flag is not None and not await is_enabled(session, source.flag):
            continue
        out.append(source)
    return out


async def search(
    session: AsyncSession,
    actor: Actor,
    *,
    roles: RoleRegistry,
    registry: SearchRegistry,
    embedder: Embedder,
    query: str,
    kinds: Sequence[str] | None = None,
    limit: int = 10,
) -> list[UnifiedHit]:
    """Hybrid search in each allowed kind, merged by fused score.

    ``kinds`` narrows the search; asking for a kind the caller may not search (or that
    does not exist, or whose flag is off) is a 400, never a silent empty result.
    """
    query = query.strip()
    if not query or len(query) > MAX_QUERY_CHARS:
        raise BadRequestError(f"the query must be 1 to {MAX_QUERY_CHARS} characters")
    limit = max(1, min(limit, MAX_LIMIT))
    available = {s.kind.key: s for s in await searchable_sources(session, actor, roles, registry)}
    if kinds is not None:
        unknown = sorted(set(kinds) - set(available))
        if unknown:
            raise BadRequestError(f"cannot search: {', '.join(unknown[:5])}")
        chosen = [available[k] for k in sorted(set(kinds))]
    else:
        chosen = list(available.values())
    hits: list[UnifiedHit] = []
    for source in chosen:
        scope = await source.scope(session, actor, roles)
        found = await hybrid_search(
            session,
            kind=source.kind,
            query=query,
            source_ids=scope,
            embedder=embedder,
            limit=limit,
        )
        if not found:
            continue
        info = await source.describe(session, sorted({h.source_id for h in found}))
        for h in found:
            described = info.get(h.source_id)
            if described is None:  # pragma: no cover - same transaction, same visibility
                continue
            hits.append(
                UnifiedHit(
                    kind=source.kind.key,
                    source_id=h.source_id,
                    title=described.title,
                    workflow=described.workflow,
                    version=described.version,
                    chunk_id=h.chunk_id,
                    text=h.text,
                    page_start=h.page_start,
                    page_end=h.page_end,
                    section=h.section,
                    score=h.score,
                )
            )
    hits.sort(key=lambda h: (-h.score, h.kind, str(h.chunk_id)))
    return hits[:limit]
