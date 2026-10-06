# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The hash-chained audit log table (``audit_events``).

Rows are written only through ``pairly_core.audit.append_audit`` and are never changed:
the application role holds INSERT and SELECT only, and database triggers reject UPDATE,
DELETE and TRUNCATE for every role (see migration 0002).

``org_id`` deliberately has no foreign key to ``organisations``: the audit trail must
outlive the organisation row and must never block its removal.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, String, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped


class AuditEvent(Base, OrgScoped):
    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("org_id", "seq", name="uq_audit_events_org_id_seq"),
        Index("ix_audit_events_org_id_occurred_at", "org_id", "occurred_at"),
        Index("ix_audit_events_org_id_entity", "org_id", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    # Overrides the OrgScoped column: same type, no foreign key (see module docstring).
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workflow: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
