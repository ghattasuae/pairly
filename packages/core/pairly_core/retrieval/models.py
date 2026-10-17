# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``search_chunks``: one generic, tenant-scoped index for hybrid search (migration 0009).

Each row is one chunk of text of one source (``kind`` plus ``source_id`` and
``source_version``), with its location (pages, section), two generated ``tsvector``
columns (``english`` with stemming, ``simple`` for Arabic and exact words) and an
optional embedding with the model and version that produced it.

RLS forced with the standard tenant policy. The application role may SELECT, INSERT and
DELETE (chunks are derived data, rebuilt when a source is re-indexed); there is no
UPDATE grant. Evidence that cites a chunk copies the quote, so deleting a chunk never
changes a recorded decision.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Computed,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped, Timestamped
from pairly_core.retrieval.vector import DIMENSIONS, Vector

MAX_CHUNK_CHARS = 4000


class SearchChunk(Base, OrgScoped, Timestamped):
    __tablename__ = "search_chunks"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "kind", "source_id", "chunk_index", name="uq_search_chunks_position"
        ),
        CheckConstraint("kind ~ '^[a-z][a-z0-9_]{0,31}$'", name="kind_format"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        CheckConstraint(f"char_length(text) BETWEEN 1 AND {MAX_CHUNK_CHARS}", name="text_length"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_format"),
        CheckConstraint(
            "(embedding IS NULL) = (embedding_model IS NULL)", name="embedding_has_model"
        ),
        Index("ix_search_chunks_source", "org_id", "kind", "source_id"),
        # Declared here (as created by migration 0009) so ``alembic check`` matches.
        Index("ix_search_chunks_tsv_en", "tsv_en", postgresql_using="gin"),
        Index("ix_search_chunks_tsv_simple", "tsv_simple", postgresql_using="gin"),
        Index(
            "ix_search_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=sql_text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section: Mapped[str | None] = mapped_column(String(500), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    tsv_en: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english'::regconfig, text)", persisted=True)
    )
    tsv_simple: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple'::regconfig, text)", persisted=True)
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(DIMENSIONS), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
