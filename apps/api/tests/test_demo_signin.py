# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Hosted demo sign-in (``GET /v1/demo/signin-accounts``, ``pairly_core.demo_signin``).

Off unless the deployment switch is on, outside production, and the organisation turned
its ``demo.public_signin`` flag on. Only listed demo addresses are ever offered.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_api.main import create_app
from pairly_core.config import Environment
from pairly_core.db import create_engine, create_sessionmaker
from pairly_core.demo_signin import (
    DEMO_ACCOUNTS,
    DemoSigninSettings,
    allowed_emails,
    load_demo_signin_settings,
)
from pairly_core.flags.registry import CORE_FLAGS, DEMO_PUBLIC_SIGNIN
from pairly_core.identity import AppContext
from pairly_core.plugins import WorkflowPlugin
from pairly_core.rbac import build_registry
from pairly_hire.plugin import plugin as hire_plugin
from pairly_pay.plugin import plugin as pay_plugin

from .conftest import DbUrls, TokenFactory, _drop_tenant, _seed_tenant, make_settings

PLUGINS: list[WorkflowPlugin] = [hire_plugin, pay_plugin]
PATH = "/v1/demo/signin-accounts"


# ------------------------------------------------------------------ no database


def test_off_by_default_and_never_in_production() -> None:
    assert load_demo_signin_settings().enabled is False
    flag = next(f for f in CORE_FLAGS if f.key == DEMO_PUBLIC_SIGNIN)
    assert flag.default is False
    on = DemoSigninSettings(env=Environment.STAGING, demo_public_signin=True)
    assert on.enabled is True
    prod = DemoSigninSettings(env=Environment.PRODUCTION, demo_public_signin=True)
    assert prod.enabled is False


def test_allow_list_is_the_seeded_people_and_never_the_admin() -> None:
    from pairly_expense.seed import DEMO_PEOPLE as EXPENSE_PEOPLE
    from pairly_expense.seed import demo_email as expense_email
    from pairly_hire.seed import DEMO_PEOPLE as HIRE_PEOPLE
    from pairly_hire.seed import demo_email as hire_email

    allowed = allowed_emails("demo")
    for who, _ in HIRE_PEOPLE:
        assert hire_email("demo", who) in allowed
    for who, role in EXPENSE_PEOPLE:
        if role != "finance_controller":
            assert expense_email("demo", who) in allowed
    for email in ("demo-pay-clerk@example.test", "demo-pay-approver@example.test"):
        assert email in allowed
    assert "demo-admin@example.test" not in allowed
    assert all(e.endswith("@example.test") for e in allowed)
    assert len(allowed) == len(DEMO_ACCOUNTS)


# ------------------------------------------------------------------ with the database


@dataclass
class DemoOrg:
    slug: str
    org_id: uuid.UUID
    listed: str
    unlisted: str


def _client_app(tokens: TokenFactory, db: DbUrls, settings: DemoSigninSettings) -> FastAPI:
    core = make_settings(db.app)
    app = create_app(core, plugins=PLUGINS)
    engine = create_engine(core)
    app.state.pairly = AppContext(
        settings=core,
        engine=engine,
        sessionmaker=create_sessionmaker(engine),
        verifier=tokens.verifier(),
        registry=build_registry(PLUGINS),
    )
    app.state.demo_signin = settings
    return app


async def _get(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
        return await c.get(PATH)


@pytest.fixture
async def demo_org(owner_engine: AsyncEngine) -> AsyncIterator[DemoOrg]:
    async with owner_engine.begin() as conn:
        listed_tenant = await _seed_tenant(conn, f"d{uuid.uuid4().hex[:6]}", "approver")
        slug = await conn.scalar(
            text("SELECT slug FROM organisations WHERE id = :o"), {"o": listed_tenant.org_id}
        )
        listed = f"{slug}-hire-recruiter@example.test"
        await conn.execute(
            text("UPDATE users SET email = :e WHERE id = :u"),
            {"e": listed, "u": listed_tenant.user_id},
        )
    # A second member of the same organisation whose address is not on the list.
    async with owner_engine.begin() as conn:
        other_user = uuid.uuid4()
        other_subject = f"sub-{other_user.hex[:8]}"
        unlisted = f"{slug}-admin@example.test"
        await conn.execute(
            text(
                "SELECT set_config('app.org_id', :o, true), "
                "set_config('app.auth_subject', :s, true)"
            ),
            {"o": str(listed_tenant.org_id), "s": other_subject},
        )
        await conn.execute(
            text("INSERT INTO users (id, auth_subject, email) VALUES (:id, :sub, :e)"),
            {"id": other_user, "sub": other_subject, "e": unlisted},
        )
        await conn.execute(
            text(
                "INSERT INTO memberships (id, org_id, user_id, role_id) SELECT :id, :o, :u, r.id "
                "FROM roles r WHERE r.key = 'admin' AND r.org_id IS NULL"
            ),
            {"id": uuid.uuid4(), "o": listed_tenant.org_id, "u": other_user},
        )
    yield DemoOrg(str(slug), listed_tenant.org_id, listed, unlisted)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(listed_tenant.org_id)}
        )
        await conn.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": other_user})
        await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": other_user})
    async with owner_engine.begin() as conn:
        await _drop_tenant(conn, listed_tenant)


