# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Embedders, the vector literal, text helpers and RRF. No network: a mock transport."""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from pairly_core.llm.settings import LLMSettings, ModelMode
from pairly_core.retrieval.embeddings import (
    Embedder,
    EmbeddingUnavailable,
    HashingEmbedder,
    OpenAIEmbedder,
    PartialEmbeddingError,
    build_embedder,
)
from pairly_core.retrieval.search import SearchableKind, reciprocal_rank_fusion
from pairly_core.retrieval.text import fold, query_words, terms
from pairly_core.retrieval.vector import DIMENSIONS, Vector, from_pgvector, to_pgvector


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_hashing_embedder_is_deterministic_and_normalised() -> None:
    first, second = HashingEmbedder(), HashingEmbedder()
    text = "Software over 10,000 AED needs IT approval"
    [a] = await first.embed([text], purpose="document")
    [b] = await second.embed([text], purpose="query")
    assert a is not None
    assert b is not None
    assert a == b
    assert len(a) == DIMENSIONS
    assert math.isclose(math.sqrt(sum(v * v for v in a)), 1.0, rel_tol=1e-9)
    # Stable across processes and versions: pinned values.
    assert first.model_id == "pairly-hashing"
    assert first.version == "1-d1536"
    nonzero = [i for i, v in enumerate(a) if v != 0.0]
    assert nonzero == sorted(nonzero)
    assert len(nonzero) >= 6
    assert isinstance(first, Embedder)


async def test_hashing_similarity_tracks_shared_words() -> None:
    emb = HashingEmbedder()
    q, near, far = await emb.embed(
        [
            "software purchase IT approval",
            "Software purchases need approval from IT.",
            "Business class flights need director sign off.",
        ],
        purpose="document",
    )
    assert q is not None
    assert near is not None
    assert far is not None
    assert cosine(q, near) > emb.relevance_floor > cosine(q, far)


async def test_hashing_empty_text_has_no_vector() -> None:
    assert await HashingEmbedder().embed(["", "the of and"], purpose="query") == [None, None]


async def test_hashing_arabic_normalisation() -> None:
    emb = HashingEmbedder()
    a, b = await emb.embed(["الموافقة على الشراء", "موافقة شراء"], purpose="document")
    assert a is not None
    assert b is not None
    assert cosine(a, b) > 0.6


def _openai_handler(seen: list[dict[str, Any]], *, status: int = 200, dims: int = DIMENSIONS):
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append(body)
        if status != 200:
            return httpx2.Response(status, json={"error": {"message": "nope"}})
        data = [
            {"object": "embedding", "index": i, "embedding": [float(i + 1)] + [0.0] * (dims - 1)}
            for i, _ in enumerate(body["input"])
        ]
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "data": list(reversed(data)),
                "model": body["model"],
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            },
        )

    return handler


async def test_openai_embedder_batches_and_orders() -> None:
    seen: list[dict[str, Any]] = []
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(_openai_handler(seen)))
    emb = OpenAIEmbedder("sk-test", http_client=client, base_url="https://api.test/v1")
    texts = [f"text {i}" for i in range(100)] + [""]
    out = await emb.embed(texts, purpose="document")
    assert len(seen) == 2  # batches of 96
    assert seen[0]["model"] == "text-embedding-3-small"
    assert seen[0]["dimensions"] == DIMENSIONS
    assert out[-1] is None
    first = out[0]
    assert first is not None
    assert first[0] == 1.0
    assert emb.model_id == "openai/text-embedding-3-small"
    assert emb.version == "d1536"


async def test_openai_embedder_fails_closed() -> None:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(_openai_handler([], status=500)))
    emb = OpenAIEmbedder("sk-test", http_client=client, base_url="https://api.test/v1")
    emb._client = emb._client.with_options(max_retries=0)
    with pytest.raises(EmbeddingUnavailable):
        await emb.embed(["x"], purpose="query")
    bad = httpx2.AsyncClient(transport=httpx2.MockTransport(_openai_handler([], dims=8)))
    emb = OpenAIEmbedder("sk-test", http_client=bad, base_url="https://api.test/v1")
    with pytest.raises(EmbeddingUnavailable, match="shape"):
        await emb.embed(["x"], purpose="query")
    with pytest.raises(ValueError, match="key"):
        OpenAIEmbedder("")


