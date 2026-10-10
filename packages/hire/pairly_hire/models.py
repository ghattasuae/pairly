# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The Hire tables (migration ``hire_0001``). All org scoped with Row Level Security forced.

* ``hire_jobs``: one row per job. Pay transparency: the salary range is stored per job
  and shown publicly only when ``show_salary`` is set. ``rubric_policy_id`` is the
  locked rubric version new applications are scored against; ``form_policy_id`` the
  application form version they answer. A job opens (``publish``) only with both.
* ``hire_rubric_drafts``: the editable draft of a job's rubric (AI draft or a person's
  edits), one per job. Locking copies it into an immutable policy version.
* ``hire_candidates``: one row per person (per email address) in the organisation.
  Personal fields are encrypted (``pairly_core.crypto``); email and phone also carry an
  HMAC blind index for exact lookups, and the name a list of blind indexes of its
  normalised words for duplicate hints. No plaintext personal data is stored.
* ``hire_applications``: one candidate applying to one job. Answers are encrypted. The
  AI-use consent (notice version, time, language) is stored with the application. A
  knockout answer or a duplicate hint sets ``needs_human_review`` with the reasons; no
  status here is ever set by a model.
* ``hire_org_settings``: per organisation: careers page visibility, how long rejected
  candidates are kept (default 12 months), the reply time promised in the
  acknowledgement.

Self-identification answers live in ``pairly_hire.selfid.models``, never here.

Statuses of an application (``ApplicationStatus``)::

    incomplete --(CV uploaded, submitted)--> received
    received --(a person rejects, later work packages)--> rejected
    received --(the candidate withdraws, later work packages)--> withdrawn
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

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
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedJSON, EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped

JOBS = "hire_jobs"
RUBRIC_DRAFTS = "hire_rubric_drafts"
CANDIDATES = "hire_candidates"
APPLICATIONS = "hire_applications"
ORG_SETTINGS = "hire_org_settings"

DEFAULT_RETENTION_MONTHS = 12
DEFAULT_RESPONSE_DAYS = 10


class JobStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    PAUSED = "paused"
    CLOSED = "closed"


class WorkModel(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    TEMPORARY = "temporary"
    INTERNSHIP = "internship"


class SalaryPeriod(StrEnum):
    YEAR = "year"
    MONTH = "month"
    HOUR = "hour"


class ApplicationStatus(StrEnum):
    INCOMPLETE = "incomplete"
    RECEIVED = "received"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


def _in(values: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _jsonb_list() -> Mapped[list[Any]]:
    return mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))


