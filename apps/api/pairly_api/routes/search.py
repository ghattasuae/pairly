# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/search``: one search over every kind the caller may search (WP1.12).

* ``GET  /v1/search/kinds``   the kinds the caller may search now
* ``POST /v1/search``         ``{"query": "...", "kinds": [...]?, "limit": 10}``: hybrid
  (full-text plus semantic) hits from each allowed kind, best first

A kind is searchable when the caller holds its read permission and its feature flag
(if any) is on: ``policy`` (``core.policy.read``), ``document`` (documents of the
workflows the caller may read, behind ``search.documents``). Naming a kind the caller
cannot search is a 400. The embedder is the metered one (budget checked, recorded).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from pairly_api.routes.policies import EmbedderDep
from pairly_core.errors import ServiceUnavailableError
from pairly_core.identity import Context, CurrentActor, OrgSession
from pairly_core.retrieval.embeddings import EmbeddingUnavailable
from pairly_core.search.service import MAX_QUERY_CHARS, search, searchable_sources
from pairly_core.search.sources import SearchRegistry

router = APIRouter(prefix="/search", tags=["search"])

KindName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


def search_registry(request: Request) -> SearchRegistry:
    registry = getattr(request.app.state, "search_registry", None)
    if not isinstance(registry, SearchRegistry):
        registry = SearchRegistry()
        request.app.state.search_registry = registry
    return registry


Registry = Annotated[SearchRegistry, Depends(search_registry)]


class KindOut(BaseModel):
    kind: str
    description: str


class SearchIn(BaseModel):
    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_CHARS)]
    kinds: Annotated[list[KindName], Field(min_length=1, max_length=20)] | None = None
    limit: Annotated[int, Field(ge=1, le=50)] = 10


class HitOut(BaseModel):
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


@router.get("/kinds", response_model=list[KindOut])
async def kinds(
    actor: CurrentActor, session: OrgSession, ctx: Context, registry: Registry
) -> list[KindOut]:
    sources = await searchable_sources(session, actor, ctx.registry, registry)
    return [KindOut(kind=s.kind.key, description=s.kind.description) for s in sources]


@router.post("", response_model=list[HitOut])
async def run_search(
    body: SearchIn,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
    registry: Registry,
    embedder: EmbedderDep,
) -> list[HitOut]:
    try:
        hits = await search(
            session,
            actor,
            roles=ctx.registry,
            registry=registry,
            embedder=embedder,
            query=body.query,
            kinds=body.kinds,
            limit=body.limit,
        )
    except EmbeddingUnavailable:
        raise ServiceUnavailableError("search is unavailable") from None
    return [
        HitOut(
            kind=h.kind,
            source_id=h.source_id,
            title=h.title,
            workflow=h.workflow,
            version=h.version,
            chunk_id=h.chunk_id,
            text=h.text,
            page_start=h.page_start,
            page_end=h.page_end,
            section=h.section,
            score=h.score,
        )
        for h in hits
    ]
