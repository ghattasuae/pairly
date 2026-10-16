# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The ``workflow_runs`` table: one row per LangGraph run (thread).

``thread_id`` is ``"<org_id>:<run_id>"``. A check constraint makes the database refuse any
other value, and the checkpoint tables refuse a thread id whose prefix is not the
organisation of the connection that writes it (migration 0006). Run ids appear in every
audit event about the run (``entity_type`` ``workflow_run``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped


class RunStatus(StrEnum):
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})


def thread_id_for(org_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """The LangGraph thread id of a run. Embeds the organisation (see module docstring)."""
    return f"{org_id}:{run_id}"


class WorkflowRun(Base, OrgScoped):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_org_status", "org_id", "status"),
        CheckConstraint(
            "status IN ('running', 'waiting_approval', 'completed', 'failed', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint("thread_id = org_id::text || ':' || id::text", name="thread_id_bound"),
        CheckConstraint("started_by_type IN ('user', 'agent', 'system')", name="started_by_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    workflow: Mapped[str] = mapped_column(String(32), nullable=False)
    graph_name: Mapped[str] = mapped_column(String(64), nullable=False)
    graph_version: Mapped[int] = mapped_column(Integer, nullable=False)
    thread_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    started_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # The approval the run waits for (status waiting_approval), else NULL.
    pending_approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # Short, PII free: an error class name and a fixed message.
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Set when a process takes the run to execute it (start or resume).
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
