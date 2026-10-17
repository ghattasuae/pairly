# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Approval inbox, decisions, signed links, delegations and rules over HTTP (WP1.8)."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_api.main import create_app
from pairly_api.routes.approvals import _finding_count
from pairly_core.approvals.models import Channel, Decision
from pairly_core.db import set_tenant
from pairly_core.signed_links import IssuedLink, issue_link
from pairly_core.workflow.runtime import RunView, WorkflowRuntime

from .conftest import DbUrls, TokenFactory, make_settings
from .wf_support import (
    EXECUTED,
    PLUGINS,
    World,
    audit_actions,
    other_world,
    owner_sql,
    start,
    world,
)

__all__ = ["other_world", "world"]  # fixtures

OTP = ["otp"]


@dataclasses.dataclass
class Api:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory

    @property
    def runtime(self) -> WorkflowRuntime:
        runtime = self.app.state.workflow_runtime
        assert isinstance(runtime, WorkflowRuntime)
        return runtime

    def headers(
        self,
        world: World,
        name: str,
        *,
        otp: bool = True,
        org: bool = True,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        claims: dict[str, Any] = {"amr": OTP} if otp else {}
        headers = {
            "Authorization": f"Bearer {self.tokens.token(world.people[name].subject, **claims)}"
        }
        if org:
            headers["X-Pairly-Org"] = str(world.org_id)
        return headers | (extra or {})

    async def decide(
        self,
        world: World,
        name: str,
        approval_id: Any,
        decision: str = "approve",
        *,
        key: str | None = None,
        reason: str | None = None,
        otp: bool = True,
    ) -> httpx.Response:
        body: dict[str, Any] = {"decision": decision}
        if reason is not None:
            body["reason"] = reason
        return await self.client.post(
            f"/v1/approvals/{approval_id}/decision",
            json=body,
            headers=self.headers(
                world, name, otp=otp, extra={"Idempotency-Key": key or f"key-{uuid.uuid4().hex}"}
            ),
        )

    async def describe(self, token: str, headers: dict[str, str]) -> httpx.Response:
        return await self.client.post(
            "/v1/approval-links/describe", json={"token": token}, headers=headers
        )

    async def confirm(
        self, token: str, headers: dict[str, str], *, reason: str | None = None
    ) -> httpx.Response:
        body: dict[str, Any] = {"token": token}
        if reason is not None:
            body["reason"] = reason
        return await self.client.post("/v1/approval-links/confirm", json=body, headers=headers)

    async def link(
        self, world: World, approval_id: Any, user: str, action: Decision = Decision.APPROVE
    ) -> IssuedLink:
        async with self.runtime.sessionmaker() as session:
            set_tenant(session, world.org_id, None)
            async with session.begin():
                return await issue_link(
                    session,
                    approval_id=uuid.UUID(str(approval_id)),
                    user_id=world.id(user),
                    action=action,
                    channel=Channel.EMAIL_LINK,
                )


@pytest.fixture
async def api(migrated: DbUrls, tokens: TokenFactory) -> AsyncIterator[Api]:
    app = create_app(make_settings(migrated.app), plugins=PLUGINS)
    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield Api(app, client, tokens)


async def _start(api: Api, world: World, amount: str = "120", **kw: Any) -> RunView:
    return await start(api.runtime, world, amount, **kw)


async def _run_status(api: Api, world: World, run_id: uuid.UUID) -> str:
    response = await api.client.get(
        f"/v1/workflow-runs/{run_id}", headers=api.headers(world, "clerk")
    )
    assert response.status_code == 200, response.text
    return str(response.json()["status"])


# ------------------------------------------------------------------ inbox


@pytest.mark.parametrize(
    ("context", "count"),
    [
        ({}, 0),
        ({"guardrail_findings": []}, 0),
        ({"guardrail_findings": [{"id": "g1"}, {"id": "g2"}]}, 2),
        ({"guardrail_findings": [{"id": "g1"}, "junk", 3, None]}, 1),
        ({"guardrail_findings": {"id": "g1"}}, 0),
        ([], 0),
    ],
)
def test_finding_count_reads_the_context_convention(context: Any, count: int) -> None:
    assert _finding_count(context) == count


async def test_inbox_lists_only_workflows_with_the_permission(api: Api, world: World) -> None:
    runs = [await _start(api, world, str(100 + i)) for i in range(3)]
    ids = [str(r.pending_approval_id) for r in reversed(runs)]

    mine = await api.client.get("/v1/approvals", headers=api.headers(world, "appr1"))
    assert mine.status_code == 200, mine.text
    assert [i["id"] for i in mine.json()["items"]] == ids
    item = mine.json()["items"][0]
    assert item["sla_state"] == "on_track"
    assert item["current_step"]["name"] == "manager"
    assert item["guardrail_findings"] == 0

    # Approver only for "pay": nothing from testwf, and the detail is hidden.
    other = await api.client.get("/v1/approvals", headers=api.headers(world, "payonly"))
    assert other.json()["items"] == []
    hidden = await api.client.get(f"/v1/approvals/{ids[0]}", headers=api.headers(world, "payonly"))
    assert hidden.status_code == 404
    # The clerk proposes but does not decide: no inbox.
    clerk = await api.client.get("/v1/approvals", headers=api.headers(world, "clerk"))
    assert clerk.json()["items"] == []

    # Filters and cursor pagination.
    page1 = await api.client.get(
        "/v1/approvals", params={"limit": 2}, headers=api.headers(world, "appr1")
    )
    cursor = page1.json()["next_cursor"]
    assert [i["id"] for i in page1.json()["items"]] == ids[:2]
    assert cursor
    page2 = await api.client.get(
        "/v1/approvals", params={"limit": 2, "cursor": cursor}, headers=api.headers(world, "appr1")
    )
    assert [i["id"] for i in page2.json()["items"]] == ids[2:]
    assert page2.json()["next_cursor"] is None
    assert (
        await api.client.get(
            "/v1/approvals", params={"workflow": "pay"}, headers=api.headers(world, "appr1")
        )
    ).json()["items"] == []
    assigned = await api.client.get(
        "/v1/approvals", params={"assigned_to_me": "true"}, headers=api.headers(world, "fc1")
    )
    assert assigned.json()["items"] == []  # finance controllers are not on the small chain
    approved = await api.client.get(
        "/v1/approvals", params={"status": "approved"}, headers=api.headers(world, "appr1")
    )
    assert approved.json()["items"] == []
    bad = await api.client.get(
        "/v1/approvals", params={"cursor": "%%%"}, headers=api.headers(world, "appr1")
    )
    assert bad.status_code == 400


async def test_detail_has_context_chain_and_history(api: Api, world: World) -> None:
    run = await _start(api, world, "5000")
    first = await api.decide(world, "appr1", run.pending_approval_id)
    assert first.status_code == 200, first.text
    detail = await api.client.get(
        f"/v1/approvals/{run.pending_approval_id}", headers=api.headers(world, "fc1")
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["payload"] == {"amount": "5000", "note": "team lunch"}
    assert body["context"]["note"] == "team lunch"
    assert body["chain_name"] == "large"
    assert body["policy_version"] == 1
    assert [s["name"] for s in body["steps"]] == ["manager", "finance"]
    assert body["current_step"]["name"] == "finance"
    assert [h["decision"] for h in body["history"]] == ["approve"]
    assert body["can_decide"] is True
    assert body["needs_second_factor"] is False


# ------------------------------------------------------------------ decisions


async def test_decision_needs_totp_and_resumes_the_run(
    api: Api, world: World, owner_engine: AsyncEngine
) -> None:
    run = await _start(api, world)
    weak = await api.decide(world, "appr1", run.pending_approval_id, otp=False)
    assert weak.status_code == 403
    assert weak.json()["code"] == "second_factor_required"
    assert "core.authz.denied" in await audit_actions(owner_engine, world.org_id)

    ok = await api.decide(world, "appr1", run.pending_approval_id)
    assert ok.status_code == 200, ok.text
    assert ok.json()["approval_status"] == "approved"
    assert await _run_status(api, world, run.id) == "completed"
    assert [c.run_id for c in EXECUTED] == [run.id]


async def test_decision_rules(api: Api, world: World) -> None:
    run = await _start(api, world)
    missing_key = await api.client.post(
        f"/v1/approvals/{run.pending_approval_id}/decision",
        json={"decision": "approve"},
        headers=api.headers(world, "appr1"),
    )
    assert missing_key.status_code == 400
    no_reason = await api.decide(world, "appr1", run.pending_approval_id, "reject")
    assert no_reason.status_code == 400
    sod = await api.decide(world, "clerk", run.pending_approval_id)
    assert sod.status_code == 403  # the clerk holds no approve permission at all
    rejected = await api.decide(world, "appr2", run.pending_approval_id, "reject", reason="dup")
    assert rejected.json()["approval_status"] == "rejected"
    assert await _run_status(api, world, run.id) == "completed"
    assert EXECUTED == []


async def test_idempotent_decisions(api: Api, world: World) -> None:
    run = await _start(api, world, "5000")
    key = f"key-{uuid.uuid4().hex}"
    first = await api.decide(world, "appr1", run.pending_approval_id, key=key)
    again = await api.decide(world, "appr1", run.pending_approval_id, key=key)
    assert first.status_code == again.status_code == 200
    assert again.json()["decision_id"] == first.json()["decision_id"]
    assert (first.json()["replayed"], again.json()["replayed"]) == (False, True)
    conflict = await api.decide(
        world, "appr1", run.pending_approval_id, "reject", key=key, reason="x"
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_key_reused"


async def test_concurrent_double_submit_records_one_decision(
    api: Api, world: World, owner_engine: AsyncEngine
) -> None:
    run = await _start(api, world)
    key = f"key-{uuid.uuid4().hex}"
    results = await asyncio.gather(
        api.decide(world, "appr1", run.pending_approval_id, key=key),
        api.decide(world, "appr1", run.pending_approval_id, key=key),
    )
    assert [r.status_code for r in results] == [200, 200]
    assert {r.json()["decision_id"] for r in results} == {results[0].json()["decision_id"]}
    assert sorted(r.json()["replayed"] for r in results) == [False, True]
    rows = await owner_sql(
        owner_engine,
        world.org_id,
        "SELECT count(*) AS n FROM approval_decisions WHERE approval_id = :a",
        {"a": run.pending_approval_id},
    )
    assert rows[0].n == 1
    # Different keys at once: one decision, the other is told it already decided.
    run2 = await _start(api, world)
    both = await asyncio.gather(
        api.decide(world, "appr2", run2.pending_approval_id),
        api.decide(world, "appr2", run2.pending_approval_id),
    )
    assert sorted(r.status_code for r in both) == [200, 409]


async def test_other_tenant_sees_nothing(api: Api, world: World, other_world: World) -> None:
    run = await _start(api, world)
    path = f"/v1/approvals/{run.pending_approval_id}"
    assert (
        await api.client.get(path, headers=api.headers(other_world, "appr1"))
    ).status_code == 404
    assert (await api.decide(other_world, "appr1", run.pending_approval_id)).status_code == 404
    listed = await api.client.get("/v1/approvals", headers=api.headers(other_world, "appr1"))
    assert str(run.pending_approval_id) not in [i["id"] for i in listed.json()["items"]]
    runs = await api.client.get(
        f"/v1/workflow-runs/{run.id}", headers=api.headers(other_world, "clerk")
    )
    assert runs.status_code == 404


# ------------------------------------------------------------------ signed links


async def test_link_describe_is_safe_and_confirm_decides_once(
    api: Api, world: World, owner_engine: AsyncEngine
) -> None:
    run = await _start(api, world)
    link = await api.link(world, run.pending_approval_id, "appr1")
    bound = api.headers(world, "appr1", org=False)

    # No sign-in: nothing.
    assert (await api.describe(link.token, {})).status_code == 401
    for _ in range(3):  # opening the link (or a mail scanner) changes nothing
        seen = await api.describe(link.token, bound)
        assert seen.status_code == 200, seen.text
        assert seen.json()["state"] == "valid"
        assert seen.json()["action"] == "approve"
        assert seen.json()["second_factor_required"] is True
    rows = await owner_sql(
        owner_engine,
        world.org_id,
        "SELECT used_at FROM approval_links WHERE id = :i",
        {"i": link.id},
    )
    assert rows[0].used_at is None
    assert await _run_status(api, world, run.id) == "waiting_approval"

    # Another person's session cannot use it.
    stranger = await api.confirm(link.token, api.headers(world, "appr2", org=False))
    assert stranger.status_code == 410
    # The bound user without TOTP cannot either.
    weak = await api.confirm(link.token, api.headers(world, "appr1", otp=False, org=False))
    assert weak.status_code == 403
    assert weak.json()["code"] == "second_factor_required"

    used = await api.confirm(link.token, bound)
    assert used.status_code == 200, used.text
    assert used.json()["approval_status"] == "approved"
    replay = await api.confirm(link.token, bound)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["decision_id"] == used.json()["decision_id"]
    assert (await api.describe(link.token, bound)).json()["state"] == "used"
    assert await _run_status(api, world, run.id) == "completed"
    actions = await audit_actions(owner_engine, world.org_id)
    assert "core.approval.link_issued" in actions
    assert "core.approval.link_used" in actions


async def test_link_expiry_invalidation_and_unknown(
    api: Api, world: World, other_world: World, owner_engine: AsyncEngine
) -> None:
    run = await _start(api, world)
    expired = await api.link(world, run.pending_approval_id, "appr1")
    await owner_sql(
        owner_engine,
        world.org_id,
        "UPDATE approval_links SET created_at = now() - interval '2 hours', "
        "expires_at = now() - interval '1 hour' WHERE id = :i",
        {"i": expired.id},
    )
    headers = api.headers(world, "appr1", org=False)
    gone = await api.confirm(expired.token, headers)
    assert gone.status_code == 410
    assert gone.json()["code"] == "link_invalid"

    # A state change (someone else approves) kills open links.
    stale = await api.link(world, run.pending_approval_id, "appr1", Decision.REJECT)
    assert (await api.decide(world, "appr2", run.pending_approval_id)).status_code == 200
    assert (await api.describe(stale.token, headers)).json()["state"] == "invalidated"
    assert (await api.confirm(stale.token, headers, reason="late")).status_code == 410

    assert (await api.describe("A" * 43, headers)).status_code == 410
    assert (await api.describe("short", headers)).status_code == 422
    # Tokens are never accepted in the URL.
    in_url = await api.client.get(f"/v1/approval-links/{expired.token}", headers=headers)
    assert in_url.status_code in {404, 405}

    # A link of org A used by a member of org B: same answer as unknown.
    run2 = await _start(api, world)
    foreign = await api.link(world, run2.pending_approval_id, "appr1")
    outsider = api.headers(other_world, "appr1", org=False)
    assert (await api.confirm(foreign.token, outsider)).status_code == 410


async def test_link_issue_refuses_people_who_cannot_decide(api: Api, world: World) -> None:
    run = await _start(api, world, preparers=["appr2"])
    for name in ("clerk", "appr2", "fc1"):
        with pytest.raises(Exception, match=r"cannot decide|not an approver"):
            await api.link(world, run.pending_approval_id, name)


# ------------------------------------------------------------------ delegations and rules


async def test_delegation_api(api: Api, world: World) -> None:
    body = {
        "delegate_id": str(world.id("fc1")),
        "kind": "out_of_office",
        "starts_at": "2026-01-01T00:00:00Z",
        "ends_at": "2026-01-10T00:00:00Z",
        "reason": "holiday",
    }
    created = await api.client.post(
        "/v1/approval-delegations", json=body, headers=api.headers(world, "appr1")
    )
    assert created.status_code == 201, created.text
    listed = await api.client.get("/v1/approval-delegations", headers=api.headers(world, "fc1"))
    assert [d["id"] for d in listed.json()["items"]] == [created.json()["id"]]
    # Only the delegator can revoke it.
    other = await api.client.delete(
        f"/v1/approval-delegations/{created.json()['id']}", headers=api.headers(world, "fc1")
    )
    assert other.status_code == 404
    revoked = await api.client.delete(
        f"/v1/approval-delegations/{created.json()['id']}", headers=api.headers(world, "appr1")
    )
    assert revoked.json()["revoked_at"] is not None
    self_delegation = await api.client.post(
        "/v1/approval-delegations",
        json=body | {"delegate_id": str(world.id("appr1"))},
        headers=api.headers(world, "appr1"),
    )
    assert self_delegation.status_code == 400


async def test_rules_versions(api: Api, world: World, owner_engine: AsyncEngine) -> None:
    first = await _start(api, world, "100")
    stricter = (
        "schema: pairly.approval_rules/v1\n"
        "approval_type: testwf.expense\n"
        "chains:\n"
        "  - name: everything\n"
        "    steps:\n"
        "      - {name: finance, mode: any_of, roles: [finance_controller]}\n"
    )
    path = "/v1/approval-rules/testwf.expense"
    denied = await api.client.post(
        path, json={"content": stricter}, headers=api.headers(world, "appr1")
    )
    assert denied.status_code == 403
    invalid = await api.client.post(
        path, json={"content": "chains: []"}, headers=api.headers(world, "admin")
    )
    assert invalid.status_code == 400
    published = await api.client.post(
        path, json={"content": stricter}, headers=api.headers(world, "admin")
    )
    assert published.status_code == 201, published.text
    assert published.json()["version"] == 2

    second = await _start(api, world, "100")
    versions = await owner_sql(
        owner_engine,
        world.org_id,
        "SELECT a.id, p.version, a.chain_name FROM approvals a "
        "JOIN policy_versions p ON p.id = a.policy_version_id WHERE a.id IN (:x, :y)",
        {"x": first.pending_approval_id, "y": second.pending_approval_id},
    )
    by_id = {r.id: (r.version, r.chain_name) for r in versions}
    assert by_id[first.pending_approval_id] == (1, "small")
    assert by_id[second.pending_approval_id] == (2, "everything")
    listed = await api.client.get(path, headers=api.headers(world, "appr1"))
    assert [v["version"] for v in listed.json()["items"]] == [2, 1]
