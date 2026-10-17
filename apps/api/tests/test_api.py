# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""HTTP behaviour: require(), the error model, security headers, CORS and /v1/me."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI

from pairly_api.main import create_app
from pairly_core.db import create_engine, create_sessionmaker
from pairly_core.identity import AppContext, current_actor
from pairly_core.plugins import WorkflowPlugin
from pairly_core.rbac import Actor, ActorKind, RoleGrant, build_registry, require
from pairly_hire.plugin import plugin as hire_plugin
from pairly_pay.plugin import plugin as pay_plugin

from .conftest import DbUrls, TokenFactory, TwoTenants, make_settings

PLUGINS: list[WorkflowPlugin] = [hire_plugin, pay_plugin]
PROBLEM = "application/problem+json"


def _app(tokens: TokenFactory) -> FastAPI:
    settings = make_settings()
    app = create_app(settings, plugins=PLUGINS)
    engine = create_engine(settings)  # never connected in these tests
    app.state.pairly = AppContext(
        settings=settings,
        engine=engine,
        sessionmaker=create_sessionmaker(engine),
        verifier=tokens.verifier(),
        registry=build_registry(PLUGINS),
    )

    @app.get("/v1/test/approve")
    async def approve(
        actor: Annotated[Actor, Depends(require("pay.invoice.approve"))],
    ) -> dict[str, str]:
        return {"by": str(actor.id)}

    @app.get("/v1/test/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    return app


def _actor_with(role: str, amr: frozenset[str] = frozenset({"otp"})) -> Actor:
    registry = build_registry(PLUGINS)
    grants = (RoleGrant(role),)
    return Actor(
        kind=ActorKind.USER,
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        grants=grants,
        permissions=registry.resolve(grants),
        amr=amr,
    )


@pytest.fixture
async def client(tokens: TokenFactory) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    app = _app(tokens)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
        yield app, c


async def test_require_denies_missing_permission(
    client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    app, c = client
    app.dependency_overrides[current_actor] = lambda: _actor_with("ap_clerk")
    response = await c.get("/v1/test/approve")
    assert response.status_code == 403
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert body["code"] == "permission_denied"
    assert body["status"] == 403
    assert body["request_id"] == response.headers["x-request-id"]


async def test_require_allows_holder(client: tuple[FastAPI, httpx.AsyncClient]) -> None:
    app, c = client
    app.dependency_overrides[current_actor] = lambda: _actor_with("approver")
    assert (await c.get("/v1/test/approve")).status_code == 200


async def test_agent_cannot_reach_approve_route(client: tuple[FastAPI, httpx.AsyncClient]) -> None:
    app, c = client
    app.dependency_overrides[current_actor] = lambda: _actor_with("agent")
    assert (await c.get("/v1/test/approve")).status_code == 403


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer not-a-jwt"}],
    ids=["none", "basic", "garbage"],
)
async def test_unauthenticated(
    client: tuple[FastAPI, httpx.AsyncClient], headers: dict[str, str]
) -> None:
    _, c = client
    response = await c.get("/v1/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_health_and_security_headers(client: tuple[FastAPI, httpx.AsyncClient]) -> None:
    _, c = client
    response = await c.get("/healthz", headers={"X-Request-ID": "req-12345678"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-12345678"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"

    bad_id = await c.get("/healthz", headers={"X-Request-ID": "bad id\r\n"})
    assert bad_id.headers["x-request-id"] != "bad id"


async def test_not_found_and_500_are_problems(client: tuple[FastAPI, httpx.AsyncClient]) -> None:
    _, c = client
    missing = await c.get("/v1/nope")
    assert missing.status_code == 404
    assert missing.headers["content-type"] == PROBLEM

    boom = await c.get("/v1/test/boom")
    assert boom.status_code == 500
    assert boom.headers["content-type"] == PROBLEM
    assert "secret" not in boom.text
    assert boom.headers["x-content-type-options"] == "nosniff"


async def test_cors_is_strict(client: tuple[FastAPI, httpx.AsyncClient]) -> None:
    _, c = client
    preflight = {"Access-Control-Request-Method": "GET"}
    ok = await c.options("/v1/me", headers={"Origin": "https://app.test.pairly", **preflight})
    assert ok.headers.get("access-control-allow-origin") == "https://app.test.pairly"
    evil = await c.options("/v1/me", headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in evil.headers


# ------------------------------------------------------------------ with a database


@pytest.fixture
async def live(migrated: DbUrls, tokens: TokenFactory) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(make_settings(migrated.app), plugins=PLUGINS)

    @app.get("/v1/test/manage-users")
    async def manage(
        actor: Annotated[Actor, Depends(require("core.users.manage"))],
    ) -> dict[str, str]:
        return {"org": str(actor.org_id)}

    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
            yield c


async def test_me(live: httpx.AsyncClient, tokens: TokenFactory, tenants: TwoTenants) -> None:
    a = tenants.a
    auth = {"Authorization": f"Bearer {tokens.token(a.subject)}"}
    response = await live.get("/v1/me", headers={**auth, "X-Pairly-Org": str(a.org_id)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(a.user_id)
    assert body["org"]["id"] == str(a.org_id)
    assert body["roles"] == [{"role": "approver", "workflow": None}]
    assert "pay.invoice.approve" in body["permissions"]
    assert "core.users.manage" not in body["permissions"]


@pytest.mark.parametrize(
    ("org", "status"),
    [("other", 403), ("missing", 403), ("random", 403), ("malformed", 400)],
)
async def test_me_org_header(
    live: httpx.AsyncClient, tokens: TokenFactory, tenants: TwoTenants, org: str, status: int
) -> None:
    a, b = tenants.a, tenants.b
    headers = {"Authorization": f"Bearer {tokens.token(a.subject)}"}
    value = {"other": str(b.org_id), "random": str(uuid.uuid4()), "malformed": "org-b"}.get(org)
    if value is not None:
        headers["X-Pairly-Org"] = value
    response = await live.get("/v1/me", headers=headers)
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM


async def test_unknown_subject_forbidden(
    live: httpx.AsyncClient, tokens: TokenFactory, tenants: TwoTenants
) -> None:
    headers = {
        "Authorization": f"Bearer {tokens.token('nobody')}",
        "X-Pairly-Org": str(tenants.a.org_id),
    }
    assert (await live.get("/v1/me", headers=headers)).status_code == 403


async def test_readyz(live: httpx.AsyncClient) -> None:
    assert (await live.get("/readyz")).status_code == 200


async def test_require_with_real_memberships(
    live: httpx.AsyncClient, tokens: TokenFactory, tenants: TwoTenants
) -> None:
    a, b = tenants.a, tenants.b  # a is an approver, b is an admin
    for tenant, status in ((a, 403), (b, 200)):
        headers = {
            "Authorization": f"Bearer {tokens.token(tenant.subject, amr=['otp'])}",
            "X-Pairly-Org": str(tenant.org_id),
        }
        response = await live.get("/v1/test/manage-users", headers=headers)
        assert response.status_code == status, response.text
