# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Tenancy models: organisations, users, roles and memberships.

Users are global identities (one person can belong to several organisations). A
membership grants one role to one user in one organisation, optionally limited to one
workflow (``workflow`` NULL means every workflow). Roles are either built in (shared,
``org_id`` NULL, seeded by migration) or custom to one organisation.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgRoot, OrgScoped, OrgScopedOrShared, Timestamped

_UUID_DEFAULT = text("gen_random_uuid()")


class Organisation(Base, OrgRoot, Timestamped):
    __tablename__ = "organisations"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'", name="slug_format"),
        CheckConstraint("status IN ('active', 'suspended')", name="status_valid"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    # Sandbox organisations (brief 2.4.6) accept test API keys (``pk_test_``). Set by the
    # operator at provisioning; the application role cannot change it (migration 0013).
    sandbox: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )


class User(Base, Timestamped):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    # The ``sub`` claim of the Better Auth JWT.
    auth_subject: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)


class Role(Base, OrgScopedOrShared, Timestamped):
    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("org_id", "key", postgresql_nulls_not_distinct=True),
        CheckConstraint("key ~ '^[a-z][a-z0-9_]{0,63}$'", name="key_format"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class Membership(Base, OrgScoped, Timestamped):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "user_id",
            "role_id",
            "workflow",
            name="uq_memberships_org_user_role_workflow",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "workflow IS NULL OR workflow ~ '^[a-z][a-z0-9_]{0,31}$'", name="workflow_format"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # NULL: the role applies to every workflow. Otherwise a workflow plugin id.
    workflow: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Invitation(Base, OrgScoped, Timestamped):
    """An invitation to join one organisation with one role (``pairly_core.invitations``).

    Only the SHA-256 of the token is stored. ``accepted_*`` and ``revoked_*`` are terminal:
    a database trigger refuses any change to an invitation that is already accepted or
    revoked, so a token can be used at most once.
    """

    __tablename__ = "invitations"
    __table_args__ = (
        Index("ix_invitations_org_id_email", "org_id", "email"),
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_format"),
        CheckConstraint(
            "workflow IS NULL OR workflow ~ '^[a-z][a-z0-9_]{0,31}$'", name="workflow_format"
        ),
        CheckConstraint("email = lower(email) AND email = btrim(email)", name="email_normalised"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint("(accepted_at IS NULL) = (accepted_by IS NULL)", name="accepted_pair"),
        CheckConstraint("accepted_at IS NULL OR revoked_at IS NULL", name="single_outcome"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    workflow: Mapped[str | None] = mapped_column(String(32), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # NULL when created by the bootstrap CLI (a system action, no inviting user).
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
