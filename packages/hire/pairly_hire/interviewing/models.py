# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Interview tables (migrations ``hire_0009`` and later). All org scoped, Row Level
Security forced, hidden from candidates' portal sessions; the application role gets
SELECT, INSERT and UPDATE, never DELETE.

* ``hire_interview_plans``: one per job: the plan (``plan.InterviewPlan`` as JSON) and its
  version, raised by every save (a save names the version it edited: no lost updates).
* ``hire_interview_kits`` (hire_0010): a generated interview kit for one application,
  stage and language, keyed by the hash of everything the model saw
  (``inputs_hash``): a new plan or a new screening makes a new kit. The kit holds no
  candidate text (the model never sees any), so it is blind safe as stored.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped

PLANS: Final = "hire_interview_plans"
KITS: Final = "hire_interview_kits"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class HireInterviewPlan(Base, OrgScoped):
    __tablename__ = PLANS
    __table_args__ = (
        UniqueConstraint("org_id", "job_id", name="uq_hire_interview_plans_job"),
        ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_interview_plans_job_org",
        ),
        CheckConstraint("jsonb_typeof(plan) = 'object'", name="plan_object"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class HireInterviewKit(Base, OrgScoped):
    __tablename__ = KITS
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "application_id",
            "stage_key",
            "locale",
            "inputs_hash",
            name="uq_hire_interview_kits_inputs",
        ),
        ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_interview_kits_application_org",
        ),
        CheckConstraint("locale IN ('en', 'ar')", name="locale_valid"),
        CheckConstraint("jsonb_typeof(items) = 'array'", name="items_array"),
        CheckConstraint("inputs_hash ~ '^[0-9a-f]{64}$'", name="inputs_hash_format"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    stage_key: Mapped[str] = mapped_column(String(32), nullable=False)
    locale: Mapped[str] = mapped_column(String(2), nullable=False)
    inputs_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_call_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, server_default=text("'{}'::uuid[]")
    )
    from_recording: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = _ts()
