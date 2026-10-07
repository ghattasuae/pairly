# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``MeteredEmbedder``: embeddings billed and recorded through the model router.

Every embedding call goes through ``ModelRouter.embed``: the worst-case cost is
reserved against the organisation's monthly budget, and a ``model_calls`` row (task
``core_embed``) records it, with a hash per input text and a hash of the vectors
(never the texts or the vectors themselves). Offline (``recorded`` mode, or no key)
the inner embedder is the local ``HashingEmbedder``: free, deterministic, and still
recorded.

A call has to be billed to an organisation, so the plain ``embed`` refuses (fail
closed); ``pairly_core.retrieval.search`` calls ``embed_for_org`` with the session's
tenant. A refused or failed call surfaces as ``EmbeddingUnavailable``, which every
caller already treats as "route to a person".
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.llm.errors import ModelUnavailable
from pairly_core.llm.factory import build_router
from pairly_core.llm.router import ModelRouter
from pairly_core.llm.settings import LLMSettings
from pairly_core.llm.types import CallContext
from pairly_core.retrieval.embeddings import (
    EmbeddingBatch,
    EmbeddingUnavailable,
    Purpose,
    build_embedder,
)


class MeterableEmbedder(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    @property
    def relevance_floor(self) -> float: ...

    @property
    def provider(self) -> str: ...

    @property
    def price_model(self) -> str | None: ...

    async def embed_batch(self, texts: Sequence[str], *, purpose: Purpose) -> EmbeddingBatch: ...


@runtime_checkable
class OrgEmbedder(Protocol):
    """An embedder that bills each call to an organisation."""

    async def embed_for_org(
        self,
        org_id: uuid.UUID,
        texts: Sequence[str],
        *,
        purpose: Purpose,
        context: CallContext | None = None,
    ) -> list[list[float] | None]: ...


class MeteredEmbedder:
    def __init__(self, inner: MeterableEmbedder, router: ModelRouter) -> None:
        self._inner = inner
        self._router = router

    @property
    def inner(self) -> MeterableEmbedder:
        return self._inner

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    @property
    def version(self) -> str:
        return self._inner.version

    @property
    def dimensions(self) -> int:
        return self._inner.dimensions

    @property
    def relevance_floor(self) -> float:
        return self._inner.relevance_floor

    async def embed(self, texts: Sequence[str], *, purpose: Purpose) -> list[list[float] | None]:
        raise EmbeddingUnavailable("a metered embedding needs an organisation to bill")

    async def embed_for_org(
        self,
        org_id: uuid.UUID,
        texts: Sequence[str],
        *,
        purpose: Purpose,
        context: CallContext | None = None,
    ) -> list[list[float] | None]:
        try:
            return await self._router.embed(
                backend=self._inner,
                texts=texts,
                purpose=purpose,
                org_id=org_id,
                context=context,
            )
        except ModelUnavailable as exc:
            raise EmbeddingUnavailable(f"embedding refused: {exc.code}") from None


def build_metered_embedder(
    settings: LLMSettings, sessionmaker: async_sessionmaker[AsyncSession]
) -> MeteredEmbedder:
    """The settings' embedder (OpenAI, or the offline hashing one), metered by a router."""
    return MeteredEmbedder(build_embedder(settings), build_router(settings, sessionmaker))
