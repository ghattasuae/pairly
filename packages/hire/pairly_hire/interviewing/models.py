# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
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
* ``hire_scorecards`` (hire_0011): one interviewer's scorecard for one interview:
  ratings 1 to 4 per criterion (the criteria of the plan stage when it was created),
  short notes, an overall recommendation. ``draft`` until submitted; a submitted
  scorecard never changes (the API refuses, and a trigger refuses any change to its
  ratings, notes or recommendation). Reminder marks: ``reminded_2h_at``,
  ``reminded_24h_at`` and ``snoozed_until`` ("remind me later").
* ``hire_scorecard_links``: one-click links to a scorecard (``open``) or to snooze its
  reminders (``later``): single use, bound to the organisation, the interview and the
  interviewer; only a hash of the secret is stored.
* ``hire_scorecard_sessions``: what a used ``open`` link starts: a short session for
  that one interview and that interviewer (never a staff credential).
* ``hire_debriefs`` (hire_0012): the debrief summary of an application at a stage, keyed
  by the hash of the submitted scorecards it summarises, and the human decision taken on
  it (with its reason and its decision record id).
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
    Text,
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
SCORECARDS: Final = "hire_scorecards"
LINKS: Final = "hire_scorecard_links"
SESSIONS: Final = "hire_scorecard_sessions"
DEBRIEFS: Final = "hire_debriefs"
DECISIONS: Final = ("advance", "hold", "reject")
RECOMMENDATIONS: Final = ("strong_no", "no", "yes", "strong_yes")


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


def _interview_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["org_id", "interview_id"],
        ["hire_interviews.org_id", "hire_interviews.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_interview_org",
    )


class HireScorecard(Base, OrgScoped):
    __tablename__ = SCORECARDS
    __table_args__ = (
        UniqueConstraint(
            "org_id", "interview_id", "interviewer_id", name="uq_hire_scorecards_seat"
        ),
        UniqueConstraint("org_id", "id", name="uq_hire_scorecards_org_id_id"),
        _interview_fk(SCORECARDS),
        CheckConstraint("status IN ('draft', 'submitted')", name="status_valid"),
        CheckConstraint(
            "recommendation IS NULL OR recommendation IN ('strong_no', 'no', 'yes', 'strong_yes')",
            name="recommendation_valid",
        ),
        CheckConstraint("jsonb_typeof(ratings) = 'object'", name="ratings_object"),
        CheckConstraint("jsonb_typeof(criteria) = 'array'", name="criteria_array"),
        CheckConstraint("char_length(notes) <= 2000", name="notes_length"),
        CheckConstraint(
            "status = 'draft' OR (submitted_at IS NOT NULL AND recommendation IS NOT NULL)",
            name="submitted_complete",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    stage_key: Mapped[str] = mapped_column(String(32), nullable=False)
    interviewer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    criteria: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    ratings: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    recommendation: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminded_2h_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminded_24h_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class HireScorecardLink(Base, OrgScoped):
    __tablename__ = LINKS
    __table_args__ = (
        _interview_fk(LINKS),
        CheckConstraint("purpose IN ('open', 'later')", name="purpose_valid"),
        CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_format"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(8), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts()


class HireScorecardSession(Base, OrgScoped):
    __tablename__ = SESSIONS
    __table_args__ = (
        _interview_fk(SESSIONS),
        CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_format"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    link_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts()


class HireDebrief(Base, OrgScoped):
    __tablename__ = DEBRIEFS
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "application_id",
            "stage_key",
            "scorecards_hash",
            name="uq_hire_debriefs_inputs",
        ),
        ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_debriefs_application_org",
        ),
        CheckConstraint("jsonb_typeof(content) = 'object'", name="content_object"),
        CheckConstraint("scorecards_hash ~ '^[0-9a-f]{64}$'", name="scorecards_hash_format"),
        CheckConstraint("source IN ('model', 'template')", name="source_valid"),
        CheckConstraint(
            "decision IS NULL OR decision IN ('advance', 'hold', 'reject')", name="decision_valid"
        ),
        CheckConstraint(
            "decision IS NULL OR (decided_by IS NOT NULL AND decided_at IS NOT NULL "
            "AND char_length(decision_reason) >= 3)",
            name="decision_complete",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    stage_key: Mapped[str] = mapped_column(String(32), nullable=False)
    scorecards_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scorecard_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    from_recording: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_record_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _ts()
