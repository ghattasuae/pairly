# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Embeddings are metered: budget reservation and a ``model_calls`` row (core_embed)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker
from pairly_core.llm.budget import BudgetManager
from pairly_core.llm.catalog import load_catalog, load_prices
from pairly_core.llm.hashing import canonical_json, sha256_hex
from pairly_core.llm.prompt_registry import PromptRegistry
from pairly_core.llm.providers.fake import FakeProvider
from pairly_core.llm.router import EMBED_TASK, ModelRouter
from pairly_core.llm.settings import LLMSettings, ModelMode
from pairly_core.llm.store import DatabaseCallStore
from pairly_core.llm.types import PartialUsage
from pairly_core.models.llm import LLMBudgetReservation
from pairly_core.policy.documents import POLICY_KIND, active_policy_documents, index_normalized
from pairly_core.retrieval.embeddings import (
    EmbeddingBatch,
    EmbeddingUnavailable,
    HashingEmbedder,
    PartialEmbeddingError,
)
from pairly_core.retrieval.metered import MeteredEmbedder
from pairly_core.retrieval.search import hybrid_search

from .policy_support import (
    SPEND_POLICY,
    add_policy,
    build_document,
    new_org,
    pol_engine,
    session_for,
)

__all__ = ["pol_engine"]  # fixture


def make_router(engine: AsyncEngine, *, mode: ModelMode, limit: int = 10_000_000) -> ModelRouter:
    prices = load_prices()
    return ModelRouter(
        settings=LLMSettings(model_mode=mode),
        providers={
            "anthropic": FakeProvider("anthropic", available=False),
            "openai": FakeProvider("openai", available=False),
        },
        catalog=load_catalog(prices),
        prices=prices,
        prompts=PromptRegistry(),
        sessionmaker=create_sessionmaker(engine),
        budget=BudgetManager(default_limit_micros=limit),
        store=DatabaseCallStore(),
    )


class PricedFake(HashingEmbedder):
    """Hashing vectors billed as text-embedding-3-small (no network)."""

    def __init__(self, tokens: int = 1000, *, fail: bool = False) -> None:
        super().__init__()
        self.tokens = tokens
        self.fail = fail

    @property
    def provider(self) -> str:
        return "openai"

    @property
    def price_model(self) -> str | None:
        return "text-embedding-3-small"

    async def embed_batch(self, texts: Sequence[str], *, purpose: Any) -> EmbeddingBatch:
        if self.fail:
            raise EmbeddingUnavailable("provider down")
        return EmbeddingBatch(await self.embed(texts, purpose=purpose), self.tokens)


async def calls(owner_engine: AsyncEngine, org: uuid.UUID) -> list[Any]:
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)})
        result = await conn.execute(
            text(
                "SELECT id, task, provider, model, status, mode, cost_micros, input_tokens, "
                "request, response, error_kind FROM model_calls WHERE task = :t "
                "ORDER BY created_at, id"
            ),
            {"t": EMBED_TASK},
        )
        return list(result.all())


