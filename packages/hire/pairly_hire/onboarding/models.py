# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Checklist tables (migration ``hire_0015``). Org scoped, Row Level Security forced,
a candidate's portal session reaches only their own items (to attach an upload to the
item their own document request points at); the application role gets SELECT, INSERT
and UPDATE, never DELETE.

* ``hire_checklist_configs``: per job, the documents to collect (``items``: key, label,
  Arabic label) and how many days after hire the files are kept (``retention_days``,
  default 90). No row: the default list (ID, degree, visa).
* ``hire_checklist_items``: one document for one application. The file is stored
  encrypted in the object store (``object_key``, an envelope token bound to this row);
  its name is encrypted here. ``status``: requested, received, verified, waived.
  ``retention_until`` is set once the candidate is hired; ``purged_at`` once the file is
  gone.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped

CONFIGS: Final = "hire_checklist_configs"
ITEMS: Final = "hire_checklist_items"
DEFAULT_RETENTION_DAYS: Final = 90


class ChecklistStatus(StrEnum):
    REQUESTED = "requested"
    RECEIVED = "received"
    VERIFIED = "verified"
    WAIVED = "waived"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class HireChecklistConfig(Base, OrgScoped):
    __tablename__ = CONFIGS
    __table_args__ = (
        UniqueConstraint("org_id", "job_id", name="uq_hire_checklist_configs_job"),
        ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_checklist_configs_job_org",
        ),
        CheckConstraint("jsonb_typeof(items) = 'array'", name="items_array"),
        CheckConstraint("retention_days BETWEEN 1 AND 365", name="retention_days_range"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="90")
    updated_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class HireChecklistItem(Base, OrgScoped):
    __tablename__ = ITEMS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hire_checklist_items_org_id_id"),
        UniqueConstraint("org_id", "application_id", "key", name="uq_hire_checklist_items_key"),
        ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_checklist_items_application_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "candidate_id"],
            ["hire_candidates.org_id", "hire_candidates.id"],
            ondelete="RESTRICT",
            name="fk_hire_checklist_items_candidate_org",
        ),
        CheckConstraint(
            "status IN ('requested', 'received', 'verified', 'waived')", name="status_valid"
        ),
        CheckConstraint("key ~ '^[a-z][a-z0-9_]{0,31}$'", name="key_format"),
        CheckConstraint(
            "status NOT IN ('received', 'verified') OR object_key IS NOT NULL "
            "OR purged_at IS NOT NULL",
            name="received_has_file",
        ),
        CheckConstraint("retention_days BETWEEN 1 AND 365", name="retention_days_range"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    key: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    label_ar: Mapped[str] = mapped_column(String(120), nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="requested")
    document_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    object_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    file_name: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="90")
    requested_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
