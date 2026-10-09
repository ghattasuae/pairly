# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Decision record tables (migration ``0012``). Every table is org scoped, RLS forced.

* ``decision_records``: append only. One row per AI step or human action, with a stable
  id ``dec_<26 Crockford base32>``. A human acting on an earlier record writes a new
  row whose ``supersedes`` points at it; a record is never changed. ``sensitive`` holds
  the personal data of the record (field snapshot, facts, quotes, human reasons),
  encrypted with ``pairly_core.crypto``. ``content_hash`` covers every field, the
  decrypted ``sensitive`` included, and is also written to the audit chain.
* ``decision_replays``: append only. One row per replay with its result.
* ``appeals``: a request for human review of one decision. Status changes are allowed
  (``open`` to ``resolved``), never after resolution.
* ``compliance_exports``: the compliance pack jobs and where their zip is stored.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedJSON, EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped

_UUID_DEFAULT = text("gen_random_uuid()")
DECISION_ID_PATTERN = r"^dec_[0-9A-HJKMNP-TV-Z]{26}$"


def _in(values: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


class DecisionKind(StrEnum):
    SCORE = "score"
    SHORTLIST_SUGGESTION = "shortlist_suggestion"
    FRAUD_FLAG = "fraud_flag"
    RISK_SCORE = "risk_score"
    EXTRACTION = "extraction"
    EXTRACTION_CORRECTION = "extraction_correction"
    POLICY_CHECK = "policy_check"
    APPROVAL = "approval"
    FINAL_ACTION = "final_action"
    APPEAL_OUTCOME = "appeal_outcome"
    # A person changed what an AI step produced (e.g. a screening score), with a reason.
    HUMAN_OVERRIDE = "human_override"


# Kinds that record what a person did (the rest record what an AI step produced).
HUMAN_KINDS = frozenset(
    {
        DecisionKind.EXTRACTION_CORRECTION,
        DecisionKind.APPROVAL,
        DecisionKind.FINAL_ACTION,
        DecisionKind.APPEAL_OUTCOME,
        DecisionKind.HUMAN_OVERRIDE,
    }
)
# Human kinds whose result is still deterministic from the stored inputs: replay runs
# their handler (never a model, never the person) instead of refusing.
REPLAYABLE_HUMAN_KINDS = frozenset({DecisionKind.HUMAN_OVERRIDE})


class ReplayMode(StrEnum):
    RECORDED = "recorded"
    LIVE = "live"


class ReplayStatus(StrEnum):
    MATCH = "match"
    DIFFERS = "differs"
    FAILED = "failed"


class AppealStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class AppealOutcome(StrEnum):
    UPHELD = "upheld"
    OVERTURNED = "overturned"
    PARTIALLY_OVERTURNED = "partially_overturned"


class RequesterType(StrEnum):
    CANDIDATE = "candidate"
    VENDOR = "vendor"
    STAFF = "staff"


class ExportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class DecisionRecord(Base, OrgScoped):
    __tablename__ = "decision_records"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_decision_records_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "supersedes"],
            ["decision_records.org_id", "decision_records.id"],
            ondelete="RESTRICT",
            name="fk_decision_records_supersedes",
        ),
        CheckConstraint(f"id ~ '{DECISION_ID_PATTERN}'", name="id_format"),
        CheckConstraint(f"kind IN ({_in(DecisionKind)})", name="kind_valid"),
        CheckConstraint("workflow ~ '^[a-z][a-z0-9_]{0,31}$'", name="workflow_format"),
        CheckConstraint("actor_type IN ('user', 'agent', 'system', 'api_key')", name="actor_valid"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_format"),
        CheckConstraint("supersedes IS NULL OR supersedes <> id", name="not_self"),
        Index("ix_decision_records_entity", "org_id", "entity_type", "entity_id", "created_at"),
        Index("ix_decision_records_org_id_decided_at", "org_id", "decided_at"),
        # A record is superseded at most once: the history of an entity is one line.
        Index(
            "uq_decision_records_supersedes",
            "org_id",
            "supersedes",
            unique=True,
            postgresql_where=text("supersedes IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    workflow: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Which deterministic re-evaluation replay runs (``pairly_core.decisions.replay``).
    replay_handler: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # References and hashes only (document ids and SHA-256, extraction or evaluation ids).
    inputs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # [{id, kind, name, version, hash}] of every policy version applied.
    policy_versions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    rubric_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("policy_versions.id", ondelete="RESTRICT"), nullable=True
    )
    # [{task, version, hash}] of every prompt used.
    prompt_versions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    model_call_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, server_default=text("'{}'::uuid[]")
    )
    # Provider, model and settings of the accepted model call, when there was one.
    model: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # The AI output (or the human action) without personal data.
    output: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Quote locations, rule results, risk signals, guardrail findings (no personal data).
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Personal data: field snapshot, facts, quote text, the human reason. Encrypted.
    sensitive: Mapped[Sealed | None] = mapped_column(EncryptedJSON(), nullable=True)
    final_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    audit_event_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, server_default=text("'{}'::uuid[]")
    )
    approval_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, server_default=text("'{}'::uuid[]")
    )
    supersedes: Mapped[str | None] = mapped_column(String(30), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DecisionReplay(Base, OrgScoped):
    __tablename__ = "decision_replays"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "decision_id"],
            ["decision_records.org_id", "decision_records.id"],
            ondelete="RESTRICT",
            name="fk_decision_replays_decision",
        ),
        CheckConstraint(f"mode IN ({_in(ReplayMode)})", name="mode_valid"),
        CheckConstraint(f"status IN ({_in(ReplayStatus)})", name="status_valid"),
        CheckConstraint("requested_by_type IN ('user', 'agent', 'system')", name="requester_valid"),
        CheckConstraint("status <> 'failed' OR reason IS NOT NULL", name="failed_has_reason"),
        Index("ix_decision_replays_decision", "org_id", "decision_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    decision_id: Mapped[str] = mapped_column(String(30), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    # False when a live replay used a model without temperature 0 (or unknown settings).
    deterministic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    diff: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    requested_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Appeal(Base, OrgScoped):
    __tablename__ = "appeals"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_appeals_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "decision_id"],
            ["decision_records.org_id", "decision_records.id"],
            ondelete="RESTRICT",
            name="fk_appeals_decision",
        ),
        CheckConstraint(f"status IN ({_in(AppealStatus)})", name="status_valid"),
        CheckConstraint(
            f"outcome IS NULL OR outcome IN ({_in(AppealOutcome)})", name="outcome_valid"
        ),
        CheckConstraint(f"requester_type IN ({_in(RequesterType)})", name="requester_valid"),
        # A resolved appeal always has a written outcome, who decided and when.
        CheckConstraint(
            "(status = 'resolved') = (outcome IS NOT NULL AND outcome_text IS NOT NULL "
            "AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="resolution_complete",
        ),
        Index("ix_appeals_queue", "org_id", "status", "sla_due_at"),
        Index("ix_appeals_decision", "org_id", "decision_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    decision_id: Mapped[str] = mapped_column(String(30), nullable=False)
    requester_type: Mapped[str] = mapped_column(String(16), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # The staff member who filed it on the requester's behalf (portals come later).
    filed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reason: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    outcome_text: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome_decision_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    sla_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ComplianceExport(Base, OrgScoped):
    __tablename__ = "compliance_exports"
    __table_args__ = (
        CheckConstraint(f"status IN ({_in(ExportStatus)})", name="status_valid"),
        CheckConstraint("period_end > period_start", name="period_valid"),
        CheckConstraint("sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
        CheckConstraint(
            "manifest_sha256 IS NULL OR manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="manifest_sha256_format",
        ),
        Index("ix_compliance_exports_status", "org_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Empty means every workflow.
    workflows: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), nullable=False, server_default=text("'{}'::varchar[]")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