async def test_recorded_mode_indexing_is_metered_offline(
    pol_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    org = await new_org(owner_engine)
    metered = MeteredEmbedder(HashingEmbedder(), make_router(pol_engine, mode=ModelMode.RECORDED))
    with pytest.raises(EmbeddingUnavailable):
        await metered.embed(["no org"], purpose="query")
    row = await add_policy(
        pol_engine, org.id, name="spend_policy", title="Spend", pages=SPEND_POLICY, embedder=metered
    )
    async with session_for(pol_engine, org.id) as s, s.begin():
        docs = [d.id for d in await active_policy_documents(s)]
        hits = await hybrid_search(
            s,
            kind=POLICY_KIND,
            query="software purchase IT approval",
            source_ids=docs,
            embedder=metered,
        )
    assert hits
    assert hits[0].source_id == row.id
    rows = await calls(owner_engine, org.id)
    assert [(r.provider, r.model, r.status, r.mode, r.cost_micros) for r in rows] == [
        ("local", "pairly-hashing", "ok", "recorded", 0),
        ("local", "pairly-hashing", "ok", "recorded", 0),
    ]
    # Encrypted at rest; the plaintext holds hashes only, never texts or vectors.
    assert rows[0].request.startswith("v1:")
    assert rows[0].response.startswith("v1:")
    async with session_for(pol_engine, org.id) as s, s.begin():
        stored = await DatabaseCallStore().get(s, rows[0].id)
    assert stored is not None
    texts = stored.request["inputs"]["texts"]
    assert texts
    assert set(texts[0]) == {"sha256", "chars"}
    assert "Software" not in str(stored.request)
    assert stored.request["inputs"]["purpose"] == "document"
    assert set(stored.response) == {"count", "dimensions", "missing", "vectors_sha256"}
    assert stored.response["count"] == row.chunk_count
    query_vector = await HashingEmbedder().embed(["software purchase IT approval"], purpose="query")
    async with session_for(pol_engine, org.id) as s, s.begin():
        query_call = await DatabaseCallStore().get(s, rows[1].id)
    assert query_call is not None
    assert query_call.response["vectors_sha256"] == sha256_hex(canonical_json(query_vector))


async def test_priced_embeddings_are_billed_and_settled(
    pol_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    org = await new_org(owner_engine)
    router = make_router(pol_engine, mode=ModelMode.LIVE)
    vectors = await MeteredEmbedder(PricedFake(tokens=1000), router).embed_for_org(
        org.id, ["hello world", "second text"], purpose="document"
    )
    assert len(vectors) == 2
    (row,) = await calls(owner_engine, org.id)
    # 1000 tokens at 0.02 USD per million tokens = 20 micro-dollars.
    assert (row.status, row.input_tokens, row.cost_micros, row.provider) == (
        "ok",
        1000,
        20,
        "openai",
    )
    async with session_for(pol_engine, org.id) as s, s.begin():
        left = await s.scalar(select(LLMBudgetReservation.id).limit(1))
    assert left is None


async def test_budget_and_provider_failures_fail_closed(
    pol_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    org = await new_org(owner_engine)
    broke = MeteredEmbedder(PricedFake(), make_router(pol_engine, mode=ModelMode.LIVE, limit=0))
    with pytest.raises(EmbeddingUnavailable, match="model_budget_exceeded"):
        await broke.embed_for_org(org.id, ["over budget"], purpose="query")
    assert await calls(owner_engine, org.id) == []

    down = MeteredEmbedder(PricedFake(fail=True), make_router(pol_engine, mode=ModelMode.LIVE))
    with pytest.raises(EmbeddingUnavailable):
        await down.embed_for_org(org.id, ["provider down"], purpose="query")
    (row,) = await calls(owner_engine, org.id)
    assert (row.status, row.error_kind, row.cost_micros) == (
        "provider_error",
        "EmbeddingUnavailable",
        0,
    )
    async with session_for(pol_engine, org.id) as s, s.begin():
        left = await s.scalar(select(LLMBudgetReservation.id).limit(1))
    assert left is None

    # Indexing with an embedder that is over budget fails closed, nothing indexed.
    org2 = await new_org(owner_engine)
    row2 = await add_policy(
        pol_engine,
        org2.id,
        name="spend_policy",
        title="Spend",
        pages=SPEND_POLICY,
        embedder=broke,
        index=False,
    )
    async with session_for(pol_engine, org2.id) as s, s.begin():
        outcome = await index_normalized(
            s, policy_document_id=row2.id, normalized=build_document(SPEND_POLICY), embedder=broke
        )
    assert (outcome.status, outcome.reason) == ("failed", "embedding_unavailable")


class PartlyFailing(PricedFake):
    """Billed for the first batch, then the provider fails (a later batch errors)."""

    def __init__(self, *, tokens: int, completed: int, bound: int = 0) -> None:
        super().__init__(tokens)
        self.completed = completed
        self.bound = bound

    async def embed_batch(self, texts: Sequence[str], *, purpose: Any) -> EmbeddingBatch:
        raise PartialEmbeddingError(
            "embedding request failed: APIError",
            PartialUsage(
                input_tokens=self.tokens, input_bound=self.bound, completed=self.completed
            ),
        )


async def test_partial_failure_is_billed_and_still_raises(
    pol_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    org = await new_org(owner_engine)
    router = make_router(pol_engine, mode=ModelMode.LIVE)
    with pytest.raises(EmbeddingUnavailable):
        await MeteredEmbedder(PartlyFailing(tokens=1000, completed=96), router).embed_for_org(
            org.id, ["first text", "second text"], purpose="document"
        )
    (row,) = await calls(owner_engine, org.id)
    # The successful batches cost 1000 tokens at 0.02 USD per million = 20 micro-dollars.
    assert (row.status, row.error_kind, row.input_tokens, row.cost_micros) == (
        "partial_error",
        "PartialEmbeddingError",
        1000,
        20,
    )
    async with session_for(pol_engine, org.id) as s, s.begin():
        stored = await DatabaseCallStore().get(s, row.id)
        left = await s.scalar(select(LLMBudgetReservation.id).limit(1))
    assert stored is not None
    assert stored.response == {"count": 2, "completed": 96}
    assert left is None

    # No usage reported: the successful part is billed at its byte bound (never the
    # whole request's bound, never nothing).
    org2 = await new_org(owner_engine)
    with pytest.raises(EmbeddingUnavailable):
        await MeteredEmbedder(PartlyFailing(tokens=0, completed=1, bound=10), router).embed_for_org(
            org2.id, ["0123456789", "abcdefghijklmnop"], purpose="document"
        )
    (row2,) = await calls(owner_engine, org2.id)
    assert (row2.status, row2.input_tokens) == ("partial_error", 10)

    # Nothing completed: an ordinary provider error at zero cost.
    org3 = await new_org(owner_engine)
    with pytest.raises(EmbeddingUnavailable):
        await MeteredEmbedder(PartlyFailing(tokens=0, completed=0), router).embed_for_org(
            org3.id, ["x"], purpose="query"
        )
    (row3,) = await calls(owner_engine, org3.id)
    assert (row3.status, row3.cost_micros) == ("provider_error", 0)


async def test_embedding_calls_are_not_replayed(
    pol_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    org = await new_org(owner_engine)
    router = make_router(pol_engine, mode=ModelMode.RECORDED)
    await MeteredEmbedder(HashingEmbedder(), router).embed_for_org(org.id, ["x y"], purpose="query")
    (row,) = await calls(owner_engine, org.id)

    class Empty(BaseModel):
        pass

    with pytest.raises(ValueError, match="not replayed"):
        await router.replay(row.id, org_id=org.id, schema=Empty)
