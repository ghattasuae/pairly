# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Sandbox mode: ``pairly-admin sandbox create`` and the test/live key separation.

* The command makes a sandbox organisation with two pending approvals and a decision
  record, and prints a ``pk_test_`` key that answers "which approvals are waiting for me?"
  through the MCP server.
* Test keys never work in a non-sandbox organisation, and live keys never work (nor can
  be created) in a sandbox one, including after the flag changes.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_api import admin
from pairly_api.main import create_app
from pairly_api.sandbox import create_sandbox
from pairly_mcp.client import PairlyApi
from pairly_mcp.config import McpConfig
from pairly_mcp.server import PairlyTools
from pairly_pay.plugin import plugin as pay_plugin

from .conftest import DbUrls, TokenFactory, make_settings
from .policy_support import Org

OTP = {"amr": ["otp"]}


@dataclasses.dataclass
class Api:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory

    def person(self, subject: str, org: uuid.UUID) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tokens.token(subject, **OTP)}",
            "X-Pairly-Org": str(org),
        }


@pytest.fixture
async def api(migrated: DbUrls, tokens: TokenFactory) -> AsyncIterator[Api]:
    app = create_app(make_settings(migrated.app), plugins=[pay_plugin])
    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield Api(app, client, tokens)


OrgFactory = Callable[[str], Awaitable[Org]]


async def _set_sandbox(owner_engine: AsyncEngine, org: uuid.UUID, value: bool) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)})
        await conn.execute(
            text("UPDATE organisations SET sandbox = :v WHERE id = :o"), {"v": value, "o": org}
        )


def _slug() -> str:
    return f"sbx-{uuid.uuid4().hex[:10]}"


async def test_sandbox_create_seeds_an_org_with_a_working_test_key(
    api: Api, migrated: DbUrls, owner_engine: AsyncEngine
) -> None:
    made = await create_sandbox(migrated.owner, name="Acme sandbox", slug=_slug())
    assert made.key.startswith("pk_test_")
    assert made.key_prefix == made.key[:16]
    assert {"core.approval.read", "core.proposal.propose", "pay.invoice.read"} <= set(made.scopes)
    assert not any(s.endswith((".approve", ".decide", ".manage")) for s in made.scopes)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(made.org_id)}
        )
        flagged = await conn.scalar(
            text("SELECT sandbox FROM organisations WHERE id = :o"), {"o": made.org_id}
        )
    assert flagged is True

    auth = {"Authorization": f"Bearer {made.key}"}
    inbox = await api.client.get("/v1/approvals", params={"assigned_to_me": "true"}, headers=auth)
    assert inbox.status_code == 200, inbox.text
    assert {i["id"] for i in inbox.json()["items"]} == set(made.approval_ids)
    assert {i["sla_state"] for i in inbox.json()["items"]} == {"on_track", "overdue"}
    assert made.decision_id is not None
    receipt = await api.client.get(f"/v1/decisions/{made.decision_id}/receipt", headers=auth)
    assert receipt.status_code == 200, receipt.text

    config = McpConfig(api_url="http://api.test", credential=made.key)
    mcp = PairlyApi(config, transport=httpx.ASGITransport(app=api.app))
    mine = await PairlyTools(mcp).list_my_approvals()
    assert len(mine["items"]) == 2
    await mcp.aclose()


async def test_test_keys_never_work_outside_their_sandbox(
    api: Api, migrated: DbUrls, make_org: OrgFactory, owner_engine: AsyncEngine
) -> None:
    made = await create_sandbox(migrated.owner, name="Isolation", slug=_slug())
    normal = await make_org("admin")
    # Pointed at a non-sandbox organisation: refused.
    foreign = await api.client.get(
        "/v1/approvals",
        headers={"Authorization": f"Bearer {made.key}", "X-Pairly-Org": str(normal.id)},
    )
    assert foreign.status_code == 403
    # A test key cannot be created outside a sandbox.
    refused = await api.client.post(
        "/v1/api-keys",
        json={"name": "t", "scopes": ["pay.invoice.read"], "mode": "test"},
        headers=api.person(normal.tenant.subject, normal.id),
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "test_key_not_allowed"
    # The sandbox leaves sandbox mode: its test key stops working.
    await _set_sandbox(owner_engine, made.org_id, False)
    stopped = await api.client.get("/v1/approvals", headers={"Authorization": f"Bearer {made.key}"})
    assert stopped.status_code == 403
    assert stopped.json()["code"] == "test_key_not_allowed"


async def test_live_keys_never_work_in_a_sandbox(
    api: Api, migrated: DbUrls, make_org: OrgFactory, owner_engine: AsyncEngine
) -> None:
    made = await create_sandbox(migrated.owner, name="No live keys", slug=_slug())
    owner = api.person(f"sandbox:{made.org_id}", made.org_id)
    live = await api.client.post(
        "/v1/api-keys", json={"name": "l", "scopes": ["pay.invoice.read"]}, headers=owner
    )
    assert live.status_code == 403
    assert live.json()["code"] == "live_key_in_sandbox"
    test = await api.client.post(
        "/v1/api-keys",
        json={"name": "t", "scopes": ["pay.invoice.read"], "mode": "test"},
        headers=owner,
    )
    assert test.status_code == 201
    # A live key of a normal organisation stops working if that organisation becomes a
    # sandbox, and a live key never reaches a sandbox through the header either.
    normal = await make_org("admin")
    created = await api.client.post(
        "/v1/api-keys",
        json={"name": "l", "scopes": ["pay.invoice.read"]},
        headers=api.person(normal.tenant.subject, normal.id),
    )
    key = created.json()["key"]
    assert key.startswith("pk_live_")
    auth = {"Authorization": f"Bearer {key}"}
    assert (await api.client.get("/v1/pay/invoices", headers=auth)).status_code == 200
    into_sandbox = await api.client.get(
        "/v1/pay/invoices", headers=auth | {"X-Pairly-Org": str(made.org_id)}
    )
    assert into_sandbox.status_code == 403
    await _set_sandbox(owner_engine, normal.id, True)
    flipped = await api.client.get("/v1/pay/invoices", headers=auth)
    assert flipped.status_code == 403
    assert flipped.json()["code"] == "live_key_in_sandbox"


def test_the_cli_prints_the_key_once(
    migrated: DbUrls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(admin.OWNER_URL_ENV, migrated.owner)
    slug = _slug()
    code = admin.main(
        ["sandbox", "create", "--name", "CLI sandbox", "--slug", slug, "--admin-email", "a@b.test"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert f"({slug})" in out
    assert len(re.findall(r"pk_test_[a-z0-9]{8}_[A-Za-z0-9_-]{43}", out)) == 1
    assert "/en/invite#token=" in out
    again = admin.main(["sandbox", "create", "--slug", slug])
    assert again == 1
