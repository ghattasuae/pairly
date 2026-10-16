# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Policy engine tables (migration ``0009``).

* ``rule_results``: one row per rule (or per policy finding) per evaluation, with its
  outcome, reason, the facts it read, its evidence and the exact policy version. Append
  only: SELECT and INSERT for the application role, and the ``workflow_reject_change``
  trigger (0006) rejects UPDATE, DELETE and TRUNCATE for every role.
* ``policy_documents``: an uploaded policy document version (a ``documents`` row) and
  its indexing state. SELECT, INSERT and UPDATE (status only: a trigger keeps every
  identifying column fixed and makes ``archived`` final); no DELETE. Migration 0010
  adds the guardrail findings and ``needs_review``. Migration 0013 adds the guardrail
  override: ``needs_review`` becomes ``pending`` (then indexed) only with a recorded
  override, approved by a second admin when the findings are high severity.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped, Timestamped

_UUID_DEFAULT = text("gen_random_uuid()")


class ResultSource(StrEnum):
    RULES = "rules"
    RAG = "rag"


RESULT_OUTCOMES = ("pass", "fail", "needs_review", "error", "no_policy_found")


class PolicyDocumentStatus(StrEnum):
    PENDING = "pending"  # uploaded, waiting for scan and parse
    INDEXED = "indexed"  # chunks stored, used by policy checks
    FAILED = "failed"  # could not be indexed (parse failure, empty, embedding error)
    # Guardrails found a high-severity instruction (or could not run): not indexed and
    # not active until an admin overrides it with a written reason (two admins for
    # high-severity findings), or uploads a corrected file (a new version).
    NEEDS_REVIEW = "needs_review"
    ARCHIVED = "archived"  # no longer used; kept for the record


class RuleResult(Base, OrgScoped, Timestamped):
    __tablename__ = "rule_results"
    __table_args__ = (
        CheckConstraint("source IN ('rules', 'rag')", name="source_valid"),
        CheckConstraint(
            "outcome IN ('pass', 'fail', 'needs_review', 'error', 'no_policy_found')",
            name="outcome_valid",
        ),
        CheckConstraint(
            "source <> 'rules' OR (policy_version_id IS NOT NULL AND policy_version IS NOT NULL)",
            name="rules_have_version",
        ),
        CheckConstraint("created_by_type IN ('user', 'agent', 'system')", name="creator_valid"),
        Index("ix_rule_results_evaluation", "org_id", "evaluation_id"),
        Index("ix_rule_results_subject", "org_id", "subject_type", "subject_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    evaluation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    policy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("policy_versions.id", ondelete="RESTRICT"), nullable=True
    )
    policy_name: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reason_ar: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    facts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    evidence: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    model_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    as_of: Mapped[str | None] = mapped_column(String(10), nullable=True)
    workflow: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subject_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(128), nullable=False)


class PolicyDocument(Base, OrgScoped, Timestamped):
    __tablename__ = "policy_documents"
    __table_args__ = (
        UniqueConstraint("org_id", "name", "version", name="uq_policy_documents_version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("name ~ '^[a-z][a-z0-9_.]{0,127}$'", name="name_format"),
        CheckConstraint(
            "status IN ('pending', 'indexed', 'failed', 'needs_review', 'archived')",
            name="status_valid",
        ),
        CheckConstraint("created_by_type IN ('user', 'agent', 'system')", name="creator_valid"),
        CheckConstraint(
            "jsonb_typeof(guardrail_findings) = 'array'", name="guardrail_findings_array"
        ),
        CheckConstraint(
            "(override_requested_by IS NULL) = (override_requested_at IS NULL) "
            "AND (override_requested_by IS NULL) = (override_request_reason IS NULL)",
            name="override_request_complete",
        ),
        CheckConstraint(
            "(overridden_by IS NULL) = (overridden_at IS NULL) "
            "AND (overridden_by IS NULL) = (override_reason IS NULL)",
            name="override_complete",
        ),
        Index("ix_policy_documents_document_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PolicyDocumentStatus.PENDING.value
    )
    status_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Guardrail findings of the indexed content, shown to admins as warnings (0010).
    guardrail_findings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    guardrail_version: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Guardrail override of a ``needs_review`` document (migration 0013). When the
    # findings need two people, the first admin's request is kept here and a second,
    # different admin approves it (``overridden_*``). Write once: a trigger refuses any
    # change to a value already set.
    override_requested_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    override_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    override_request_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    overridden_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    overridden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
