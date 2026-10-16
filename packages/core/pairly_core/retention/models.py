# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Retention tables. All org scoped, RLS forced, closed to portal sessions.

* ``retention_reports`` (migration 0013, 0030): per run and rule, how many rows or
  subjects are due (``would_delete``), when they are scheduled for deletion
  (``scheduled_for``), whether the rule was on and how many it deleted. A report never
  deletes anything itself (``dry_run`` stays true: it is the report of a run).
* ``retention_rule_settings`` (0030): an organisation's choice per rule: on or off, and
  its period in days (default the rule's own).
* ``retention_org_settings`` (0030): the daily cap of subjects deleted per organisation
  (default 200).
* ``retention_schedule`` (0030): the preview list. Each subject a rule found due, the day
  it was first listed and the day it may be deleted (seven days later), its legal hold,
  then its deletion.
* ``retention_holds`` (0030): a legal hold on a candidate or a vendor. While it stands no
  rule deletes anything about the subject. The reason is encrypted.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped

DEFAULT_DAILY_CAP: Final = 200
MAX_DAILY_CAP: Final = 10000
PREVIEW_DAYS: Final = 7


class RetentionReport(Base, OrgScoped):
    __tablename__ = "retention_reports"
    __table_args__ = (
        UniqueConstraint("org_id", "run_id", "rule", name="uq_retention_reports_run_rule"),
        CheckConstraint("would_delete >= 0", name="count_non_negative"),
        CheckConstraint("deleted >= 0", name="deleted_non_negative"),
        CheckConstraint("dry_run", name="dry_run_only"),
        Index("ix_retention_reports_org_id_created_at", "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    rule: Mapped[str] = mapped_column(String(100), nullable=False)
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    would_delete: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # When what ``would_delete`` counts may be deleted (the preview's end), None when
    # nothing is due or the rule only reports.
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    deleted: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RetentionRuleSetting(Base, OrgScoped):
    __tablename__ = "retention_rule_settings"
    __table_args__ = (
        UniqueConstraint("org_id", "rule", name="uq_retention_rule_settings_org_rule"),
        CheckConstraint("days IS NULL OR days BETWEEN 0 AND 36500", name="days_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    rule: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RetentionOrgSetting(Base, OrgScoped):
    __tablename__ = "retention_org_settings"
    __table_args__ = (
        UniqueConstraint("org_id", name="uq_retention_org_settings_org"),
        CheckConstraint(f"daily_cap BETWEEN 1 AND {MAX_DAILY_CAP}", name="daily_cap_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    daily_cap: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=str(DEFAULT_DAILY_CAP)
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ScheduleStatus(StrEnum):
    # Listed, waiting for the end of its preview (``delete_after``).
    SCHEDULED = "scheduled"
    # A legal hold stands: never deleted while it does.
    HELD = "held"
    # The database part is done; the stored files are being removed (retried).
    ERASING = "erasing"
    DELETED = "deleted"


class RetentionScheduleItem(Base, OrgScoped):
    __tablename__ = "retention_schedule"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "rule", "subject_id", name="uq_retention_schedule_org_rule_subject"
        ),
        CheckConstraint(
            "status IN ('scheduled', 'held', 'erasing', 'deleted')", name="status_valid"
        ),
        CheckConstraint("delete_after >= listed_at", name="preview_after_listing"),
        CheckConstraint(
            "(status = 'deleted') = (deleted_at IS NOT NULL)", name="deleted_when_done"
        ),
        Index("ix_retention_schedule_org_id_status", "org_id", "status", "delete_after"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    rule: Mapped[str] = mapped_column(String(100), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # A reference for the list (an application reference), never a name.
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    listed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delete_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # When the database part was done (the daily cap counts these).
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RetentionHold(Base, OrgScoped):
    __tablename__ = "retention_holds"
    __table_args__ = (
        Index(
            "ix_retention_holds_active",
            "org_id",
            "subject_type",
            "subject_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        CheckConstraint("(released_at IS NULL) = (released_by IS NULL)", name="released_by_person"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # Why the data must be kept (a dispute, a claim). May name people: encrypted.
    reason: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    placed_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    released_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
