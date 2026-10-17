# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Scheduling tables (migration ``hire_0005``). All org scoped, Row Level Security forced;
the application role gets SELECT, INSERT and UPDATE, never DELETE.

* ``hire_scheduling_settings``: per organisation defaults (time zone, interview length,
  slot step, minimum notice, how far ahead slots are offered, how long a
  self-scheduling link lives, the fake calendar's seed).
* ``hire_interviewer_settings``: one row per person who interviews: time zone, working
  hours per weekday, buffers, at most so many interviews a day, holidays. A person
  without a row uses the organisation's defaults.
* ``hire_interviews``: one interview of one application at one stage. ``status``:
  ``proposed`` (a self-scheduling link was sent, no time yet), ``booked``,
  ``rescheduled`` (booked, moved at least once), ``cancelled``, ``completed``,
  ``no_show``. ``ics_uid`` and ``ics_sequence`` are the calendar identity sent in every
  invitation (the sequence grows with every change attendees must see).
  ``link_nonce`` makes the candidate's link single use per booking: every booking
  replaces it. The meeting link is encrypted (it lets anyone join).
* ``hire_interview_panelists``: who is on an interview's panel (``removed_at`` when a
  person is taken off it; rows are never deleted).
* ``hire_calendar_blocks``: the fake calendar's stored busy time (``kind`` ``seed``:
  generated per person and local day; ``event``: interviews written through it).
* ``hire_calendar_connections``: a person's own calendar account (Google or Microsoft
  365). The refresh token and the account address are encrypted, bound to the row.
* ``hire_meeting_integrations`` (``hire_0019``): an organisation's meeting-link app
  (Zoom Server-to-Server OAuth): account id, client id, client secret and host user,
  all encrypted, bound to the row. ``status`` ``active`` once a token was obtained with
  them, ``revoked`` when an administrator removed it (the secret is overwritten).
* ``hire_calendar_operations`` (``hire_0020``): one row per write to an external
  calendar or meeting provider, recorded in its own transaction before the call
  (``operations``): the provider, what was asked, our idempotency key, the provider's id
  once known, the intended state. ``pending`` until the interview's transaction commits
  (it marks the row ``done`` itself, so both commit together); a row still pending later
  is an orphan or a lost change, and the reconciliation job settles it (``reconciled``
  or ``failed``). No foreign key to the interview on purpose: the row is written while
  the interview is locked, or not even committed yet.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
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

from pairly_core.crypto.types import EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped

SETTINGS = "hire_scheduling_settings"
INTERVIEWERS = "hire_interviewer_settings"
INTERVIEWS = "hire_interviews"
PANELISTS = "hire_interview_panelists"
BLOCKS = "hire_calendar_blocks"
CONNECTIONS = "hire_calendar_connections"
MEETING_INTEGRATIONS = "hire_meeting_integrations"
OPERATIONS = "hire_calendar_operations"

STAGE_KEY_PATTERN: Final = "^[a-z][a-z0-9_]{0,31}$"
DEFAULT_TIMEZONE: Final = "Asia/Dubai"
DEFAULT_DURATION: Final = 45
DEFAULT_STEP: Final = 30
DEFAULT_NOTICE_HOURS: Final = 12
DEFAULT_HORIZON_DAYS: Final = 14
DEFAULT_LINK_DAYS: Final = 7
DEFAULT_SEED: Final = "pairly-demo"


class InterviewStatus(StrEnum):
    PROPOSED = "proposed"
    BOOKED = "booked"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


# Holding a time on the panel's calendars.
ACTIVE: Final = frozenset({InterviewStatus.BOOKED.value, InterviewStatus.RESCHEDULED.value})
# Still open: a link is out, or a time is held.
OPEN: Final = ACTIVE | {InterviewStatus.PROPOSED.value}


