# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``job_runs`` (migration 0013), a platform table (not org scoped).

Declared here so the schema stays checked (``alembic check``). The application never
queries it through this model: the application role has no privilege on the table and
goes through the SECURITY DEFINER functions in ``pairly_core.jobs.runs``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, String, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base


class JobRunRow(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        CheckConstraint("job_name ~ '^[a-z][a-z0-9_]{0,63}$'", name="job_name_format"),
        CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="status_valid"),
        CheckConstraint("(status = 'running') = (finished_at IS NULL)", name="finished_pair"),
        Index("ix_job_runs_job_name_started_at", "job_name", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    job_name: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="running")
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String(128), nullable=True)
