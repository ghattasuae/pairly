# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The screening tables (migration ``hire_0002``). Org scoped, Row Level Security forced.

* ``hire_screenings``: one row per screening run of an application. Public columns hold
  ids, statuses, digests, offsets and the overall score; everything read from the CV
  (the guardrail report with its evidence text, the parsed profile, the criterion
  scores with their quotes and "why not higher", the knockout answers, the flags) is
  in ``details``, encrypted. ``blind`` says whether the screening ran blind (what the
  scoring model read); whether reviewers may see who the candidate is now is the
  application's one reveal state (``pairly_hire.reveal``).
* ``hire_screening_settings``: per organisation: blind mode, whether university names
  are hidden, the gap a claim check calls unexplained, and the whole-document injection
  classifier mode (Hire default ``always``).

Statuses (``ScreeningStatus``)::

    processing --(asks a person)--> awaiting_review --(shortlist)--> shortlisted
                                                    --(not shortlisted)--> not_shortlisted
                                                    --(changes requested)--> returned
    processing --(no approval possible)--> failed

Nothing here is ever set by a model: every outcome after ``awaiting_review`` is a
person's decision through the approval.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
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
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedJSON, Sealed
from pairly_core.db import Base, OrgScoped

SCREENINGS: Final = "hire_screenings"
SCREENING_SETTINGS: Final = "hire_screening_settings"
DEFAULT_GAP_MONTHS: Final = 6
CLASSIFIER_MODES: Final = ("off", "sampled", "always")


class ScreeningStatus(StrEnum):
    PROCESSING = "processing"
    AWAITING_REVIEW = "awaiting_review"
    SHORTLISTED = "shortlisted"
    NOT_SHORTLISTED = "not_shortlisted"
    RETURNED = "returned"
    FAILED = "failed"


def _in(values: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


def _jsonb(default: str = "'[]'::jsonb") -> Any:
    return mapped_column(JSONB, nullable=False, server_default=text(default))


class HireScreening(Base, OrgScoped):
    __tablename__ = SCREENINGS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hire_screenings_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_screenings_application_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_screenings_job_org",
        ),
        CheckConstraint(f"status IN ({_in(ScreeningStatus)})", name="status_valid"),
        CheckConstraint(
            "overall_score IS NULL OR (overall_score >= 0 AND overall_score <= 100)",
            name="overall_range",
        ),
        CheckConstraint(
            "sanitized_sha256 IS NULL OR sanitized_sha256 ~ '^[0-9a-f]{64}$'",
            name="sanitized_sha256_format",
        ),
        CheckConstraint(
            "blinded_sha256 IS NULL OR blinded_sha256 ~ '^[0-9a-f]{64}$'",
            name="blinded_sha256_format",
        ),
        Index("ix_hire_screenings_application", "org_id", "application_id", "created_at"),
        Index("ix_hire_screenings_status", "org_id", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    # The locked rubric version the application is scored against (pinned on it).
    rubric_policy_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("policy_versions.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ScreeningStatus.PROCESSING.value
    )
    # The screening ran blind (brief 3.3.3); reviewers' view: ``pairly_hire.reveal``.
    blind: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    blind_config: Mapped[dict[str, Any]] = _jsonb("'{}'::jsonb")
    review_reasons: Mapped[list[str]] = _jsonb()
    sanitized_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    blinded_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Original offsets only, never text.
    removed_spans: Mapped[list[dict[str, Any]]] = _jsonb()
    blind_spans: Mapped[list[dict[str, Any]]] = _jsonb()
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    overall_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    # Ids, versions and counts of the model calls: classifier, CV parse, scoring.
    model_info: Mapped[dict[str, Any]] = _jsonb("'{}'::jsonb")
    # Flag codes, kinds and counts (the explanations live in ``details``).
    flag_codes: Mapped[list[str]] = _jsonb()
    # Encrypted: report, profile, scoring, knockouts, flags (see the module docstring).
    details: Mapped[Sealed | None] = mapped_column(EncryptedJSON(), nullable=True)
    score_decision_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    decision_ids: Mapped[list[str]] = _jsonb()
    approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HireScreeningSettings(Base, OrgScoped):
    __tablename__ = SCREENING_SETTINGS
    __table_args__ = (
        UniqueConstraint("org_id", name="uq_hire_screening_settings_org_id"),
        CheckConstraint("gap_months BETWEEN 1 AND 60", name="gap_months_range"),
        CheckConstraint(
            "classifier_mode IN ('off', 'sampled', 'always')", name="classifier_mode_valid"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    blind_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    blind_universities: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    gap_months: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_GAP_MONTHS, server_default=str(DEFAULT_GAP_MONTHS)
    )
    classifier_mode: Mapped[str] = mapped_column(
        String(8), nullable=False, default="always", server_default="always"
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
