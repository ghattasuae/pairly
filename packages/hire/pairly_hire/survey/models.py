# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Survey tables (migration ``hire_0016``). Org scoped, Row Level Security forced,
hidden from candidates' portal sessions; the application role gets SELECT, INSERT and
UPDATE, never DELETE.

* ``hire_survey_invites``: one per finished application: the outcome, the language,
  the hash of the link's secret, when it expires, ``status`` (``sent``, ``skipped``
  when the candidate turned update emails off, ``used``) and the day it was used.
* ``hire_survey_responses``: the answers, with the job, the outcome and the language
  for grouping, and the day (not the time) it was answered. No invite id, no candidate
  id, no application id, unless the candidate chose to attach their name
  (``named``): then ``candidate_id`` and ``application_id`` are set. The free text
  comment is encrypted.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped

INVITES: Final = "hire_survey_invites"
RESPONSES: Final = "hire_survey_responses"
OUTCOMES: Final = ("hired", "rejected", "withdrawn")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


class HireSurveyInvite(Base, OrgScoped):
    __tablename__ = INVITES
    __table_args__ = (
        UniqueConstraint("org_id", "application_id", name="uq_hire_survey_invites_application"),
        ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_survey_invites_application_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "candidate_id"],
            ["hire_candidates.org_id", "hire_candidates.id"],
            ondelete="RESTRICT",
            name="fk_hire_survey_invites_candidate_org",
        ),
        CheckConstraint("outcome IN ('hired', 'rejected', 'withdrawn')", name="outcome_valid"),
        CheckConstraint("status IN ('sent', 'skipped', 'used')", name="status_valid"),
        CheckConstraint("locale IN ('en', 'ar')", name="locale_valid"),
        CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_format"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    locale: Mapped[str] = mapped_column(String(2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HireSurveyResponse(Base, OrgScoped):
    __tablename__ = RESPONSES
    __table_args__ = (
        CheckConstraint("outcome IN ('hired', 'rejected', 'withdrawn')", name="outcome_valid"),
        CheckConstraint("locale IN ('en', 'ar')", name="locale_valid"),
        CheckConstraint("jsonb_typeof(answers) = 'object'", name="answers_object"),
        CheckConstraint(
            "named OR (candidate_id IS NULL AND application_id IS NULL)", name="anonymous"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    locale: Mapped[str] = mapped_column(String(2), nullable=False)
    answers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    comment: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    named: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    answered_on: Mapped[date] = mapped_column(Date, nullable=False)
