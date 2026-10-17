# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``document_batches`` and ``document_batch_items`` (migration 0025).

Org scoped, RLS forced, closed to portal sessions. The application role may SELECT,
INSERT and UPDATE (item outcomes and the batch's completion), never DELETE.

A batch is ``processing`` until every item has an outcome, then ``completed`` (and the
``batch.completed`` webhook event is written in that same transaction). An item is
``pending``, then one of ``accepted`` (a new document), ``duplicate`` (the same bytes
were already submitted to the workflow), ``rejected`` (too large, empty or a type the
workflow does not take) or ``failed`` (a URL that could not be fetched safely).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped, Timestamped


class DocumentBatch(Base, OrgScoped, Timestamped):
    __tablename__ = "document_batches"
    __table_args__ = (
        CheckConstraint("status IN ('processing', 'completed')", name="status_valid"),
        CheckConstraint("workflow ~ '^[a-z][a-z0-9_]{0,31}$'", name="workflow_format"),
        CheckConstraint("item_count BETWEEN 1 AND 100", name="item_count_range"),
        CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)", name="completed_pair"
        ),
        CheckConstraint(
            "created_by_type IN ('user', 'agent', 'api_key')", name="created_by_type_valid"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    workflow: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # The client's own labels (at most 4 KiB of JSON), returned as given.
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(64), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DocumentBatchItem(Base, OrgScoped):
    __tablename__ = "document_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "position", name="uq_document_batch_items_batch_position"),
        CheckConstraint("source IN ('file', 'url')", name="source_valid"),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'duplicate', 'rejected', 'failed')",
            name="status_valid",
        ),
        CheckConstraint("position BETWEEN 0 AND 99", name="position_range"),
        CheckConstraint("(source = 'url') = (url IS NOT NULL)", name="url_only_for_urls"),
        CheckConstraint("url IS NULL OR char_length(url) <= 2048", name="url_length"),
        CheckConstraint(
            "(status = 'pending') = (completed_at IS NULL)", name="completed_unless_pending"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("document_batches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )
    # What the workflow made of the document (``pay_invoice`` and its id), when it has
    # an intake (``pairly_core.batches.intake``).
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
