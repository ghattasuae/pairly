# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``api_keys`` (migration 0013). Org scoped, RLS forced.

The application role may SELECT and INSERT, and UPDATE only ``last_used_at``,
``revoked_at`` and ``revoked_by``; a trigger makes a revocation final. Keys are never
deleted (they are referenced from the audit log). ``prefix`` is unique across all
organisations because ``api_key_org_id(prefix)`` (SECURITY DEFINER) finds a key's
organisation before the request is bound to one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped, Timestamped


class ApiKey(Base, OrgScoped, Timestamped):
    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint("prefix ~ '^pk_(live|test)_[a-z0-9]{8}$'", name="prefix_format"),
        CheckConstraint("mode IN ('live', 'test')", name="mode_valid"),
        CheckConstraint("split_part(prefix, '_', 2) = mode", name="prefix_matches_mode"),
        CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_format"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_length"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint("(revoked_at IS NULL) = (revoked_by IS NULL)", name="revoked_pair"),
        CheckConstraint(
            "rate_limit_per_minute IS NULL OR rate_limit_per_minute BETWEEN 1 AND 100000",
            name="rate_limit_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Permission names; only read, propose and upload actions (rbac.api_key_may_hold).
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(100)), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    # Requests per minute; NULL means the server default (PAIRLY_API_KEY_RATE_LIMIT_PER_MINUTE).
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
