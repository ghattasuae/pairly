# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Tenancy models: organisations, users, roles and memberships.

Users are global identities (one person can belong to several organisations). A
membership grants one role to one user in one organisation, optionally limited to one
workflow (``workflow`` NULL means every workflow). Roles are either built in (shared,
``org_id`` NULL, seeded by migration) or custom to one organisation.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint, Uuid, text
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
