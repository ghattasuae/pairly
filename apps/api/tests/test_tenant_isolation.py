# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Tenant isolation against a real Postgres.

Layer 1 (RLS): a session for org A, connected as the non-owner app role, cannot read,
update, delete or insert org B rows, even with raw SQL that has no WHERE clause. With no
org set it sees no tenant rows at all.

Layer 2 (ORM filter): proven on its own by running ORM queries through a superuser
connection (which bypasses RLS) and checking that only the current org's rows return.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy import CursorResult, Result, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from pairly_core.db import (
    TenantError,
    UnsafeDatabaseRoleError,
    check_app_role,
    create_sessionmaker,
    set_tenant,
)
from pairly_core.models import Membership, Organisation, Role

from .conftest import TwoTenants


def _session(
    engine: AsyncEngine, org: uuid.UUID | None, subject: str | None = None
) -> AsyncSession:
    session = create_sessionmaker(engine)()
    set_tenant(session, org, subject)
    return session


def _rows(result: Result[Any]) -> int:
    return cast(CursorResult[Any], result).rowcount


async def test_raw_sql_without_where_sees_only_own_org(
    app_engine: AsyncEngine, tenants: TwoTenants
) -> None:
    a, b = tenants.a, tenants.b
    async with _session(app_engine, a.org_id, a.subject) as s, s.begin():
        orgs = (await s.execute(text("SELECT id FROM organisations"))).scalars().all()
        assert orgs == [a.org_id]

        members = (await s.execute(text("SELECT org_id FROM memberships"))).scalars().all()
        assert members
        assert set(members) == {a.org_id}

        role_orgs = set((await s.execute(text("SELECT org_id FROM roles"))).scalars().all())
        assert role_orgs == {None, a.org_id}

        users = set((await s.execute(text("SELECT id FROM users"))).scalars().all())
        assert a.user_id in users
        assert b.user_id not in users

        # Asking for B's rows by id returns nothing.
        hit = await s.execute(
            text("SELECT 1 FROM memberships WHERE id = :id"), {"id": b.membership_id}
        )
        assert hit.first() is None


async def test_cannot_update_or_delete_other_org(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, tenants: TwoTenants
) -> None:
    a, b = tenants.a, tenants.b
    async with _session(app_engine, a.org_id, a.subject) as s, s.begin():
        # No WHERE clause: touches only A's rows.
        updated = await s.execute(text("UPDATE memberships SET workflow = 'pay'"))
        assert _rows(updated) == 1
        assert _rows(await s.execute(text("UPDATE organisations SET name = 'pwned'"))) == 1
        targeted = await s.execute(
            text("UPDATE memberships SET workflow = 'hire' WHERE id = :id"),
            {"id": b.membership_id},
        )
        assert _rows(targeted) == 0
        deleted = await s.execute(
            text("DELETE FROM memberships WHERE org_id = :org"), {"org": b.org_id}
        )
        assert _rows(deleted) == 0
        deleted = await s.execute(text("DELETE FROM roles WHERE org_id IS NULL"))
        assert _rows(deleted) == 0  # built-in roles are read only for tenants

    # Check B as the owner, with explicit filters (the owner may bypass RLS).
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(b.org_id)})
        name = (
            await conn.execute(
                text("SELECT name FROM organisations WHERE id = :o"), {"o": b.org_id}
            )
        ).scalar_one()
        workflow = (
            await conn.execute(
                text("SELECT workflow FROM memberships WHERE id = :m"), {"m": b.membership_id}
            )
        ).scalar_one()
    assert name == "Org b"
    assert workflow is None


