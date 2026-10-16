# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``feature_flags`` (migration 0013): an organisation's flag overrides. Org scoped, RLS
forced; SELECT, INSERT, UPDATE, DELETE for the application role (deleting a row puts the
flag back to its declared default)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped


class FeatureFlag(Base, OrgScoped):
    __tablename__ = "feature_flags"
    __table_args__ = (
        UniqueConstraint("org_id", "key", name="uq_feature_flags_org_key"),
        CheckConstraint("key ~ '^[a-z][a-z0-9_]*([.][a-z][a-z0-9_]*){1,3}$'", name="key_format"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
