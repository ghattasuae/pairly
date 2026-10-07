# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``retention_reports`` (migration 0013): what a retention dry run found. Org scoped, RLS
forced; SELECT, INSERT, DELETE for the application role. ``dry_run`` must be true."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped


class RetentionReport(Base, OrgScoped):
    __tablename__ = "retention_reports"
    __table_args__ = (
        UniqueConstraint("org_id", "run_id", "rule", name="uq_retention_reports_run_rule"),
        CheckConstraint("would_delete >= 0", name="count_non_negative"),
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
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
