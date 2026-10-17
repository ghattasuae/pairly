# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Audit hardening tables (migration ``0012``). Org scoped, RLS forced.

* ``audit_verifications``: one row per chain verification run by the worker (large
  chains are verified there, not in the request). Holds the status and the result.
* ``audit_denial_limits``: one token bucket per actor for ``core.authz.denied``
  events. Denials beyond the bucket are counted here and written later as one
  ``core.authz.denials_suppressed`` summary event, never dropped silently.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
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

from pairly_core.db import Base, OrgScoped

_UUID_DEFAULT = text("gen_random_uuid()")


class AuditVerification(Base, OrgScoped):
    __tablename__ = "audit_verifications"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'running', 'done', 'failed')", name="status_valid"),
        CheckConstraint("requested_by_type IN ('user', 'agent', 'system')", name="requester_valid"),
        Index("ix_audit_verifications_status", "org_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    requested_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditDenialLimit(Base, OrgScoped):
    __tablename__ = "audit_denial_limits"
    __table_args__ = (
        UniqueConstraint("org_id", "actor_key", name="uq_audit_denial_limits_actor"),
        CheckConstraint("tokens >= 0", name="tokens_non_negative"),
        CheckConstraint("suppressed >= 0", name="suppressed_non_negative"),
        Index("ix_audit_denial_limits_pending", "org_id", "suppressed", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    # Rate limit state only (the denials are audit events): deleted with the org.
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # ``<actor type>:<actor id>``.
    actor_key: Mapped[str] = mapped_column(String(160), nullable=False)
    tokens: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    suppressed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    suppressed_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The permissions of the suppressed denials (distinct, capped), for the summary.
    suppressed_permissions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
