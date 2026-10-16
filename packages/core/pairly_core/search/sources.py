# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The search source registry: who may search which kind, over which sources.

A ``SearchSource`` has four parts:

* ``may_search(actor, registry)``: whether the caller may search this kind at all;
* ``scope(session, actor, registry)``: the sources (ids, or a SELECT of ids) the caller
  may see hits from;
* ``describe(session, ids)``: a title and a workflow for each hit's source;
* ``flag``: a feature flag that must be on for the organisation (None: always on).

Core registers ``policy`` (active policy versions, ``core.policy.read``) and
``document`` (parsed documents of the workflows whose documents the caller may read,
behind the ``search.documents`` flag). A workflow plugin may add sources for its own
kinds through an optional ``search_sources`` attribute; the kind key must start with
the plugin id (for example ``hire_candidate``).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.documents.permissions import document_permission
from pairly_core.flags.registry import SEARCH_DOCUMENTS
from pairly_core.ingestion.models import Document, DocumentStatus
from pairly_core.policy.documents import POLICY_KIND, active_policy_documents
from pairly_core.policy.permissions import POLICY_READ
from pairly_core.rbac import Actor, PermissionDeniedError, RoleRegistry, authorize
from pairly_core.retrieval.search import SearchableKind, SourceScope
from pairly_core.search.documents import DOCUMENT_KIND


class SearchSourceError(ValueError):
    """An invalid or duplicate search source (a programming error)."""


@dataclass(frozen=True, slots=True)
class SourceInfo:
    title: str
    workflow: str | None = None
    version: int | None = None


MayFn = Callable[[Actor, RoleRegistry], bool]
ScopeFn = Callable[[AsyncSession, Actor, RoleRegistry], Awaitable[SourceScope]]
DescribeFn = Callable[[AsyncSession, list[uuid.UUID]], Awaitable[dict[uuid.UUID, SourceInfo]]]


@dataclass(frozen=True, slots=True)
class SearchSource:
    kind: SearchableKind
    may_search: MayFn
    scope: ScopeFn
    describe: DescribeFn
    flag: str | None = None


def allowed(actor: Actor, permission: str) -> bool:
    """``authorize`` as a yes or no (a refusal here is not a denied request)."""
    try:
        authorize(actor, permission)
    except PermissionDeniedError:
        return False
    return True


# ------------------------------------------------------------------ policy


def _policy_may(actor: Actor, registry: RoleRegistry) -> bool:
    return POLICY_READ in registry and allowed(actor, POLICY_READ)


async def _policy_scope(session: AsyncSession, actor: Actor, registry: RoleRegistry) -> SourceScope:
    return [row.id for row in await active_policy_documents(session)]


async def _policy_describe(
    session: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, SourceInfo]:
    from pairly_core.policy.models import PolicyDocument

    rows = await session.execute(
        select(
            PolicyDocument.id, PolicyDocument.title, PolicyDocument.name, PolicyDocument.version
        ).where(PolicyDocument.id.in_(ids))
    )
    return {r.id: SourceInfo(title=r.title or r.name, version=r.version) for r in rows}


# ------------------------------------------------------------------ document


def readable_workflows(actor: Actor, registry: RoleRegistry) -> list[str]:
    """The workflows whose documents ``actor`` may read."""
    return sorted(
        w for w in registry.workflows if allowed(actor, document_permission(registry, w, "read"))
    )


def _document_may(actor: Actor, registry: RoleRegistry) -> bool:
    return bool(readable_workflows(actor, registry))


async def _document_scope(
    session: AsyncSession, actor: Actor, registry: RoleRegistry
) -> SourceScope:
    workflows = readable_workflows(actor, registry)
    if not workflows:
        return []
    return select(Document.id).where(
        Document.org_id == actor.org_id,
        Document.workflow.in_(workflows),
        Document.status == DocumentStatus.PARSED.value,
        Document.needs_review.is_(False),
    )


async def _document_describe(
    session: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, SourceInfo]:
    rows = await session.execute(
        select(Document.id, Document.original_filename, Document.workflow).where(
            Document.id.in_(ids)
        )
    )
    return {
        r.id: SourceInfo(title=r.original_filename or "document", workflow=r.workflow) for r in rows
    }


CORE_SOURCES: tuple[SearchSource, ...] = (
    SearchSource(POLICY_KIND, _policy_may, _policy_scope, _policy_describe),
    SearchSource(
        DOCUMENT_KIND, _document_may, _document_scope, _document_describe, flag=SEARCH_DOCUMENTS
    ),
)


class SearchRegistry:
    def __init__(self, sources: Iterable[SearchSource] = CORE_SOURCES) -> None:
        self._sources: dict[str, SearchSource] = {}
        for source in sources:
            if source.kind.key in self._sources:
                raise SearchSourceError(f"duplicate search kind {source.kind.key!r}")
            self._sources[source.kind.key] = source

    @property
    def sources(self) -> list[SearchSource]:
        return [self._sources[k] for k in sorted(self._sources)]

    def get(self, kind: str) -> SearchSource | None:
        return self._sources.get(kind)


def build_search_registry(plugins: Iterable[Any] = ()) -> SearchRegistry:
    sources = list(CORE_SOURCES)
    for plugin in plugins:
        for source in getattr(plugin, "search_sources", ()) or ():
            if not isinstance(source, SearchSource):
                raise SearchSourceError(f"plugin {plugin.id!r}: search_sources must be sources")
            if not source.kind.key.startswith(f"{plugin.id}_"):
                raise SearchSourceError(
                    f"plugin {plugin.id!r}: kind {source.kind.key!r} is outside its namespace"
                )
            sources.append(source)
    return SearchRegistry(sources)
