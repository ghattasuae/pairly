# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Text embeddings behind one small interface.

Anthropic has no embeddings API (its documentation points to Voyage AI). Pairly ships:

* ``OpenAIEmbedder``: OpenAI ``text-embedding-3-small`` at 1536 dimensions, using the
  OpenAI key the model router already reads. Built only with an explicit key; the SDK
  never picks one up from elsewhere.
* ``HashingEmbedder``: a deterministic, offline feature-hashing embedder (no model, no
  network). Used by tests, by keyless installs and in ``recorded`` model mode so
  ``make demo`` and replays never depend on a remote service. Its similarity is lexical
  (shared words and word pairs), which is enough for policy lookups on small corpora.

Every stored vector records ``model_id`` and ``version``; search compares a query only
with vectors of the same model and version, because vectors from different models are
not comparable. The column is ``vector(1536)``, so every embedder emits 1536 values.
"""

from __future__ import annotations

import hashlib
import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol, runtime_checkable

import httpx2
import openai

from pairly_core.llm.settings import LLMSettings, ModelMode
from pairly_core.llm.types import PartialUsage
from pairly_core.retrieval.text import terms
from pairly_core.retrieval.vector import DIMENSIONS

MAX_EMBED_CHARS: Final = 8000
Purpose = Literal["document", "query"]


class EmbeddingUnavailable(RuntimeError):  # noqa: N818 (mirrors ModelUnavailable)
    """Embeddings could not be computed. Callers fail closed (human review)."""


class PartialEmbeddingError(EmbeddingUnavailable):
    """A batched call failed after some batches succeeded (and were billed).

    ``partial_usage`` lets the model router record the cost of the successful batches
    (status ``partial_error``). The vectors themselves are discarded: the caller fails
    closed exactly as for any other ``EmbeddingUnavailable``.
    """

    def __init__(self, message: str, usage: PartialUsage) -> None:
        super().__init__(message)
        self.partial_usage = usage


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """Vectors of one ``embed_batch`` call and the input tokens the provider billed."""

    vectors: list[list[float] | None]
    input_tokens: int = 0


@runtime_checkable
class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    @property
    def relevance_floor(self) -> float:
        """Minimum cosine similarity for a vector hit to count as relevant."""
        ...

    async def embed(self, texts: Sequence[str], *, purpose: Purpose) -> list[list[float] | None]:
        """One vector per text, or ``None`` when a text has nothing to embed."""
        ...


def _normalize(vector: list[float]) -> list[float] | None:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return None
    return [v / norm for v in vector]


class HashingEmbedder:
    """Deterministic feature hashing over normalised words and word pairs.

    Same text, same vector, on every machine and Python version: the hash is keyed
    BLAKE2b, not Python's randomised ``hash``.
    """

    VERSION: Final = "1"

    def __init__(self, dimensions: int = DIMENSIONS, *, relevance_floor: float = 0.2) -> None:
        self._dimensions = dimensions
        self._floor = relevance_floor

    @property
    def model_id(self) -> str:
        return "pairly-hashing"

    @property
    def provider(self) -> str:
        return "local"

    @property
    def price_model(self) -> str | None:
        return None  # computed locally: free, but still metered (recorded)

    @property
    def version(self) -> str:
        return f"{self.VERSION}-d{self._dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def relevance_floor(self) -> float:
        return self._floor

    def _bucket(self, feature: str) -> tuple[int, float]:
        digest = hashlib.blake2b(
            feature.encode("utf-8"), digest_size=8, key=b"pairly-hash-v1"
        ).digest()
        number = int.from_bytes(digest, "big")
        return number % self._dimensions, (1.0 if number >> 63 else -1.0)

    def embed_one(self, text: str) -> list[float] | None:
        words = terms(text[: MAX_EMBED_CHARS * 4])
        vector = [0.0] * self._dimensions
        features: dict[str, float] = {}
        for word in words:
            features[f"w:{word}"] = features.get(f"w:{word}", 0.0) + 1.0
        for left, right in itertools.pairwise(words):
            key = f"b:{left} {right}"
            features[key] = features.get(key, 0.0) + 0.5
        for feature, weight in features.items():
            index, sign = self._bucket(feature)
            # Sublinear term frequency, so one repeated word cannot dominate.
            vector[index] += sign * (1.0 + math.log(weight)) if weight >= 1 else sign * weight
        return _normalize(vector)

    async def embed(self, texts: Sequence[str], *, purpose: Purpose) -> list[list[float] | None]:
        return [self.embed_one(t) for t in texts]

    async def embed_batch(self, texts: Sequence[str], *, purpose: Purpose) -> EmbeddingBatch:
        return EmbeddingBatch(await self.embed(texts, purpose=purpose), 0)


class OpenAIEmbedder:
    """OpenAI embeddings (``text-embedding-3-small``, 1536 dimensions)."""

    MODEL: Final = "text-embedding-3-small"
    BATCH: Final = 96

    def __init__(
        self,
        api_key: str,
        *,
        model: str = MODEL,
        dimensions: int = DIMENSIONS,
        timeout_seconds: float = 30.0,
        relevance_floor: float = 0.3,
        http_client: httpx2.AsyncClient | None = None,
        base_url: str | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("an API key is required")
        self._model = model
        self._dimensions = dimensions
        self._floor = relevance_floor
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            max_retries=2,
            timeout=timeout_seconds,
            http_client=http_client,
            base_url=base_url,
        )

    @property
    def model_id(self) -> str:
        return f"openai/{self._model}"

    @property
    def provider(self) -> str:
        return "openai"

    @property
    def price_model(self) -> str | None:
        return self._model

    @property
    def version(self) -> str:
        return f"d{self._dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def relevance_floor(self) -> float:
        return self._floor

    async def embed(self, texts: Sequence[str], *, purpose: Purpose) -> list[list[float] | None]:
        return (await self.embed_batch(texts, purpose=purpose)).vectors

    async def embed_batch(self, texts: Sequence[str], *, purpose: Purpose) -> EmbeddingBatch:
        out: list[list[float] | None] = [None] * len(texts)
        tokens = 0
        # What the successful batches consumed, for billing if a later batch fails.
        done_bound = 0
        done = 0
        pending = [(i, t[:MAX_EMBED_CHARS]) for i, t in enumerate(texts) if t.strip()]

        def fail(message: str) -> EmbeddingUnavailable:
            if done == 0:
                return EmbeddingUnavailable(message)
            return PartialEmbeddingError(
                message, PartialUsage(input_tokens=tokens, input_bound=done_bound, completed=done)
            )

        for start in range(0, len(pending), self.BATCH):
            batch = pending[start : start + self.BATCH]
            try:
                response = await self._client.embeddings.create(
                    model=self._model,
                    input=[t for _, t in batch],
                    dimensions=self._dimensions,
                    encoding_format="float",
                )
            except openai.OpenAIError as exc:
                raise fail(f"embedding request failed: {type(exc).__name__}") from None
            # The provider answered: this batch is billed, whatever we make of the answer.
            usage = getattr(response, "usage", None)
            tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            done_bound += sum(len(t.encode("utf-8", "surrogatepass")) for _, t in batch)
            done += len(batch)
            if len(response.data) != len(batch):
                raise fail("the embedding response has the wrong length")
            for item in response.data:
                if not 0 <= item.index < len(batch):
                    raise fail("the embedding response is malformed")
                vector = [float(v) for v in item.embedding]
                if len(vector) != self._dimensions or not all(math.isfinite(v) for v in vector):
                    raise fail("the embedding has the wrong shape")
                out[batch[item.index][0]] = _normalize(vector)
        return EmbeddingBatch(out, tokens)


def build_embedder(settings: LLMSettings) -> HashingEmbedder | OpenAIEmbedder:
    """OpenAI in ``live`` and ``record`` modes when a key is set, else the hashing fake.

    ``recorded`` mode stays offline and deterministic, like the model router.
    """
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
    if key and settings.model_mode in {ModelMode.LIVE, ModelMode.RECORD}:
        return OpenAIEmbedder(key)
    return HashingEmbedder()
