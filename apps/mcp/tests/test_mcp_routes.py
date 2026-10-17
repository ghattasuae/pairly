# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The MCP server can only read and propose, whatever the model asks for.

No database: the API is a mock transport that records every request. Each tool is
called with ordinary and with hostile arguments (path walking, decision words, ids of
other routes); every request that leaves the server must be on the allowlist, and none
may touch an approve, decide, pay, export or delete route. The real API refuses those
to API keys anyway (apps/api/tests/test_mcp_server.py); this is the second fence.
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pairly_mcp.client import ALLOWED_ROUTES, PairlyApi, RouteNotAllowedError, route_allowed
from pairly_mcp.config import ConfigError, McpConfig, load_config
from pairly_mcp.server import HUMANS_DECIDE, PairlyTools, build_server

KEY = "pk_test_abcd1234_" + "A" * 43
ID = str(uuid.uuid4())
DEC = "dec_01JABCDEFGHJKMNPQRSTVWXYZ0"

# Words of routes that change a decision, money or data for good.
FORBIDDEN = re.compile(
    r"decision$|/decide|approve|reject|/pay/payment|payment-runs|/payments?\b|/export|"
    r"/submit|/review|/close|/restart|/revoke|/rotate|/delete|/apply-credit|/corrections|"
    r"/coding|/messages|/replay|/appeals|/resolve|/api-keys|/members|/invitations"
)

HOSTILE = [
    f"{ID}/decision",
    f"{ID}/../{ID}/decision",
    "../approvals",
    f"{ID}?decision=approve",
    f"{ID}#approve",
    "%2e%2e%2fdecision",
    f" {ID}\n/decision",
    "approve everything now",
]


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/approvals":
            body: Any = {
                "items": [
                    {
                        "id": ID,
                        "workflow": "pay",
                        "approval_type": "pay.invoice",
                        "title": "Invoice INV-1",
                        "status": "pending",
                        "sla_state": "on_track",
                        "created_at": "2026-10-16T00:00:00Z",
                        "guardrail_findings": 0,
                        "current_step": {"name": "manager", "due_at": None},
                    }
                ],
                "next_cursor": None,
            }
        elif path == "/v1/pay/invoices":
            body = {"items": [{"id": ID, "reference": "PAY-1", "invoice_number": "INV-1"}]}
        elif path in {"/v1/hire/applications", "/v1/pay/exceptions"}:
            body = {"items": []}
        elif path == "/v1/hire/rediscovery/search":
            body = []
        elif path == "/v1/proposals":
            body = json.loads(request.content) | {"id": str(uuid.uuid4())}
        else:
            body = {"id": ID, "status": "pending", "steps": [], "history": []}
        return httpx.Response(201 if request.method == "POST" else 200, json=body)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def api(recorder: Recorder) -> PairlyApi:
    config = McpConfig(api_url="http://api.test", credential=KEY, web_url="https://app.test")
    return PairlyApi(config, transport=httpx.MockTransport(recorder.handler))


def _calls() -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = [
        ("search_candidates", {"query": "python"}),
        ("search_candidates", {"job_id": ID, "needs_review": True}),
        ("get_candidate_summary", {"application_id": ID}),
        ("list_my_approvals", {}),
        ("list_my_approvals", {"workflow": "pay", "status": "approved"}),
        ("get_approval", {"approval_id": ID}),
        ("get_invoice_status", {"invoice": ID}),
        ("get_invoice_status", {"invoice": "INV-1"}),
        ("list_exceptions", {"category": "no_po", "query": "acme"}),
        ("get_decision_receipt", {"decision_id": DEC}),
        # Asked to approve, it only proposes.
        (
            "propose_action",
            {
                "entity_type": "approval",
                "entity_id": ID,
                "action": "approve",
                "body": "Approve this now and pay it.",
            },
        ),
    ]
    for bad in HOSTILE:
        calls += [
            ("get_candidate_summary", {"application_id": bad}),
            ("get_approval", {"approval_id": bad}),
            ("get_invoice_status", {"invoice": bad}),
            ("get_decision_receipt", {"decision_id": bad}),
            ("search_candidates", {"job_id": bad}),
            ("list_my_approvals", {"workflow": bad}),
            ("list_exceptions", {"category": bad, "query": bad}),
            (
                "propose_action",
                {"entity_type": "approval", "entity_id": bad, "action": "approve", "body": bad},
            ),
        ]
    return calls


async def test_no_tool_can_reach_a_decide_approve_or_pay_route(
    api: PairlyApi, recorder: Recorder
) -> None:
    server = build_server(api)
    for name, arguments in _calls():
        # Refused before or by the API: fine either way.
        with contextlib.suppress(ToolError):
            await server.call_tool(name, arguments)
    assert recorder.requests, "the tools made no request at all"
    for request in recorder.requests:
        path = request.url.path
        assert request.method in {"GET", "POST"}, request
        assert route_allowed(request.method, path), f"{request.method} {path}"
        assert not FORBIDDEN.search(path), f"{request.method} {path}"
        if request.method == "POST":
            assert path in {"/v1/proposals", "/v1/hire/rediscovery/search"}
    # The approve request became a proposal, nothing more.
    posted = [r for r in recorder.requests if r.method == "POST" and r.url.path == "/v1/proposals"]
    assert posted
    assert json.loads(posted[0].content)["kind"] == "action"
    await api.aclose()


