# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The client without a server: retries and backoff, idempotency keys, pagination,
errors, configuration, and the requests the helpers build (sync and async)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from pairly import (
    AsyncPairly,
    AuthenticationError,
    NotFoundError,
    Pairly,
    PairlyConfigError,
    PairlyConnectionError,
    PermissionDeniedError,
    RateLimitError,
    RetryPolicy,
    ServerError,
)

KEY = "pk_test_abcd1234_" + "A" * 43
BATCH_ID = str(uuid.uuid4())


def batch(status: str = "completed") -> dict[str, Any]:
    return {
        "id": BATCH_ID,
        "workflow": "pay",
        "status": status,
        "item_count": 1,
        "metadata": {},
        "counts": {"accepted": 1},
        "items": [
            {
                "id": str(uuid.uuid4()),
                "position": 0,
                "source": "file",
                "url": None,
                "filename": "inv.pdf",
                "status": "accepted",
                "reason": None,
                "document_id": "d1",
                "entity_type": "pay.invoice",
                "entity_id": "i1",
                "completed_at": None,
            }
        ],
        "created_at": "2026-10-16T00:00:00Z",
        "completed_at": None,
    }


class Script:
    """A mock API that answers from a list and records what it got."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def sync_client(script: Script, **kwargs: Any) -> tuple[Pairly, list[float]]:
    slept: list[float] = []
    client = Pairly(
        api_key=KEY,
        transport=httpx.MockTransport(script),
        retry=RetryPolicy(max_retries=kwargs.pop("max_retries", 3), jitter=lambda: 1.0),
        sleep=slept.append,
        **kwargs,
    )
    return client, slept


def problem(status: int, code: str, **headers: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"type": "about:blank", "title": "t", "status": status, "detail": "d", "code": code},
        headers=headers,
    )


def test_429_and_5xx_are_retried_with_backoff_and_one_idempotency_key() -> None:
    script = Script(
        problem(429, "rate_limited", **{"Retry-After": "2"}),
        problem(503, "unavailable"),
        httpx.Response(202, json=batch()),
    )
    client, slept = sync_client(script)
    job = client.documents.submit(b"%PDF-1.7 fake", workflow="pay", filename="inv.pdf")
    assert job.job_id == BATCH_ID
    assert job.entity_type == "pay.invoice"
    assert job.entity_id == "i1"
    assert slept == [2.0, 1.0]  # Retry-After, then base_delay * 2 ** 1 * jitter
    keys = {r.headers["idempotency-key"] for r in script.requests}
    assert len(script.requests) == 3
    assert len(keys) == 1
    first = script.requests[0]
    assert first.url.path == "/v1/documents/batch"
    assert first.url.params["workflow"] == "pay"
    assert first.headers["authorization"] == f"Bearer {KEY}"
    assert b'name="file"; filename="inv.pdf"' in first.content


def test_retries_stop_and_raise_the_last_error() -> None:
    script = Script(problem(429, "rate_limited", **{"Retry-After": "1"}))
    client, slept = sync_client(script, max_retries=2)
    with pytest.raises(RateLimitError) as caught:
        client.approvals.list()
    assert caught.value.retry_after == 1.0
    assert caught.value.code == "rate_limited"
    assert len(script.requests) == 3
    assert slept == [1.0, 1.0]
    script = Script(problem(500, "boom"))
    client, _ = sync_client(script, max_retries=1)
    with pytest.raises(ServerError):
        client.approvals.list()
    assert len(script.requests) == 2


def test_client_errors_are_not_retried() -> None:
    for status, error, code in (
        (401, AuthenticationError, "invalid_api_key"),
        (403, PermissionDeniedError, "api_key_action_not_allowed"),
        (404, NotFoundError, "not_found"),
    ):
        script = Script(problem(status, code))
        client, slept = sync_client(script)
        with pytest.raises(error) as caught:
            client.approvals.get(uuid.uuid4())
        assert caught.value.status == status
        assert caught.value.code == code
        assert len(script.requests) == 1
        assert slept == []


def test_connection_errors_are_retried_then_raised() -> None:
    script = Script(httpx.ConnectError("down"))
    client, slept = sync_client(script, max_retries=2)
    with pytest.raises(PairlyConnectionError):
        client.approvals.list()
    assert len(script.requests) == 3
    assert slept == [0.5, 1.0]


def test_backoff_is_capped_and_jittered() -> None:
    policy = RetryPolicy(base_delay=0.5, max_delay=4.0, jitter=lambda: 0.5)
    assert [policy.delay(n, None) for n in (1, 2, 3, 4, 5)] == [0.25, 0.5, 1.0, 2.0, 2.0]
    assert policy.delay(1, 600.0) == 60.0


def test_list_mine_follows_every_page() -> None:
    def page(ids: list[str], cursor: str | None) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"id": i} for i in ids], "next_cursor": cursor})

    script = Script(page(["a", "b"], "c1"), page(["c"], "c2"), page([], None))
    client, _ = sync_client(script)
    mine = client.approvals.list_mine(workflow="pay")
    assert [a["id"] for a in mine] == ["a", "b", "c"]
    params = [dict(r.url.params) for r in script.requests]
    assert params[0] == {
        "workflow": "pay",
        "status": "pending",
        "assigned_to_me": "true",
        "limit": "200",
    }
    assert params[1]["cursor"] == "c1"
    assert params[2]["cursor"] == "c2"


def test_urls_go_as_json_and_metadata_rides_along() -> None:
    script = Script(httpx.Response(202, json=batch("processing")))
    client, _ = sync_client(script)
    client.documents.submit_batch(
        urls=["https://files.example.com/a.pdf"], workflow="pay", metadata={"run": 1}
    )
    sent = script.requests[0]
    assert json.loads(sent.content) == {
        "urls": ["https://files.example.com/a.pdf"],
        "metadata": {"run": 1},
    }


def test_wait_for_batch_polls_until_completed() -> None:
    script = Script(
        httpx.Response(200, json=batch("processing")), httpx.Response(200, json=batch())
    )
    client, slept = sync_client(script)
    done = client.documents.wait_for_batch(BATCH_ID, interval=0.25)
    assert done["status"] == "completed"
    assert slept == [0.25]


def test_proposals_are_notes_or_actions() -> None:
    script = Script(httpx.Response(201, json={"id": "p1"}))
    client, _ = sync_client(script)
    client.proposals.create("approval", BATCH_ID, "Looks right", action="approve")
    assert json.loads(script.requests[0].content) == {
        "entity_type": "approval",
        "entity_id": BATCH_ID,
        "kind": "action",
        "action": "approve",
        "body": "Looks right",
    }


def test_ids_are_checked_before_a_url_is_built() -> None:
    script = Script(httpx.Response(200, json={}))
    client, _ = sync_client(script)
    with pytest.raises(ValueError, match="UUID"):
        client.approvals.get("../approvals/x/decision")
    with pytest.raises(ValueError, match="dec_"):
        client.decisions.receipt("dec_../../x")
    assert script.requests == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "exactly one"),
        ({"api_key": "sk_live_x"}, "Pairly API key"),
        ({"token": "a.b.c"}, "org_id"),
        ({"api_key": KEY, "org_id": "acme"}, "UUID"),
        ({"api_key": KEY, "base_url": "http://api.example.com"}, "https"),
        ({"api_key": KEY, "base_url": "ftp://x"}, "http"),
    ],
)
def test_configuration_fails_closed(
    kwargs: dict[str, Any], message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("PAIRLY_API_KEY", "PAIRLY_TOKEN", "PAIRLY_ORG", "PAIRLY_API_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(PairlyConfigError, match=message):
        Pairly(**kwargs)


def test_configuration_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    org = str(uuid.uuid4())
    monkeypatch.setenv("PAIRLY_TOKEN", "a.b.c")
    monkeypatch.setenv("PAIRLY_ORG", org)
    monkeypatch.setenv("PAIRLY_API_URL", "https://api.example.com/")
    monkeypatch.delenv("PAIRLY_API_KEY", raising=False)
    client = Pairly()
    assert client.config.base_url == "https://api.example.com"
    assert client.config.headers()["X-Pairly-Org"] == org
    assert "a.b.c" not in repr(client.config)
    client.close()


async def test_the_async_client_has_the_same_behaviour() -> None:
    script = Script(
        problem(502, "bad_gateway"),
        httpx.Response(200, json={"items": [{"id": "a"}], "next_cursor": "n"}),
        httpx.Response(200, json={"items": [{"id": "b"}], "next_cursor": None}),
    )
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    async with AsyncPairly(
        api_key=KEY,
        transport=httpx.MockTransport(script),
        retry=RetryPolicy(jitter=lambda: 1.0),
        sleep=sleep,
    ) as client:
        mine = await client.approvals.list_mine()
    assert [a["id"] for a in mine] == ["a", "b"]
    assert slept == [0.5]