_RLS = "row-level security"
_DENIED = "permission denied"


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        # Insert a row for another org.
        (
            "INSERT INTO memberships (org_id, user_id, role_id) VALUES (:b_org, :a_user, :b_role)",
            _RLS,
        ),
        # Move an own row into another org.
        ("UPDATE memberships SET org_id = :b_org WHERE org_id = :a_org", _RLS),
        # Grant a role that belongs to another org, inside the own org.
        (
            "INSERT INTO memberships (org_id, user_id, role_id) VALUES (:a_org, :a_user, :b_role)",
            _RLS,
        ),
        # Create a custom role for another org, or a shared (built-in) role.
        ("INSERT INTO roles (org_id, key, name) VALUES (:b_org, 'x', 'x')", _RLS),
        ("INSERT INTO roles (org_id, key, name) VALUES (NULL, 'x', 'x')", _RLS),
        # Create or remove organisations (no privilege at all).
        ("INSERT INTO organisations (name, slug) VALUES ('x', 'x-org')", _DENIED),
        ("DELETE FROM organisations", _DENIED),
        # Change the auth subject or the status of an organisation (column privileges).
        ("UPDATE users SET auth_subject = 'other'", _DENIED),
        ("UPDATE organisations SET status = 'active'", _DENIED),
        # Turn RLS off (only the table owner could).
        ("ALTER TABLE memberships DISABLE ROW LEVEL SECURITY", "must be owner"),
    ],
)
async def test_cross_tenant_writes_rejected(
    app_engine: AsyncEngine, tenants: TwoTenants, sql: str, reason: str
) -> None:
    a, b = tenants.a, tenants.b
    params = {
        "a_org": a.org_id,
        "b_org": b.org_id,
        "a_user": a.user_id,
        "b_role": b.custom_role_id,
    }
    used = {k: v for k, v in params.items() if f":{k}" in sql}
    async with _session(app_engine, a.org_id, a.subject) as s:
        with pytest.raises(DBAPIError, match=reason):
            async with s.begin():
                await s.execute(text(sql), used)


async def test_no_org_set_sees_nothing(app_engine: AsyncEngine, tenants: TwoTenants) -> None:
    async with _session(app_engine, None) as s, s.begin():
        for table in ("organisations", "memberships", "users"):
            assert (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0  # noqa: S608  # nosec B608
        role_orgs = set((await s.execute(text("SELECT org_id FROM roles"))).scalars().all())
        assert role_orgs == {None}
        assert _rows(await s.execute(text("UPDATE memberships SET workflow = 'x'"))) == 0

    # A plain connection that never set anything sees nothing either.
    async with app_engine.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM memberships"))).scalar_one()
        assert count == 0


async def test_setting_does_not_leak_across_transactions(
    app_engine: AsyncEngine, tenants: TwoTenants
) -> None:
    a = tenants.a
    async with _session(app_engine, a.org_id) as s, s.begin():
        assert (await s.execute(text("SELECT count(*) FROM memberships"))).scalar_one() == 1
    # Same single pooled connection, new transaction, no tenant: nothing visible.
    async with app_engine.begin() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM memberships"))).scalar_one() == 0
        org = (await conn.execute(text("SELECT current_setting('app.org_id', true)"))).scalar()
        assert not org


async def test_orm_filter_is_a_second_layer(owner_engine: AsyncEngine, tenants: TwoTenants) -> None:
    """The owner here is a superuser (RLS bypassed), so only the ORM filter applies."""
    a, b = tenants.a, tenants.b
    for tenant, other in ((a, b), (b, a)):
        async with _session(owner_engine, tenant.org_id) as s, s.begin():
            members = (await s.scalars(select(Membership))).all()
            assert {m.org_id for m in members} == {tenant.org_id}
            orgs = (await s.scalars(select(Organisation))).all()
            assert [o.id for o in orgs] == [tenant.org_id]
            roles = (await s.scalars(select(Role))).all()
            assert other.custom_role_id not in {r.id for r in roles}
            assert await s.get(Membership, other.membership_id) is None
            result = await s.execute(
                update(Membership)
                .where(Membership.id == other.membership_id)
                .values(workflow="x")
                .execution_options(synchronize_session=False)
            )
            assert _rows(result) == 0

    async with _session(owner_engine, None) as s, s.begin():
        assert (await s.scalars(select(Membership))).all() == []
        assert (await s.scalars(select(Organisation))).all() == []


async def test_orm_refuses_cross_tenant_writes(
    owner_engine: AsyncEngine, tenants: TwoTenants
) -> None:
    a, b = tenants.a, tenants.b
    async with _session(owner_engine, a.org_id) as s:
        s.add(Membership(org_id=b.org_id, user_id=a.user_id, role_id=a.custom_role_id))
        with pytest.raises(TenantError):
            await s.flush()
    async with _session(owner_engine, None) as s:
        s.add(Membership(user_id=a.user_id, role_id=a.custom_role_id))
        with pytest.raises(TenantError):
            await s.flush()


async def test_app_role_must_not_bypass_rls(
    app_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    await check_app_role(app_engine)
    async with owner_engine.connect() as conn:
        is_super = (
            await conn.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))
        ).scalar_one()
    if is_super:
        with pytest.raises(UnsafeDatabaseRoleError):
            await check_app_role(owner_engine)
