# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Analytics tables. Org scoped, Row Level Security forced, hidden from candidates'
portal sessions; the application role gets SELECT, INSERT and UPDATE, never DELETE.

Migration ``hire_0017``, the counterfactual fairness runner (``analytics.fairness``):

* ``hire_fairness_runs``: one run over a sample of screened applications: how it
  started (``scheduled`` or ``manual``, and who), the model mode (``live`` or
  ``recorded``), the threshold, the counts and a summary. ``status``: ``running``,
  ``completed``, ``failed``.
* ``hire_fairness_results``: one counterfactual copy: the application and screening it
  copied, the dimension (``name``, ``gender``, ``nationality``), the synthetic values put
  in (never the real ones), which of them the redaction missed, whether the scoring
  model would read exactly the same input, and (live runs only) the two overall scores,
  their difference and whether it is above the threshold. ``model_status`` says
  ``pending_live_run`` when the model step was not measured.

Migration ``hire_0018``, calibration mode (``analytics.calibration``):

* ``hire_calibration_labels``: a person's score (0 to 4) for one criterion of one
  screened application, given without seeing the AI's score. One per person, screening
  and criterion; labelling again replaces it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped

FAIRNESS_RUNS: Final = "hire_fairness_runs"
FAIRNESS_RESULTS: Final = "hire_fairness_results"
CALIBRATION_LABELS: Final = "hire_calibration_labels"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


class HireFairnessRun(Base, OrgScoped):
    __tablename__ = FAIRNESS_RUNS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hire_fairness_runs_org_id_id"),
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="status_valid"),
        CheckConstraint("trigger IN ('scheduled', 'manual')", name="trigger_valid"),
        CheckConstraint("mode IN ('live', 'recorded')", name="mode_valid"),
        CheckConstraint("jsonb_typeof(summary) = 'object'", name="summary_object"),
        Index("ix_hire_fairness_runs_started", "org_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    threshold: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    applications: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    copies: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    flagged: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    started_by: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


class HireFairnessResult(Base, OrgScoped):
    __tablename__ = FAIRNESS_RESULTS
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "run_id"],
            ["hire_fairness_runs.org_id", "hire_fairness_runs.id"],
            ondelete="RESTRICT",
            name="fk_hire_fairness_results_run_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_fairness_results_application_org",
        ),
        CheckConstraint("dimension IN ('name', 'gender', 'nationality')", name="dimension_valid"),
        CheckConstraint("jsonb_typeof(inserted) = 'array'", name="inserted_array"),
        CheckConstraint("jsonb_typeof(leaked) = 'array'", name="leaked_array"),
        Index("ix_hire_fairness_results_run", "org_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    screening_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    dimension: Mapped[str] = mapped_column(String(16), nullable=False)
    inserted: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    leaked: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    redaction_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    input_identical: Mapped[bool] = mapped_column(Boolean, nullable=False)
    blind: Mapped[bool] = mapped_column(Boolean, nullable=False)
    model_status: Mapped[str] = mapped_column(String(48), nullable=False)
    control_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    copy_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    delta: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    flagged: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HireCalibrationLabel(Base, OrgScoped):
    __tablename__ = CALIBRATION_LABELS
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "screening_id",
            "criterion_id",
            "labelled_by",
            name="uq_hire_calibration_labels_person",
        ),
        ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_calibration_labels_application_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_calibration_labels_job_org",
        ),
        CheckConstraint("score BETWEEN 0 AND 4", name="score_range"),
        CheckConstraint("criterion_id ~ '^[a-z][a-z0-9_]{0,47}$'", name="criterion_format"),
        Index("ix_hire_calibration_labels_job", "org_id", "job_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    screening_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    rubric_policy_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    criterion_id: Mapped[str] = mapped_column(String(48), nullable=False)
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    labelled_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
