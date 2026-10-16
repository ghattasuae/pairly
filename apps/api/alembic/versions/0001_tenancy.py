# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Tenancy: organisations, users, roles, memberships, with Row Level Security.

RLS is enabled and forced on every table. The tenant comes from the transaction-local
setting ``app.org_id`` (``app_current_org_id()``); when it is unset or empty the function
returns NULL and no tenant rows match. The application role gets DML only.

Revision ID: 0001
Revises:
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import (
    CURRENT_ORG_FN,
    CURRENT_SUBJECT_FN,
    app_db_role,
    org_rls_statements,
)
from pairly_core.rbac import BUILTIN_ROLE_NAMES, HUMAN_ROLES

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Fixed ids for built-in roles, so every environment agrees on them. These are the roles
# of this revision; later built-in roles are seeded by later migrations (0015: employee).
BUILTIN_ROLE_IDS = {
    "admin": "00000000-0000-4000-8000-000000000001",
    "recruiter": "00000000-0000-4000-8000-000000000002",
    "hiring_manager": "00000000-0000-4000-8000-000000000003",
    "interviewer": "00000000-0000-4000-8000-000000000004",
    "ap_clerk": "00000000-0000-4000-8000-000000000005",
    "approver": "00000000-0000-4000-8000-000000000006",
    "finance_controller": "00000000-0000-4000-8000-000000000007",
    "auditor": "00000000-0000-4000-8000-000000000008",
    "read_only": "00000000-0000-4000-8000-000000000009",
}


