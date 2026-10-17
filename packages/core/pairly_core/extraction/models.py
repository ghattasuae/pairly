# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Extraction tables (migration ``0007``). All org scoped, RLS forced, append only.

* ``extractions``: one row per extraction run of one document with one schema version:
  prompt task, version and hash, the model call ids, status and reasons, the field specs
  in force (so a correction is validated with the rules of that run, even after the
  schema changes), and an optional ``template_key`` (vendor or layout) for hints.
* ``extraction_fields``: one row per leaf field the run produced, with typed value as
  exact JSON, raw answer, confidence, threshold, flags and evidence spans.
* ``extraction_corrections``: a person's correction of one field path. Never updated:
  the current value of a field is its latest correction, else the extracted value.

The application role may only SELECT and INSERT; a trigger rejects UPDATE, DELETE and
TRUNCATE for every role. Children reference their extraction by ``(org_id, id)`` so a
row can never point at another organisation's extraction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped

_UUID_DEFAULT = text("gen_random_uuid()")
_WORKFLOW_CHECK = "workflow ~ '^[a-z][a-z0-9_]{0,31}$'"


class Extraction(Base, OrgScoped):
    __tablename__ = "extractions"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_extractions_org_id_id"),
        CheckConstraint(_WORKFLOW_CHECK, name="workflow_format"),
        CheckConstraint("status IN ('ok', 'needs_human')", name="status_valid"),
        CheckConstraint("created_by_type IN ('user', 'agent', 'system')", name="creator_valid"),
        Index("ix_extractions_org_id_document_id", "org_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    workflow: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_name: Mapped[str] = mapped_column(String(96), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    field_specs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    prompt_task: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_call_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, server_default=text("'{}'::uuid[]")
    )
    model_chain_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    from_recording: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    ignored_paths: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    template_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExtractionField(Base, OrgScoped):
    __tablename__ = "extraction_fields"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "extraction_id"],
            ["extractions.org_id", "extractions.id"],
            ondelete="RESTRICT",
            name="fk_extraction_fields_extraction",
        ),
        UniqueConstraint("extraction_id", "path", name="uq_extraction_fields_extraction_id_path"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("threshold >= 0 AND threshold <= 1", name="threshold_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    extraction_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(String(256), nullable=False)
    pattern: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    needs_correction: Mapped[bool] = mapped_column(Boolean, nullable=False)
    flags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    spans: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


class ExtractionCorrection(Base, OrgScoped):
    __tablename__ = "extraction_corrections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "extraction_id"],
            ["extractions.org_id", "extractions.id"],
            ondelete="RESTRICT",
            name="fk_extraction_corrections_extraction",
        ),
        CheckConstraint(_WORKFLOW_CHECK, name="workflow_format"),
        CheckConstraint("length(reason) BETWEEN 1 AND 1000", name="reason_length"),
        Index(
            "ix_extraction_corrections_hints",
            "org_id",
            "workflow",
            "schema_name",
            "template_key",
            "created_at",
        ),
        Index("ix_extraction_corrections_extraction", "org_id", "extraction_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    extraction_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    field_path: Mapped[str] = mapped_column(String(256), nullable=False)
    field_pattern: Mapped[str] = mapped_column(String(256), nullable=False)
    old_value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    corrected_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Copied from the extraction, so hints are one indexed query.
    workflow: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_name: Mapped[str] = mapped_column(String(96), nullable=False)
    template_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
