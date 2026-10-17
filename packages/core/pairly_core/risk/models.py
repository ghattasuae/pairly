# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Risk tables (migration ``0007``). Org scoped, RLS forced, append only.

* ``risk_assessments``: one combined assessment of one entity, with the policy version
  it used (``policy_version_id`` is NULL only when no policy existed, and then the
  assessment always requires a person), score, level, ``requires_human`` and reasons.
* ``risk_signals``: the signals behind it, with the weight the policy gave each.

The application role may only SELECT and INSERT; a trigger rejects UPDATE, DELETE and
TRUNCATE. A new assessment is a new row; history is never rewritten.
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
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped

_UUID_DEFAULT = text("gen_random_uuid()")
_LEVELS = "'info', 'low', 'medium', 'high', 'critical'"


class RiskAssessment(Base, OrgScoped):
    __tablename__ = "risk_assessments"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_risk_assessments_org_id_id"),
        CheckConstraint("workflow ~ '^[a-z][a-z0-9_]{0,31}$'", name="workflow_format"),
        CheckConstraint("score >= 0 AND score <= 1", name="score_range"),
        CheckConstraint(f"level IN ({_LEVELS})", name="level_valid"),
        # No policy: the assessment must require a person (fail closed).
        CheckConstraint("policy_version_id IS NOT NULL OR requires_human", name="policy_or_human"),
        CheckConstraint("created_by_type IN ('user', 'agent', 'system')", name="creator_valid"),
        Index("ix_risk_assessments_entity", "org_id", "entity_type", "entity_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    workflow: Mapped[str] = mapped_column(String(32), nullable=False)
    assessment_type: Mapped[str] = mapped_column(String(96), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("policy_versions.id", ondelete="RESTRICT"), nullable=True
    )
    policy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    requires_human: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RiskSignalRow(Base, OrgScoped):
    __tablename__ = "risk_signals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "assessment_id"],
            ["risk_assessments.org_id", "risk_assessments.id"],
            ondelete="RESTRICT",
            name="fk_risk_signals_assessment",
        ),
        UniqueConstraint("assessment_id", "position", name="uq_risk_signals_assessment_position"),
        CheckConstraint("score >= 0 AND score <= 1", name="score_range"),
        CheckConstraint("weight >= 0 AND weight <= 1", name="weight_range"),
        CheckConstraint("status IN ('ok', 'error')", name="status_valid"),
        CheckConstraint(f"severity IN ({_LEVELS})", name="severity_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    detector: Mapped[str] = mapped_column(String(96), nullable=False)
    detector_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    explanation_key: Mapped[str] = mapped_column(String(128), nullable=False)
