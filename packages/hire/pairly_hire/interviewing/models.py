# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Interview tables (migrations ``hire_0009`` and later). All org scoped, Row Level
Security forced, hidden from candidates' portal sessions; the application role gets
SELECT, INSERT and UPDATE, never DELETE.

* ``hire_interview_plans``: one per job: the plan (``plan.InterviewPlan`` as JSON) and its
  version, raised by every save (a save names the version it edited: no lost updates).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped

PLANS: Final = "hire_interview_plans"


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