async def _set_flag(owner_engine: AsyncEngine, org: DemoOrg, enabled: bool) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.org_id)}
        )
        await conn.execute(
            text(
                "INSERT INTO feature_flags (org_id, key, enabled, updated_at) "
                "VALUES (:o, :k, :e, now()) ON CONFLICT ON CONSTRAINT uq_feature_flags_org_key "
                "DO UPDATE SET enabled = excluded.enabled"
            ),
            {"o": org.org_id, "k": DEMO_PUBLIC_SIGNIN, "e": enabled},
        )


def _on(slug: str) -> DemoSigninSettings:
    return DemoSigninSettings(env=Environment.STAGING, demo_public_signin=True, demo_org_slug=slug)


async def test_lists_only_listed_members_when_every_lock_is_open(
    tokens: TokenFactory, migrated: DbUrls, owner_engine: AsyncEngine, demo_org: DemoOrg
) -> None:
    await _set_flag(owner_engine, demo_org, True)
    response = await _get(_client_app(tokens, migrated, _on(demo_org.slug)))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["org_slug"] == demo_org.slug
    assert [a["email"] for a in body["accounts"]] == [demo_org.listed]
    assert body["accounts"][0]["label"] == "recruiter"
    assert demo_org.unlisted not in response.text


@pytest.mark.parametrize("case", ["switch_off", "production", "flag_off", "flag_unset", "slug"])
async def test_every_lock_closes_it_with_the_same_404(
    tokens: TokenFactory,
    migrated: DbUrls,
    owner_engine: AsyncEngine,
    demo_org: DemoOrg,
    case: str,
) -> None:
    settings = _on(demo_org.slug)
    if case == "switch_off":
        await _set_flag(owner_engine, demo_org, True)
        settings = DemoSigninSettings(env=Environment.STAGING, demo_org_slug=demo_org.slug)
    elif case == "production":
        await _set_flag(owner_engine, demo_org, True)
        settings = DemoSigninSettings(
            env=Environment.PRODUCTION, demo_public_signin=True, demo_org_slug=demo_org.slug
        )
    elif case == "flag_off":
        await _set_flag(owner_engine, demo_org, False)
    elif case == "slug":
        await _set_flag(owner_engine, demo_org, True)
        settings = _on("no-such-demo-org")
    response = await _get(_client_app(tokens, migrated, settings))
    assert response.status_code == 404
    assert demo_org.listed not in response.text


async def test_the_lookup_function_is_the_only_way_in(
    migrated: DbUrls, owner_engine: AsyncEngine, demo_org: DemoOrg
) -> None:
    """The application role reads no organisation without a tenant, and the function
    returns an id only for an organisation whose flag is on."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated.app, pool_size=1, max_overflow=0)
    try:
        async with engine.begin() as conn:
            seen = await conn.scalar(
                text("SELECT count(*) FROM organisations WHERE slug = :s"), {"s": demo_org.slug}
            )
            assert seen == 0
            assert (
                await conn.scalar(text("SELECT demo_signin_org_id(:s)"), {"s": demo_org.slug})
                is None
            )
        await _set_flag(owner_engine, demo_org, True)
        async with engine.begin() as conn:
            found = await conn.scalar(text("SELECT demo_signin_org_id(:s)"), {"s": demo_org.slug})
            assert found == demo_org.org_id
            # The lookup switch is reset after the call: no organisation leaks afterwards.
            seen = await conn.scalar(
                text("SELECT count(*) FROM organisations WHERE slug = :s"), {"s": demo_org.slug}
            )
            assert seen == 0
            assert await conn.scalar(text("SELECT demo_signin_org_id('Bad Slug')")) is None
    finally:
        await engine.dispose()