def _in(values: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _user_fk(nullable: bool = False) -> Mapped[Any]:
    return mapped_column(Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=nullable)


class HireSchedulingSettings(Base, OrgScoped):
    __tablename__ = SETTINGS
    __table_args__ = (
        UniqueConstraint("org_id", name="uq_hire_scheduling_settings_org_id"),
        CheckConstraint("default_duration_min BETWEEN 15 AND 240", name="duration_range"),
        CheckConstraint("slot_step_min IN (15, 30, 60)", name="step_valid"),
        CheckConstraint("min_notice_hours BETWEEN 0 AND 168", name="notice_range"),
        CheckConstraint("horizon_days BETWEEN 1 AND 60", name="horizon_range"),
        CheckConstraint("link_days BETWEEN 1 AND 30", name="link_days_range"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    default_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_TIMEZONE, server_default=DEFAULT_TIMEZONE
    )
    default_duration_min: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_DURATION, server_default=str(DEFAULT_DURATION)
    )
    slot_step_min: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_STEP, server_default=str(DEFAULT_STEP)
    )
    min_notice_hours: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_NOTICE_HOURS,
        server_default=str(DEFAULT_NOTICE_HOURS),
    )
    horizon_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_HORIZON_DAYS,
        server_default=str(DEFAULT_HORIZON_DAYS),
    )
    link_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_LINK_DAYS, server_default=str(DEFAULT_LINK_DAYS)
    )
    fake_seed: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_SEED, server_default=DEFAULT_SEED
    )
    updated_by: Mapped[uuid.UUID | None] = _user_fk(nullable=True)
    updated_at: Mapped[datetime] = _created()


