# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pay's Resend inbound webhook without a database: the event parser, the handler's
order of work (route, fetch, record, intake) and the route's answers."""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pairly_pay.api.inbound as inbound_route
import pairly_pay.resend_inbound as handler_mod
from pairly_api.problems import install_error_handlers
from pairly_api.uploads import DocumentServices
from pairly_core.identity import get_context
from pairly_core.ingestion.queue import NullJobQueue
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_core.integrations.resend.receiving import ReceivingError
from pairly_core.integrations.resend.signature import sign
from pairly_core.notifications.settings import load_notification_settings
from pairly_pay.intake import EmailIntake
from pairly_pay.resend_inbound import ReceivedEvent, handle_received, parse_event

SECRET = "whsec_" + base64.b64encode(b"pairly-test-only-signing-key-0003").decode()
EMAIL_ID = "56761188-7520-42d8-8898-ff6fc54ce618"
ORG = uuid.uuid4()


def _ingestion() -> IngestionSettings:
    values: dict[str, Any] = {
        "env": "test",
        "s3_bucket": "test-bucket",
        "s3_access_key": "test-access",
        "s3_secret_key": "test-secret-value",
    }
    return IngestionSettings(**values)


def _event_body(event_type: str = "email.received", **data: Any) -> bytes:
    inner = {"email_id": EMAIL_ID, "to": ["pay+acme.tok@in.example.test"], **data}
    return json.dumps({"type": event_type, "data": inner}).encode()


def test_parse_event() -> None:
    event = parse_event(
        "msg_1",
        _event_body(
            to=["pay+acme.tok@in.example.test", "PAY+acme.tok@in.example.test"],
            received_for=["fwd@in.example.test", 7],
        ),
    )
    assert event == ReceivedEvent(
        "msg_1", EMAIL_ID, ("pay+acme.tok@in.example.test", "fwd@in.example.test")
    )
    assert parse_event("m", _event_body("email.delivered")) is None
    assert parse_event("m", _event_body(email_id="../x")) is None
    assert parse_event("m", b"not json") is None


class _FakeSession:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}

    def in_transaction(self) -> bool:
        return False

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        yield

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _deps(monkeypatch: pytest.MonkeyPatch, *, org: uuid.UUID | None, first: bool) -> list[str]:
    calls: list[str] = []

    async def route_org(sessionmaker: Any, recipients: Any, settings: Any) -> uuid.UUID | None:
        calls.append("route")
        return org

    async def record(session: Any, *, provider: str, event_id: str) -> bool:
        assert provider == "resend"
        calls.append(f"record:{event_id}")
        return first

    async def receive(raw: bytes, **kw: Any) -> EmailIntake:
        calls.append(f"intake:{raw.decode()}:{','.join(kw['envelope_recipients'])}")
        return EmailIntake(status="ingested", org_id=org)

    monkeypatch.setattr(handler_mod, "route_org", route_org)
    monkeypatch.setattr(handler_mod, "record_inbound_event", record)
    monkeypatch.setattr(handler_mod, "receive_email", receive)
    return calls


async def _handle(calls: list[str]) -> str:
    async def fetch(email_id: str, limit: int) -> bytes:
        calls.append(f"fetch:{email_id}")
        return b"RAW"

    return await handle_received(
        ReceivedEvent("msg_1", EMAIL_ID, ("pay+acme.tok@in.example.test",)),
        sessionmaker=_FakeSession,  # type: ignore[arg-type]
        store=InMemoryObjectStore(),
        ingestion=_ingestion(),
        notifications=SimpleNamespace(),  # type: ignore[arg-type]
        fetch_raw=fetch,
    )


async def test_unroutable_fetches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _deps(monkeypatch, org=None, first=True)
    assert await _handle(calls) == "unroutable"
    assert calls == ["route"]


async def test_first_delivery_is_ingested(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _deps(monkeypatch, org=ORG, first=True)
    assert await _handle(calls) == "ingested"
    assert calls == [
        "route",
        f"fetch:{EMAIL_ID}",
        "record:msg_1",
        "intake:RAW:pay+acme.tok@in.example.test",
    ]


async def test_duplicate_event_is_not_ingested_again(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _deps(monkeypatch, org=ORG, first=False)
    assert await _handle(calls) == "duplicate"
    assert not any(c.startswith("intake") for c in calls)


# ------------------------------------------------------------------ route


def _app(configured: bool = True) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(inbound_route.router)
    extra: dict[str, Any] = (
        {"resend_webhook_secret": SECRET, "resend_inbound_api_key": "re_placeholder_full"}
        if configured
        else {}
    )
    app.state.notifications = load_notification_settings(env="test", **extra)
    settings = _ingestion()
    app.state.documents = DocumentServices(settings, InMemoryObjectStore(), NullJobQueue())
    app.dependency_overrides[get_context] = lambda: SimpleNamespace(sessionmaker=object())
    return app


def _post(client: TestClient, body: bytes, *, secret: str = SECRET, ts: int | None = None) -> Any:
    stamp = str(ts if ts is not None else int(time.time()))
    return client.post(
        "/inbound/resend",
        content=body,
        headers={
            "svix-id": "msg_1",
            "svix-timestamp": stamp,
            "svix-signature": sign(secret, "msg_1", stamp, body),
            "content-type": "application/json",
        },
    )


def test_route_unconfigured_is_503() -> None:
    with TestClient(_app(configured=False)) as client:
        assert _post(client, _event_body()).status_code == 503


def test_route_refuses_bad_signatures(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(*a: Any, **kw: Any) -> str:
        raise AssertionError("must not run for an unsigned request")

    monkeypatch.setattr(inbound_route, "handle_received", never)
    other = "whsec_" + base64.b64encode(b"pairly-test-only-signing-key-0004").decode()
    with TestClient(_app()) as client:
        assert _post(client, _event_body(), secret=other).status_code == 401
        assert _post(client, _event_body(), ts=int(time.time()) - 3600).status_code == 401
        assert client.post("/inbound/resend", content=_event_body()).status_code == 401


def test_route_ignores_other_events(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(_app()) as client:
        response = _post(client, _event_body("email.bounced"))
    assert response.status_code == 200
    assert response.json()["outcome"] == "ignored"


@pytest.mark.parametrize(
    ("error", "status", "outcome"),
    [
        (None, 200, "ingested"),
        (ReceivingError("resend_503", transient=True), 503, None),
        (ReceivingError("too_large", transient=False), 200, "dropped"),
    ],
)
def test_route_outcomes(
    monkeypatch: pytest.MonkeyPatch, error: ReceivingError | None, status: int, outcome: str | None
) -> None:
    seen: list[ReceivedEvent] = []

    async def fake(event: ReceivedEvent, **kw: Any) -> str:
        seen.append(event)
        if error is not None:
            raise error
        return "ingested"

    monkeypatch.setattr(inbound_route, "handle_received", fake)
    with TestClient(_app()) as client:
        response = _post(client, _event_body())
    assert response.status_code == status
    if outcome:
        assert response.json()["outcome"] == outcome
    assert seen
    assert seen[0].msg_id == "msg_1"
