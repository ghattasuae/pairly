# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The MCP server against the real API (in process, real Postgres), and proposals.

* "Which approvals are waiting for me?" through a personal API key (``core.approval.read``:
  the key reads its creator's inbox) and through a person's JWT.
* ``propose_action`` asked to approve only records a proposal: the approval stays pending.
* The API key itself is refused the decision route (the server-side fence), and the MCP
  client refuses to send it at all (the local fence).
* Proposals: validation, listing, audit, and that an API key without the scope is refused.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_api.devseed import seed_approval
from pairly_api.main import create_app
from pairly_mcp.client import PairlyApi, RouteNotAllowedError
from pairly_mcp.config import McpConfig
from pairly_mcp.server import PairlyTools
from pairly_pay.plugin import plugin as pay_plugin

from .conftest import DbUrls, TokenFactory, make_settings
from .policy_support import Org

OTP = {"amr": ["otp"]}
MCP_SCOPES = [
    "core.approval.read",
    "core.proposal.propose",
    "core.proposal.read",
    "pay.invoice.read",
]


@dataclasses.dataclass
class Api:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory

    def h(self, subject: str, org: uuid.UUID) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tokens.token(subject, **OTP)}",
            "X-Pairly-Org": str(org),
        }

    async def key(self, org: Org, scopes: list[str]) -> str:
        created = await self.client.post(
            "/v1/api-keys",
            json={"name": "mcp", "scopes": scopes},
            headers=self.h(org.tenant.subject, org.id),
        )
        assert created.status_code == 201, created.text
        return str(created.json()["key"])

    def mcp(self, credential: str, org: uuid.UUID | None = None) -> PairlyApi:
        config = McpConfig(
            api_url="http://api.test", credential=credential, org_id=org, web_url="https://app.test"
        )
        return PairlyApi(config, transport=httpx.ASGITransport(app=self.app))


@pytest.fixture
async def api(migrated: DbUrls, tokens: TokenFactory) -> AsyncIterator[Api]:
    app = create_app(make_settings(migrated.app), plugins=[pay_plugin])
    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield Api(app, client, tokens)


OrgFactory = Callable[[str], Awaitable[Org]]


async def _slug_and_email(owner_engine: AsyncEngine, org: Org) -> tuple[str, str]:
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.id)})
        slug = await conn.scalar(
            text("SELECT slug FROM organisations WHERE id = :o"), {"o": org.id}
        )
        email = await conn.scalar(
            text("SELECT email FROM users WHERE id = :u"), {"u": org.tenant.user_id}
        )
    return str(slug), str(email)


@pytest.fixture
async def world(
    make_org: OrgFactory, owner_engine: AsyncEngine, migrated: DbUrls
) -> dict[str, Any]:
    """An admin who is also a Pay approver, with one pending invoice approval for them."""
    org = await make_org("admin")
    slug, email = await _slug_and_email(owner_engine, org)
    seeded = await seed_approval(
        migrated.owner,
        org_slug=slug,
        approver_email=email,
        title="Invoice INV-MCP-1 from Sample Supplies LLC",
        overdue=False,
        link=None,
        decision=False,
    )
    return {"org": org, "approval_id": seeded["approval_id"]}


async def test_which_approvals_are_waiting_for_me_with_a_personal_key(
    api: Api, world: dict[str, Any]
) -> None:
    org: Org = world["org"]
    approval_id = world["approval_id"]
    mcp = api.mcp(await api.key(org, MCP_SCOPES))
    tools = PairlyTools(mcp)

    mine = await tools.list_my_approvals()
    assert [i["id"] for i in mine["items"]] == [approval_id]
    item = mine["items"][0]
    assert item["status"] == "pending"
    assert item["decide_in_pairly"] == f"https://app.test/en/inbox/{approval_id}"
    assert "decides" in mine["note"]

    detail = await tools.get_approval(approval_id)
    assert detail["id"] == approval_id
    assert detail["payload"]["vendor"] == "Sample Supplies LLC"

    # Asked to approve, the server proposes; the approval is untouched.
    proposed = await tools.propose_action(
        "approval", approval_id, "The amount matches the PO. Please approve.", action="approve"
    )
    assert proposed["kind"] == "action"
    assert proposed["action"] == "approve"
    assert proposed["workflow"] == "pay"
    assert proposed["status"].startswith("proposed")
    assert (await tools.get_approval(approval_id))["status"] == "pending"

    # The local fence: the client will not send a decision at all.
    with pytest.raises(RouteNotAllowedError):
        await mcp.request(
            "POST", f"/v1/approvals/{approval_id}/decision", json={"decision": "approve"}
        )
    await mcp.aclose()