class HireInterviewerSettings(Base, OrgScoped):
    __tablename__ = INTERVIEWERS
    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_hire_interviewer_settings_user"),
        CheckConstraint("jsonb_typeof(working_hours) = 'object'", name="working_hours_object"),
        CheckConstraint("jsonb_typeof(holidays) = 'array'", name="holidays_array"),
        CheckConstraint("buffer_before_min BETWEEN 0 AND 240", name="buffer_before_range"),
        CheckConstraint("buffer_after_min BETWEEN 0 AND 240", name="buffer_after_range"),
        CheckConstraint(
            "max_per_day IS NULL OR max_per_day BETWEEN 0 AND 24", name="max_per_day_range"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    # {"mon": [["09:00", "17:00"]], ...} (``pairly_core.scheduling.availability``).
    working_hours: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    buffer_before_min: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    buffer_after_min: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ["2026-12-02", ...] local dates.
    holidays: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    updated_by: Mapped[uuid.UUID | None] = _user_fk(nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class HireInterview(Base, OrgScoped):
    __tablename__ = INTERVIEWS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hire_interviews_org_id_id"),
        UniqueConstraint("org_id", "ics_uid", name="uq_hire_interviews_ics_uid"),
        ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_interviews_application_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_interviews_job_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "candidate_id"],
            ["hire_candidates.org_id", "hire_candidates.id"],
            ondelete="RESTRICT",
            name="fk_hire_interviews_candidate_org",
        ),
        CheckConstraint(f"status IN ({_in(InterviewStatus)})", name="status_valid"),
        CheckConstraint(f"stage_key ~ '{STAGE_KEY_PATTERN}'", name="stage_key_format"),
        CheckConstraint("duration_min BETWEEN 5 AND 480", name="duration_range"),
        CheckConstraint(
            "status IN ('proposed', 'cancelled') OR starts_at IS NOT NULL",
            name="booked_has_time",
        ),
        CheckConstraint(
            "(starts_at IS NULL) = (ends_at IS NULL) AND (ends_at IS NULL OR ends_at > starts_at)",
            name="times_valid",
        ),
        CheckConstraint("ics_sequence >= 0", name="sequence_valid"),
        CheckConstraint("candidate_locale IN ('en', 'ar')", name="locale_valid"),
        CheckConstraint(
            "link_nonce IS NULL OR link_nonce ~ '^[0-9a-f]{16}$'", name="link_nonce_format"
        ),
        Index("ix_hire_interviews_application", "org_id", "application_id", "created_at"),
        Index("ix_hire_interviews_status_start", "org_id", "status", "starts_at"),
        Index("ix_hire_interviews_job", "org_id", "job_id", "starts_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    stage_key: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    candidate_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_locale: Mapped[str] = mapped_column(String(2), nullable=False)
    organizer_id: Mapped[uuid.UUID] = _user_fk()
    meeting_url: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    meeting_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    calendar_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    calendar_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # The meeting provider's own id (Zoom), to move or delete the meeting (hire_0019).
    meeting_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ics_uid: Mapped[str] = mapped_column(String(200), nullable=False)
    ics_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    link_nonce: Mapped[str | None] = mapped_column(String(16), nullable=True)
    link_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminded_24h_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminded_1h_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Who cancelled: ``team`` or ``candidate``.
    cancelled_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome_by: Mapped[uuid.UUID | None] = _user_fk(nullable=True)
    created_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class HireInterviewPanelist(Base, OrgScoped):
    __tablename__ = PANELISTS
    __table_args__ = (
        UniqueConstraint("org_id", "interview_id", "user_id", name="uq_hire_interview_panelists"),
        ForeignKeyConstraint(
            ["org_id", "interview_id"],
            ["hire_interviews.org_id", "hire_interviews.id"],
            ondelete="RESTRICT",
            name="fk_hire_interview_panelists_interview_org",
        ),
        Index("ix_hire_interview_panelists_user", "org_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = _user_fk()
    created_at: Mapped[datetime] = _created()
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HireCalendarBlock(Base, OrgScoped):
    __tablename__ = BLOCKS
    __table_args__ = (
        CheckConstraint("kind IN ('seed', 'event')", name="kind_valid"),
        CheckConstraint("ends_at > starts_at", name="times_valid"),
        CheckConstraint("kind = 'seed' OR event_uid IS NOT NULL", name="event_has_uid"),
        Index(
            "uq_hire_calendar_blocks_seed",
            "org_id",
            "user_id",
            "day",
            "seq",
            unique=True,
            postgresql_where=text("kind = 'seed'"),
        ),
        Index(
            "uq_hire_calendar_blocks_event",
            "org_id",
            "user_id",
            "event_uid",
            unique=True,
            postgresql_where=text("kind = 'event'"),
        ),
        Index("ix_hire_calendar_blocks_user_time", "org_id", "user_id", "starts_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    day: Mapped[date] = mapped_column(Date, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    event_uid: Mapped[str | None] = mapped_column(String(200), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class HireCalendarConnection(Base, OrgScoped):
    __tablename__ = CONNECTIONS
    __table_args__ = (
        UniqueConstraint("org_id", "user_id", "provider", name="uq_hire_calendar_connections"),
        CheckConstraint("provider IN ('google', 'microsoft')", name="provider_valid"),
        CheckConstraint("status IN ('active', 'revoked', 'error')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    account_email: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    refresh_token: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    scopes: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    connected_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class HireMeetingIntegration(Base, OrgScoped):
    __tablename__ = MEETING_INTEGRATIONS
    __table_args__ = (
        UniqueConstraint("org_id", "provider", name="uq_hire_meeting_integrations"),
        CheckConstraint("provider IN ('zoom')", name="provider_valid"),
        CheckConstraint("status IN ('active', 'revoked', 'error')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    client_id: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    client_secret: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    host: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by: Mapped[uuid.UUID] = _user_fk()
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class OperationStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    RECONCILED = "reconciled"
    FAILED = "failed"


class HireCalendarOperation(Base, OrgScoped):
    __tablename__ = OPERATIONS
    __table_args__ = (
        UniqueConstraint("org_id", "client_request_id", name="uq_hire_calendar_operations_key"),
        CheckConstraint("kind IN ('event', 'meeting')", name="kind_valid"),
        CheckConstraint("action IN ('create', 'update', 'cancel')", name="action_valid"),
        CheckConstraint(f"status IN ({_in(OperationStatus)})", name="status_valid"),
        CheckConstraint("client_request_id ~ '^[0-9a-f]{32}$'", name="key_format"),
        CheckConstraint("jsonb_typeof(intended) = 'object'", name="intended_object"),
        Index("ix_hire_calendar_operations_pending", "org_id", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    interview_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    organizer_id: Mapped[uuid.UUID] = _user_fk()
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    intended: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # The provider refused the call (nothing was written); else unknown or written.
    rejected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
