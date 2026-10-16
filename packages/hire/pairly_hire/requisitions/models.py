# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The requisitions table (migration ``hire_0008``). Org scoped, Row Level Security
forced; the application role gets SELECT, INSERT and UPDATE, never DELETE.

``status``:

* ``pending_approval``: submitted; the ``hire_requisition`` run asks for the approval
  (``approval_id`` once it exists).
* ``approved``: the chain approved it (the final action, after the executor re-checked
  the approval). A job may now be opened from it (``hire_jobs.requisition_id``).
* ``rejected``, ``changes_requested``: an approver said no, or asked for changes.
* ``cancelled``: the requester withdrew it while it waited.
* ``failed``: nobody may approve it (``status_reason``), fail closed.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
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
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped

REQUISITIONS: Final = "hire_requisitions"


class RequisitionStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"


def _in(values: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


class HireRequisition(Base, OrgScoped):
    __tablename__ = REQUISITIONS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hire_requisitions_org_id_id"),
        UniqueConstraint("org_id", "reference", name="uq_hire_requisitions_reference"),
        CheckConstraint(f"status IN ({_in(RequisitionStatus)})", name="status_valid"),
        CheckConstraint("headcount BETWEEN 1 AND 100", name="headcount_range"),
        CheckConstraint("budget > 0", name="budget_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint("work_model IN ('onsite', 'hybrid', 'remote')", name="work_model_valid"),
        Index("ix_hire_requisitions_status", "org_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    reference: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    title_ar: Mapped[str] = mapped_column(
        String(160), nullable=False, default="", server_default=""
    )
    team: Mapped[str] = mapped_column(String(120), nullable=False)
    budget: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    headcount: Mapped[int] = mapped_column(Integer, nullable=False)
    location: Mapped[str] = mapped_column(String(160), nullable=False)
    work_model: Mapped[str] = mapped_column(String(16), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    status_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