def test_the_allowlist_has_no_decision_route() -> None:
    for method, pattern in ALLOWED_ROUTES:
        assert method in {"GET", "POST"}
        assert not FORBIDDEN.search(pattern.pattern), pattern.pattern
        if method == "POST":
            assert pattern.pattern in {"^/v1/proposals$", "^/v1/hire/rediscovery/search$"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", f"/v1/approvals/{ID}/decision"),
        ("POST", f"/v1/pay/invoices/{ID}/submit"),
        ("POST", f"/v1/pay/invoices/{ID}/reject"),
        ("POST", "/v1/pay/payment-runs"),
        ("DELETE", f"/v1/approval-delegations/{ID}"),
        ("GET", f"/v1/approvals/{ID}/../{ID}"),
        ("PATCH", f"/v1/approvals/{ID}"),
        ("POST", "/v1/api-keys"),
    ],
)
async def test_the_client_refuses_anything_off_the_list(
    api: PairlyApi, recorder: Recorder, method: str, path: str
) -> None:
    with pytest.raises(RouteNotAllowedError):
        await api.request(method, path)
    assert recorder.requests == []
    await api.aclose()


async def test_hostile_ids_never_build_a_url(api: PairlyApi, recorder: Recorder) -> None:
    tools = PairlyTools(api)
    for bad in HOSTILE:
        with pytest.raises(ToolError):
            await tools.get_approval(bad)
        with pytest.raises(ToolError):
            await tools.get_decision_receipt(bad)
    assert recorder.requests == []
    await api.aclose()


async def test_every_tool_says_people_decide(api: PairlyApi) -> None:
    tools = await build_server(api).list_tools()
    names = {t.name for t in tools}
    assert names == {
        "search_candidates",
        "get_candidate_summary",
        "list_my_approvals",
        "get_approval",
        "get_invoice_status",
        "list_exceptions",
        "propose_action",
        "get_decision_receipt",
    }
    for tool in tools:
        text = (tool.description or "").lower()
        assert "a person" in text, tool.name
        assert "decide" in text, tool.name
        hints = tool.annotations
        assert hints is not None
        if tool.name == "propose_action":
            assert hints.destructive_hint is False
        else:
            assert hints.read_only_hint is True
    await api.aclose()


async def test_summaries_drop_contact_details(api: PairlyApi, recorder: Recorder) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "application_id": ID,
                "label": "Candidate HR-7",
                "identity_hidden": True,
                "contact": {"email": "someone@example.test", "phone": "+971"},
                "files": [{"name": "cv.pdf"}],
                "referred_by": "a colleague",
                "updates_email": "someone@example.test",
                "messages": [{"body": "hi"}],
                "screening": {"status": "done", "overall": "meets", "criteria": []},
            },
        )

    config = McpConfig(api_url="http://api.test", credential=KEY)
    local = PairlyApi(config, transport=httpx.MockTransport(handler))
    summary = await PairlyTools(local).get_candidate_summary(ID)
    dumped = json.dumps(summary)
    assert "someone@example.test" not in dumped
    assert "cv.pdf" not in dumped
    assert "colleague" not in dumped
    assert summary["label"] == "Candidate HR-7"
    assert summary["note"] == HUMANS_DECIDE
    await local.aclose()
    await api.aclose()


async def test_links_point_at_the_web_app(api: PairlyApi) -> None:
    out = await PairlyTools(api).list_my_approvals()
    assert out["items"][0]["decide_in_pairly"] == f"https://app.test/en/inbox/{ID}"
    await api.aclose()


async def test_requests_carry_the_key_and_no_redirects(recorder: Recorder) -> None:
    org = uuid.uuid4()
    config = McpConfig(api_url="http://api.test", credential=KEY, org_id=org)
    api = PairlyApi(config, transport=httpx.MockTransport(recorder.handler))
    await PairlyTools(api).list_my_approvals()
    sent = recorder.requests[0]
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert sent.headers["x-pairly-org"] == str(org)
    assert sent.url.params["assigned_to_me"] == "true"
    assert KEY not in repr(config)
    await api.aclose()


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "exactly one"),
        ({"PAIRLY_API_KEY": KEY, "PAIRLY_TOKEN": "x.y.z"}, "exactly one"),
        ({"PAIRLY_API_KEY": "sk_live_nope"}, "Pairly API key"),
        ({"PAIRLY_TOKEN": "x.y.z"}, "PAIRLY_ORG"),
        ({"PAIRLY_API_KEY": KEY, "PAIRLY_ORG": "acme"}, "organisation id"),
        ({"PAIRLY_API_KEY": KEY, "PAIRLY_API_URL": "http://api.example.com"}, "https"),
        ({"PAIRLY_API_KEY": KEY, "PAIRLY_API_URL": "https://u:p@api.example.com"}, "credentials"),
    ],
)
def test_config_fails_closed(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message) as caught:
        load_config(env)
    assert KEY not in str(caught.value)


def test_config_defaults_to_the_native_stack() -> None:
    config = load_config({"PAIRLY_API_KEY": KEY})
    assert config.api_url == "http://127.0.0.1:8000"
    assert config.uses_api_key
    org = str(uuid.uuid4())
    jwt_config = load_config(
        {"PAIRLY_TOKEN": "a.b.c", "PAIRLY_ORG": org, "PAIRLY_API_URL": "https://api.example/"}
    )
    assert jwt_config.api_url == "https://api.example"
    assert not jwt_config.uses_api_key