class HireJob(Base, OrgScoped):
    __tablename__ = JOBS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hire_jobs_org_id_id"),
        UniqueConstraint("org_id", "slug", name="uq_hire_jobs_org_id_slug"),
        CheckConstraint(f"status IN ({_in(JobStatus)})", name="status_valid"),
        CheckConstraint(f"work_model IN ({_in(WorkModel)})", name="work_model_valid"),
        CheckConstraint(
            f"employment_type IN ({_in(EmploymentType)})", name="employment_type_valid"
        ),
        CheckConstraint(
            f"salary_period IS NULL OR salary_period IN ({_in(SalaryPeriod)})",
            name="salary_period_valid",
        ),
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{0,79}$'", name="slug_format"),
        CheckConstraint(
            "salary_currency IS NULL OR salary_currency ~ '^[A-Z]{3}$'", name="currency_format"
        ),
        CheckConstraint("country IS NULL OR country ~ '^[A-Z]{2}$'", name="country_format"),
        CheckConstraint(
            "(salary_min IS NULL OR salary_min >= 0) AND (salary_max IS NULL OR salary_max >= 0)",
            name="salary_non_negative",
        ),
        CheckConstraint(
            "salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max",
            name="salary_range_order",
        ),
        # A shown salary needs a range, a currency and a period.
        CheckConstraint(
            "NOT show_salary OR (salary_min IS NOT NULL AND salary_max IS NOT NULL "
            "AND salary_currency IS NOT NULL AND salary_period IS NOT NULL)",
            name="shown_salary_complete",
        ),
        # An open job always has a locked rubric and a form version.
        CheckConstraint(
            "status <> 'open' OR (rubric_policy_id IS NOT NULL AND form_policy_id IS NOT NULL)",
            name="open_needs_rubric_and_form",
        ),
        CheckConstraint("cardinality(co_recruiter_ids) <= 10", name="co_recruiters_limit"),
        Index("ix_hire_jobs_status", "org_id", "status", "published_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    title_ar: Mapped[str] = mapped_column(
        String(160), nullable=False, default="", server_default=""
    )
    team: Mapped[str | None] = mapped_column(String(120), nullable=True)
    location: Mapped[str] = mapped_column(String(160), nullable=False)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    work_model: Mapped[str] = mapped_column(String(16), nullable=False)
    employment_type: Mapped[str] = mapped_column(String(16), nullable=False)
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    salary_period: Mapped[str | None] = mapped_column(String(8), nullable=True)
    show_salary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    description_en: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    description_ar: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=JobStatus.DRAFT.value)
    rubric_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("policy_versions.id", ondelete="RESTRICT"), nullable=True
    )
    form_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("policy_versions.id", ondelete="RESTRICT"), nullable=True
    )
    # The person who created the job (NULL for jobs a demo seed created).
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    # The owner recruiter (defaults to the creator) and optional co-recruiters: the
    # people told about candidate replies, uploads and SLA alerts (hire_0006).
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    co_recruiter_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, default=list, server_default=text("'{}'::uuid[]")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class HireRubricDraft(Base, OrgScoped):
    __tablename__ = RUBRIC_DRAFTS
    __table_args__ = (
        UniqueConstraint("org_id", "job_id", name="uq_hire_rubric_drafts_job"),
        ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_rubric_drafts_job_org",
        ),
        CheckConstraint("source IN ('ai', 'person')", name="source_valid"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # Canonical rubric YAML (``Rubric.to_yaml``), always valid.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    # The model call behind an AI draft (``model_calls``).
    model_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class HireCandidate(Base, OrgScoped):
    __tablename__ = CANDIDATES
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hire_candidates_org_id_id"),
        UniqueConstraint("org_id", "email_bidx", name="uq_hire_candidates_org_id_email_bidx"),
        CheckConstraint("email_bidx ~ '^[0-9a-f]{64}$'", name="email_bidx_format"),
        CheckConstraint(
            "phone_bidx IS NULL OR phone_bidx ~ '^[0-9a-f]{64}$'", name="phone_bidx_format"
        ),
        CheckConstraint("locale IN ('en', 'ar')", name="locale_valid"),
        Index("ix_hire_candidates_phone_bidx", "org_id", "phone_bidx"),
        Index("ix_hire_candidates_name_bidx", "name_bidx", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    email_bidx: Mapped[str] = mapped_column(String(64), nullable=False)
    full_name: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    # Blind indexes of the normalised words of the name (duplicate hints, never search).
    name_bidx: Mapped[list[str]] = _jsonb_list()
    phone: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    phone_bidx: Mapped[str | None] = mapped_column(String(64), nullable=True)
    location: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    profile_url: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    locale: Mapped[str] = mapped_column(String(2), nullable=False, default="en")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class HireApplication(Base, OrgScoped):
    __tablename__ = APPLICATIONS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hire_applications_org_id_id"),
        UniqueConstraint("org_id", "reference", name="uq_hire_applications_org_id_reference"),
        UniqueConstraint("upload_token_hash", name="uq_hire_applications_upload_token_hash"),
        ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_applications_job_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "candidate_id"],
            ["hire_candidates.org_id", "hire_candidates.id"],
            ondelete="RESTRICT",
            name="fk_hire_applications_candidate_org",
        ),
        CheckConstraint(f"status IN ({_in(ApplicationStatus)})", name="status_valid"),
        CheckConstraint("reference ~ '^[A-Z0-9]{4}-[A-Z0-9]{4}$'", name="reference_format"),
        CheckConstraint("consent_locale IN ('en', 'ar')", name="consent_locale_valid"),
        CheckConstraint(
            "upload_token_hash IS NULL OR upload_token_hash ~ '^[0-9a-f]{64}$'",
            name="upload_token_hash_format",
        ),
        # Past ``incomplete`` an application always has its CV and a submission time.
        CheckConstraint(
            "status = 'incomplete' OR (cv_document_id IS NOT NULL AND submitted_at IS NOT NULL)",
            name="submitted_complete",
        ),
        CheckConstraint(
            "status <> 'rejected' OR (rejected_at IS NOT NULL AND retention_until IS NOT NULL)",
            name="rejected_has_retention",
        ),
        CheckConstraint(
            "(identity_revealed_at IS NULL) = (identity_revealed_by IS NULL)",
            name="identity_revealed_pair",
        ),
        CheckConstraint(
            "identity_revealed_by IS NULL OR identity_revealed_by ~ "
            "'^(shortlist|stage:[a-z][a-z0-9_]{0,31})$'",
            name="identity_revealed_by_format",
        ),
        Index("ix_hire_applications_job", "org_id", "job_id", "status", "submitted_at"),
        Index("ix_hire_applications_candidate", "org_id", "candidate_id"),
        Index("ix_hire_applications_cv", "org_id", "cv_document_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # Short public reference for the candidate (acknowledgement email), e.g. 7KQ2-M9XD.
    reference: Mapped[str] = mapped_column(String(9), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ApplicationStatus.INCOMPLETE.value
    )
    rubric_policy_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("policy_versions.id", ondelete="RESTRICT"), nullable=False
    )
    form_policy_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("policy_versions.id", ondelete="RESTRICT"), nullable=False
    )
    # ``{"questions": {...}, "knockouts": {...}}``. Never self-identification answers.
    answers: Mapped[Sealed] = mapped_column(EncryptedJSON(), nullable=False)
    # The contact details sent with this application (full name, email, phone, location,
    # profile link). The candidate row keeps the details first seen for the address and
    # is never overwritten by a public form, so nobody who knows an address can change
    # another person's details; a person compares the two when they differ.
    contact: Mapped[Sealed] = mapped_column(EncryptedJSON(), nullable=False)
    review_reasons: Mapped[list[str]] = _jsonb_list()
    needs_human_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # ``[{"candidate_id", "application_id", "signals": [...]}]``: hints for a person.
    duplicate_signals: Mapped[list[dict[str, Any]]] = _jsonb_list()
    consent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consent_locale: Mapped[str] = mapped_column(String(2), nullable=False)
    cv_document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )
    # Extra files by form field id: ``{"portfolio": "<document id>"}``.
    files: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # SHA-256 of the one-time token the candidate's browser uses to upload files and
    # submit, until the application is submitted (then cleared).
    upload_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    upload_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="careers", server_default="careers"
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When a rejected application becomes due for deletion (``pairly_hire.retention``).
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Blind mode: the one reveal state of this application (``pairly_hire.reveal``). Set
    # once, by the shortlist approval or a move into a stage that reveals identity,
    # whichever comes first; never cleared. ``shortlist`` or ``stage:<key>``.
    identity_revealed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    identity_revealed_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class HireOrgSettings(Base, OrgScoped):
    __tablename__ = ORG_SETTINGS
    __table_args__ = (
        UniqueConstraint("org_id", name="uq_hire_org_settings_org_id"),
        CheckConstraint(
            "rejected_retention_months BETWEEN 1 AND 120", name="retention_months_range"
        ),
        CheckConstraint("response_days BETWEEN 1 AND 90", name="response_days_range"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    careers_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    rejected_retention_months: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_RETENTION_MONTHS,
        server_default=str(DEFAULT_RETENTION_MONTHS),
    )
    response_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_RESPONSE_DAYS,
        server_default=str(DEFAULT_RESPONSE_DAYS),
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = _created()