async def test_the_api_refuses_the_decision_to_any_api_key(api: Api, world: dict[str, Any]) -> None:
    org: Org = world["org"]
    approval_id = world["approval_id"]
    key = await api.key(org, MCP_SCOPES)
    for body in ({"decision": "approve"}, {"decision": "reject", "reason": "no"}):
        denied = await api.client.post(
            f"/v1/approvals/{approval_id}/decision",
            json=body,
            headers={"Authorization": f"Bearer {key}", "Idempotency-Key": uuid.uuid4().hex},
        )
        assert denied.status_code == 403, denied.text
    # A key can never be given a decide scope in the first place.
    refused = await api.client.post(
        "/v1/api-keys",
        json={"name": "nope", "scopes": ["pay.invoice.approve"]},
        headers=api.h(org.tenant.subject, org.id),
    )
    assert refused.status_code == 400
    still = await api.client.get(
        f"/v1/approvals/{approval_id}", headers=api.h(org.tenant.subject, org.id)
    )
    assert still.json()["status"] == "pending"
    assert still.json()["can_decide"] is True  # the person still can, in Pairly


async def test_a_key_never_can_decide_from_the_inbox_view(api: Api, world: dict[str, Any]) -> None:
    org: Org = world["org"]
    key = await api.key(org, MCP_SCOPES)
    shown = await api.client.get(
        f"/v1/approvals/{world['approval_id']}", headers={"Authorization": f"Bearer {key}"}
    )
    assert shown.status_code == 200
    assert shown.json()["can_decide"] is False


async def test_without_the_inbox_scope_a_key_sees_nothing(api: Api, world: dict[str, Any]) -> None:
    org: Org = world["org"]
    mcp = api.mcp(await api.key(org, ["pay.invoice.read"]))
    with pytest.raises(ToolError, match="403"):
        await PairlyTools(mcp).list_my_approvals()
    await mcp.aclose()


async def test_the_same_question_with_a_persons_jwt(api: Api, world: dict[str, Any]) -> None:
    org: Org = world["org"]
    token = api.tokens.token(org.tenant.subject, **OTP)
    mcp = api.mcp(token, org.id)
    mine = await PairlyTools(mcp).list_my_approvals(workflow="pay")
    assert [i["id"] for i in mine["items"]] == [world["approval_id"]]
    await mcp.aclose()


async def test_another_organisations_key_sees_none_of_it(
    api: Api, world: dict[str, Any], make_org: OrgFactory
) -> None:
    other = await make_org("admin")
    mcp = api.mcp(await api.key(other, MCP_SCOPES))
    tools = PairlyTools(mcp)
    assert (await tools.list_my_approvals())["items"] == []
    with pytest.raises(ToolError, match="404"):
        await tools.get_approval(world["approval_id"])
    with pytest.raises(ToolError, match="404"):
        await tools.propose_action("approval", world["approval_id"], "approve", action="approve")
    await mcp.aclose()


# ------------------------------------------------------------------ proposals


async def test_proposals_are_validated_listed_and_audited(
    api: Api, world: dict[str, Any], owner_engine: AsyncEngine
) -> None:
    org: Org = world["org"]
    key = await api.key(org, MCP_SCOPES)
    auth = {"Authorization": f"Bearer {key}"}
    note = await api.client.post(
        "/v1/proposals",
        json={"entity_type": "pay_invoice", "entity_id": "PAY-0001", "body": "Vendor called."},
        headers=auth,
    )
    assert note.status_code == 201, note.text
    assert note.json()["kind"] == "note"
    assert note.json()["proposed_by_type"] == "api_key"
    bad = [
        {"entity_type": "pay_invoice", "entity_id": "x", "kind": "action", "body": "b"},
        {"entity_type": "pay_invoice", "entity_id": "x", "action": "approve", "body": "b"},
        {"entity_type": "Pay Invoice", "entity_id": "x", "body": "b"},
        {"entity_type": "pay_invoice", "entity_id": "a/b", "body": "b"},
        {"entity_type": "pay_invoice", "entity_id": "x", "body": "   "},
        {"entity_type": "approval", "entity_id": "not-a-uuid", "body": "b"},
        {"entity_type": "pay_invoice", "entity_id": "x", "body": "b", "workflow": "nope"},
    ]
    for body in bad:
        refused = await api.client.post("/v1/proposals", json=body, headers=auth)
        assert refused.status_code in {400, 422}, (body, refused.text)
    listed = await api.client.get(
        "/v1/proposals", params={"entity_type": "pay_invoice"}, headers=auth
    )
    assert [p["entity_id"] for p in listed.json()["items"]] == ["PAY-0001"]
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.id)})
        events = await conn.scalar(
            text(
                "SELECT count(*) FROM audit_events WHERE org_id = :o "
                "AND action = 'core.proposal.created' AND actor_type = 'api_key'"
            ),
            {"o": org.id},
        )
    assert events == 1
    # Append only: the application role cannot change or remove a proposal.
    async with owner_engine.begin() as conn:
        grants = await conn.scalar(
            text(
                "SELECT string_agg(privilege_type, ',' ORDER BY privilege_type) "
                "FROM information_schema.role_table_grants "
                "WHERE table_name = 'proposals' AND grantee = 'pairly_app'"
            )
        )
    assert grants == "INSERT,SELECT"


async def test_proposing_needs_the_scope(api: Api, world: dict[str, Any]) -> None:
    org: Org = world["org"]
    key = await api.key(org, ["pay.invoice.read"])
    refused = await api.client.post(
        "/v1/proposals",
        json={"entity_type": "pay_invoice", "entity_id": "PAY-1", "body": "hello"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert refused.status_code == 403