async def test_openai_embedder_partial_failure_carries_usage() -> None:
    """A later batch fails: the error says what the successful batches consumed."""
    seen: list[dict[str, Any]] = []
    ok = _openai_handler(seen)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if len(seen) >= 1:
            seen.append({})
            return httpx2.Response(500, json={"error": {"message": "down"}})
        return ok(request)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    emb = OpenAIEmbedder("sk-test", http_client=client, base_url="https://api.test/v1")
    emb._client = emb._client.with_options(max_retries=0)
    texts = [f"text {i}" for i in range(100)]
    with pytest.raises(PartialEmbeddingError) as info:
        await emb.embed_batch(texts, purpose="document")
    usage = info.value.partial_usage
    assert isinstance(info.value, EmbeddingUnavailable)
    assert (usage.input_tokens, usage.completed) == (3, 96)
    assert usage.input_bound == sum(len(t.encode()) for t in texts[:96])

    # A failure on the very first batch consumed nothing: a plain EmbeddingUnavailable.
    first = httpx2.AsyncClient(transport=httpx2.MockTransport(_openai_handler([], status=500)))
    emb = OpenAIEmbedder("sk-test", http_client=first, base_url="https://api.test/v1")
    emb._client = emb._client.with_options(max_retries=0)
    with pytest.raises(EmbeddingUnavailable) as plain:
        await emb.embed_batch(texts, purpose="document")
    assert not isinstance(plain.value, PartialEmbeddingError)


def test_build_embedder_selection() -> None:
    key = SecretStr("sk-test")
    assert isinstance(build_embedder(LLMSettings(model_mode=ModelMode.LIVE)), HashingEmbedder)
    recorded = LLMSettings(model_mode=ModelMode.RECORDED, openai_api_key=key)
    assert isinstance(build_embedder(recorded), HashingEmbedder)
    live = LLMSettings(model_mode=ModelMode.LIVE, openai_api_key=key)
    assert isinstance(build_embedder(live), OpenAIEmbedder)


def test_vector_literal_round_trip_and_checks() -> None:
    assert to_pgvector([0.5, -1.0]) == "[0.5,-1.0]"
    assert from_pgvector("[0.5,-1]") == [0.5, -1.0]
    assert from_pgvector("[]") == []
    with pytest.raises(ValueError, match="finite"):
        to_pgvector([float("nan")])
    with pytest.raises(ValueError, match="pgvector"):
        from_pgvector("0.5")
    with pytest.raises(ValueError, match="dimensions"):
        Vector(0)
    process = Vector(2).bind_processor(None)
    assert process(None) is None
    with pytest.raises(ValueError, match="expected a vector"):
        process([1.0])
    assert Vector(2).get_col_spec() == "vector(2)"
    assert Vector(2).result_processor(None, None)("[1,2]") == [1.0, 2.0]


def test_text_helpers() -> None:
    assert fold("“Quoted”  text\u00a0here — ok") == '"quoted" text here - ok'
    assert fold("الْمُوافَقة") == fold("الموافقه")
    assert terms("The purchases are approved") == ["purchas", "approv"]
    assert terms("purchase approve") == terms("purchased approves")
    assert terms("الموافقة") == terms("موافقة")
    words = query_words("Software OR (approval) -IT AND the; DROP TABLE x_y")
    assert words == ["software", "approval", "it", "drop", "table", "xy"]
    assert all(w.isalnum() for w in words)


def test_rrf_and_kind() -> None:
    a, b, c = (uuid.uuid4() for _ in range(3))
    scores = reciprocal_rank_fusion([[a, b], [b, c]])
    assert scores[b] > scores[a] > scores[c]
    with pytest.raises(ValueError, match="kind"):
        SearchableKind("Bad Kind")