def _uuid_pk() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    app_role = app_db_role()
    assert set(BUILTIN_ROLE_IDS) <= set(HUMAN_ROLES)  # noqa: S101 (migration sanity check)

    # STABLE SQL functions get inlined by the planner, so policies stay index friendly.
    op.execute(
        f"""
        CREATE FUNCTION {CURRENT_ORG_FN} RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT NULLIF(current_setting('app.org_id', true), '')::uuid $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {CURRENT_SUBJECT_FN} RETURNS text
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT NULLIF(current_setting('app.auth_subject', true), '') $$
        """
    )

    op.create_table(
        "organisations",
        _uuid_pk(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        _created_at(),
        sa.UniqueConstraint("slug", name="uq_organisations_slug"),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'", name="ck_organisations_slug_format"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')", name="ck_organisations_status_valid"
        ),
    )
    op.create_table(
        "users",
        _uuid_pk(),
        sa.Column("auth_subject", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        _created_at(),
        sa.UniqueConstraint("auth_subject", name="uq_users_auth_subject"),
    )
    op.create_table(
        "roles",
        _uuid_pk(),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name="fk_roles_org_id_organisations"
            ),
            nullable=True,
        ),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        _created_at(),
        sa.UniqueConstraint(
            "org_id", "key", name="uq_roles_org_id", postgresql_nulls_not_distinct=True
        ),
        sa.CheckConstraint("key ~ '^[a-z][a-z0-9_]{0,63}$'", name="ck_roles_key_format"),
    )
    op.create_index("ix_roles_org_id", "roles", ["org_id"])
    op.create_table(
        "memberships",
        _uuid_pk(),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id",
                ondelete="RESTRICT",
                name="fk_memberships_org_id_organisations",
            ),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name="fk_memberships_user_id_users"),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            sa.Uuid(),
            sa.ForeignKey("roles.id", ondelete="RESTRICT", name="fk_memberships_role_id_roles"),
            nullable=False,
        ),
        sa.Column("workflow", sa.String(32), nullable=True),
        _created_at(),
        sa.UniqueConstraint(
            "org_id",
            "user_id",
            "role_id",
            "workflow",
            name="uq_memberships_org_user_role_workflow",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            "workflow IS NULL OR workflow ~ '^[a-z][a-z0-9_]{0,31}$'",
            name="ck_memberships_workflow_format",
        ),
    )
    op.create_index("ix_memberships_org_id", "memberships", ["org_id"])
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_index("ix_memberships_role_id", "memberships", ["role_id"])

    # Built-in human roles are shared rows (org_id NULL). Seeded before RLS is forced so
    # that a non-superuser owner can insert them. The agent role is a service role and is
    # deliberately not assignable through memberships.
    roles = sa.table(
        "roles",
        sa.column("id", postgresql.UUID(as_uuid=False)),
        sa.column("key", sa.String),
        sa.column("name", sa.String),
    )
    op.bulk_insert(
        roles,
        [
            {"id": BUILTIN_ROLE_IDS[key], "key": key, "name": BUILTIN_ROLE_NAMES[key]}
            for key in sorted(BUILTIN_ROLE_IDS)
        ],
    )

    cur_org = CURRENT_ORG_FN
    cur_sub = CURRENT_SUBJECT_FN
    # Start from zero: ALTER DEFAULT PRIVILEGES in the database may have granted the app
    # role full DML on new tables. The grants below are the only ones it keeps.
    for table in ("organisations", "users", "roles", "memberships"):
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON {table} FROM {app_role}")

    # organisations: a tenant sees and edits only its own row. No INSERT or DELETE for
    # the app role: creating and removing organisations is a provisioning task.
    op.execute("ALTER TABLE organisations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organisations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY organisations_tenant_isolation ON organisations "
        f"USING (id = {cur_org}) WITH CHECK (id = {cur_org})"
    )
    op.execute(f"GRANT SELECT, UPDATE (name, slug) ON organisations TO {app_role}")

    # users: global identities. Visible when they are the authenticated subject or a
    # member of the current org. Writable only for the authenticated subject itself.
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY users_select ON users FOR SELECT USING ("  # noqa: S608  # nosec B608
        f"auth_subject = {cur_sub} OR EXISTS (SELECT 1 FROM memberships m "
        f"WHERE m.user_id = users.id AND m.org_id = {cur_org}))"
    )
    op.execute(
        f"CREATE POLICY users_insert ON users FOR INSERT WITH CHECK (auth_subject = {cur_sub})"
    )
    op.execute(
        f"CREATE POLICY users_update ON users FOR UPDATE USING (auth_subject = {cur_sub}) "
        f"WITH CHECK (auth_subject = {cur_sub})"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE (email, display_name) ON users TO {app_role}")

    # roles: built-in rows readable by every tenant; custom rows only by their org.
    op.execute("ALTER TABLE roles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY roles_select ON roles FOR SELECT "
        f"USING (org_id IS NULL OR org_id = {cur_org})"
    )
    op.execute(f"CREATE POLICY roles_insert ON roles FOR INSERT WITH CHECK (org_id = {cur_org})")
    op.execute(
        f"CREATE POLICY roles_update ON roles FOR UPDATE USING (org_id = {cur_org}) "
        f"WITH CHECK (org_id = {cur_org})"
    )
    op.execute(f"CREATE POLICY roles_delete ON roles FOR DELETE USING (org_id = {cur_org})")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON roles TO {app_role}")

    # memberships: standard tenant isolation, plus the role must be a built-in role or
    # one of this org's custom roles.
    for stmt in org_rls_statements("memberships", app_role):
        op.execute(stmt)
    op.execute(
        "CREATE POLICY memberships_role_in_scope ON memberships AS RESTRICTIVE "  # noqa: S608  # nosec B608
        "FOR INSERT WITH CHECK (EXISTS (SELECT 1 FROM roles r WHERE r.id = role_id "
        f"AND (r.org_id IS NULL OR r.org_id = {cur_org})))"
    )
    op.execute(
        "CREATE POLICY memberships_role_in_scope_update ON memberships AS RESTRICTIVE "  # noqa: S608  # nosec B608
        "FOR UPDATE WITH CHECK (EXISTS (SELECT 1 FROM roles r WHERE r.id = role_id "
        f"AND (r.org_id IS NULL OR r.org_id = {cur_org})))"
    )


def downgrade() -> None:
    # users_select references memberships, so it goes first.
    op.execute("DROP POLICY IF EXISTS users_select ON users")
    op.drop_table("memberships")
    op.drop_table("roles")
    op.drop_table("users")
    op.drop_table("organisations")
    op.execute(f"DROP FUNCTION IF EXISTS {CURRENT_SUBJECT_FN}")
    op.execute(f"DROP FUNCTION IF EXISTS {CURRENT_ORG_FN}")
