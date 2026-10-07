# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Webhooks on real Postgres: endpoint management over HTTP (SSRF checks, secrets shown
once, RBAC), the transactional outbox, signed delivery, retries, auto-disable, the
due-organisation scan and inbound deduplication.

DNS is a stub (``app.state.webhook_resolver``) and the sender is fake: nothing leaves the
test process.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from pairly_api.main import create_app
from pairly_api.uploads import DocumentServices
from pairly_core.config import Environment
from pairly_core.db import set_tenant
from pairly_core.ingestion.queue import RecordingJobQueue
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_core.webhooks.delivery import (
    DISABLED_ACTION,
    OutgoingWebhook,
    SendResult,
    WebhookDispatcher,
    due_org_ids,
)
from pairly_core.webhooks.events import DOCUMENT_RECEIVED, publish_event
from pairly_core.webhooks.inbound import process_inbound
from pairly_core.webhooks.settings import load_webhook_settings
from pairly_core.webhooks.signature import SIGNATURE_HEADER, verify_webhook_signature
from pairly_pay.plugin import plugin as pay_plugin

from .conftest import DbUrls, TokenFactory, make_settings
from .policy_support import Org
from .test_api_keys import OrgFactory, add_member_role, owner_rows, pdf_bytes

OTP = {"amr": ["otp"]}
PUBLIC_IP = "93.184.215.14"
HOOK_URL = "https://hooks.example.com/pairly?token=abc"


@dataclasses.dataclass
class Api:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory
    dns: dict[str, list[str]]

    def h(self, subject: str, org: uuid.UUID, *, otp: bool = True) -> dict[str, str]:
        claims = OTP if otp else {}
        return {
            "Authorization": f"Bearer {self.tokens.token(subject, **claims)}",
            "X-Pairly-Org": str(org),
        }

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        sm: async_sessionmaker[AsyncSession] = self.app.state.pairly.sessionmaker
        return sm

    async def create_endpoint(self, org: Org, **body: Any) -> httpx.Response:
        payload = {"url": HOOK_URL, "event_types": [DOCUMENT_RECEIVED]} | body
        return await self.client.post(
            "/v1/webhooks/endpoints", json=payload, headers=self.h(org.tenant.subject, org.id)
        )


@pytest.fixture
async def api(migrated: DbUrls, tokens: TokenFactory, tmp_path: Path) -> AsyncIterator[Api]:
    settings = IngestionSettings(
        env=Environment.TEST,
        s3_bucket="test",
        s3_access_key=SecretStr("test"),
        s3_secret_key=SecretStr("test-secret"),
        inbound_email_domain="inbound.pairly.localhost",
    )
    services = DocumentServices(
        settings=settings, store=InMemoryObjectStore(), queue=RecordingJobQueue(), tmp_dir=tmp_path
    )
    app = create_app(make_settings(migrated.app), plugins=[pay_plugin], documents=services)
    dns: dict[str, list[str]] = {"hooks.example.com": [PUBLIC_IP]}

    async def resolver(host: str, port: int) -> list[str]:
        if host not in dns:
            raise OSError("unknown host")
        return dns[host]

    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        app.state.webhook_resolver = resolver
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield Api(app, client, tokens, dns)


@pytest.fixture
async def org(make_org: OrgFactory) -> Org:
    return await make_org("admin")


@dataclasses.dataclass
class FakeSender:
    status: int = 204
    sent: list[OutgoingWebhook] = dataclasses.field(default_factory=list)

    async def send(self, webhook: OutgoingWebhook) -> SendResult:
        self.sent.append(webhook)
        return SendResult(self.status, 3)


def dispatcher(api: Api, sender: FakeSender, **settings: Any) -> WebhookDispatcher:
    return WebhookDispatcher(
        api.sessionmaker,
        sender,
        load_webhook_settings(env=Environment.TEST, **settings),
        rng=lambda: 0.5,
    )


async def upload(api: Api, org: Org, label: str) -> str:
    response = await api.client.post(
        "/v1/documents?workflow=pay",
        files={"file": ("invoice.pdf", pdf_bytes(label), "application/pdf")},
        headers=api.h(org.tenant.subject, org.id),
    )
    assert response.status_code == 201, response.text
    document_id: str = response.json()["document_id"]
    return document_id


# ------------------------------------------------------------------ management


async def test_endpoint_management(api: Api, org: Org, owner_engine: AsyncEngine) -> None:
    admin = org.tenant.subject
    types = await api.client.get("/v1/webhooks/event-types", headers=api.h(admin, org.id))
    assert DOCUMENT_RECEIVED in [e["key"] for e in types.json()]

    created = await api.create_endpoint(org, description="ERP")
    assert created.status_code == 201, created.text
    body = created.json()
    secret = body["secret"]
    assert secret.startswith("whsec_")
    endpoint_id = body["id"]
    assert body["active"] is True

    listed = await api.client.get("/v1/webhooks/endpoints", headers=api.h(admin, org.id))
    assert listed.status_code == 200
    assert secret not in listed.text
    one = await api.client.get(
        f"/v1/webhooks/endpoints/{endpoint_id}", headers=api.h(admin, org.id)
    )
    assert "secret" not in one.json()
    # Stored encrypted; the audit log names only the host, never the path or query.
    ((stored,),) = await owner_rows(
        owner_engine, org.id, "SELECT secret FROM webhook_endpoints WHERE id = :i", i=endpoint_id
    )
    assert secret not in stored
    audit = await owner_rows(
        owner_engine,
        org.id,
        "SELECT action, payload::text FROM audit_events WHERE entity_type = 'webhook_endpoint'",
    )
    assert audit[0][0] == "core.webhook.endpoint_created"
    assert "hooks.example.com" in audit[0][1]
    assert "token=abc" not in audit[0][1]

    rotated = await api.client.post(
        f"/v1/webhooks/endpoints/{endpoint_id}/rotate-secret", headers=api.h(admin, org.id)
    )
    assert rotated.status_code == 200
    assert rotated.json()["secret"].startswith("whsec_")
    assert rotated.json()["secret"] != secret

    disabled = await api.client.patch(
        f"/v1/webhooks/endpoints/{endpoint_id}",
        json={"active": False},
        headers=api.h(admin, org.id),
    )
    assert disabled.status_code == 200
    assert disabled.json()["disabled_reason"] == "disabled_by_admin"
    enabled = await api.client.patch(
        f"/v1/webhooks/endpoints/{endpoint_id}",
        json={"active": True, "event_types": [DOCUMENT_RECEIVED, "approval.decided"]},
        headers=api.h(admin, org.id),
    )
    assert enabled.status_code == 200
    assert enabled.json()["active"] is True
    assert enabled.json()["event_types"] == ["approval.decided", DOCUMENT_RECEIVED]


async def test_endpoint_validation_and_rbac(
    api: Api, org: Org, owner_engine: AsyncEngine, make_org: OrgFactory
) -> None:
    api.dns["rebind.example.com"] = [PUBLIC_IP, "10.0.0.8"]
    api.dns["meta.example.com"] = ["169.254.169.254"]
    for url in (
        "http://hooks.example.com/",
        "https://127.0.0.1/",
        "https://localhost/",
        "https://rebind.example.com/",
        "https://meta.example.com/",
        "https://unknown.example.com/",
    ):
        refused = await api.create_endpoint(org, url=url)
        assert refused.status_code == 400, url
        assert refused.json()["code"] == "unsafe_webhook_url", url
    unknown = await api.create_endpoint(org, event_types=["nothing.here"])
    assert unknown.status_code == 400

    no_otp = await api.client.post(
        "/v1/webhooks/endpoints",
        json={"url": HOOK_URL, "event_types": [DOCUMENT_RECEIVED]},
        headers=api.h(org.tenant.subject, org.id, otp=False),
    )
    assert no_otp.status_code == 403
    auditor = await add_member_role(owner_engine, org.id, "auditor")
    read = await api.client.get("/v1/webhooks/endpoints", headers=api.h(auditor, org.id))
    assert read.status_code == 200
    write = await api.client.post(
        "/v1/webhooks/endpoints",
        json={"url": HOOK_URL, "event_types": [DOCUMENT_RECEIVED]},
        headers=api.h(auditor, org.id),
    )
    assert write.status_code == 403
    # Another organisation never sees this one's endpoints.
    created = await api.create_endpoint(org)
    other = await make_org("admin")
    hidden = await api.client.get(
        f"/v1/webhooks/endpoints/{created.json()['id']}",
        headers=api.h(other.tenant.subject, other.id),
    )
    assert hidden.status_code == 404


# ------------------------------------------------------------------ delivery


async def test_outbox_fan_out_and_signed_delivery(
    api: Api, org: Org, owner_engine: AsyncEngine
) -> None:
    created = (await api.create_endpoint(org)).json()
    # Subscribed to another type only: gets nothing.
    await api.create_endpoint(
        org, url="https://hooks.example.com/other", event_types=["approval.decided"]
    )
    document_id = await upload(api, org, "webhook")

    assert org.id in await due_org_ids(api.sessionmaker)
    sender = FakeSender()
    stats = await dispatcher(api, sender).run_org(org.id)
    assert (stats.fanned_out, stats.sent, stats.succeeded) == (1, 1, 1)
    (sent,) = sender.sent
    assert sent.url == HOOK_URL
    payload = json.loads(sent.body)
    assert payload["type"] == DOCUMENT_RECEIVED
    assert payload["org_id"] == str(org.id)
    assert payload["data"]["document_id"] == document_id
    assert sent.headers["Pairly-Event-Id"] == payload["id"]
    assert sent.headers["Pairly-Delivery-Attempt"] == "1"
    verify_webhook_signature(sent.body, sent.headers[SIGNATURE_HEADER], created["secret"])

    # Nothing is sent twice: the outbox row is dispatched, the delivery succeeded.
    again = await dispatcher(api, sender).run_org(org.id)
    assert (again.fanned_out, again.sent) == (0, 0)
    assert org.id not in await due_org_ids(api.sessionmaker)

    deliveries = await api.client.get(
        f"/v1/webhooks/endpoints/{created['id']}/deliveries",
        headers=api.h(org.tenant.subject, org.id),
    )
    (row,) = deliveries.json()
    assert (row["status"], row["attempts"], row["response_code"]) == ("succeeded", 1, 204)
    assert row["event_id"] == payload["id"]


async def test_rolled_back_change_emits_nothing(
    api: Api, org: Org, owner_engine: AsyncEngine
) -> None:
    async with api.sessionmaker() as session:
        set_tenant(session, org.id, None)
        async with session.begin():
            await publish_event(session, DOCUMENT_RECEIVED, {"document_id": "x"})
            await session.rollback()
    ((count,),) = await owner_rows(
        owner_engine, org.id, "SELECT count(*) FROM webhook_outbox WHERE org_id = :o", o=org.id
    )
    assert count == 0


async def test_retries_then_auto_disable(api: Api, org: Org, owner_engine: AsyncEngine) -> None:
    created = (await api.create_endpoint(org)).json()
    await upload(api, org, "failing one")
    await upload(api, org, "failing two")
    failing = FakeSender(status=500)
    first = await dispatcher(api, failing, webhook_max_consecutive_failures=3).run_org(org.id)
    assert (first.sent, first.failed_attempts, first.disabled_endpoints) == (2, 2, [])
    rows = await owner_rows(
        owner_engine,
        org.id,
        "SELECT status, attempts, last_error, next_attempt_at > now() + interval '50 seconds' "
        "FROM webhook_deliveries WHERE endpoint_id = :e",
        e=created["id"],
    )
    assert sorted(tuple(r) for r in rows) == [("pending", 1, "http_500", True)] * 2
    # Not due yet: a second run sends nothing.
    assert (await dispatcher(api, failing).run_org(org.id)).sent == 0

    await owner_rows(
        owner_engine,
        org.id,
        "UPDATE webhook_deliveries SET next_attempt_at = now() WHERE endpoint_id = :e RETURNING id",
        e=created["id"],
    )
    second = await dispatcher(api, failing, webhook_max_consecutive_failures=3).run_org(org.id)
    assert second.disabled_endpoints == [created["id"]]
    ((active, reason, failures),) = await owner_rows(
        owner_engine,
        org.id,
        "SELECT active, disabled_reason, consecutive_failures FROM webhook_endpoints WHERE id = :e",
        e=created["id"],
    )
    assert (active, reason, failures) == (False, "consecutive_failures", 3)
    statuses = sorted(
        r[0]
        for r in await owner_rows(
            owner_engine,
            org.id,
            "SELECT status FROM webhook_deliveries WHERE endpoint_id = :e",
            e=created["id"],
        )
    )
    # The disable cancels every pending delivery of the endpoint, both of these included.
    assert statuses == ["cancelled", "cancelled"]
    audit = await owner_rows(
        owner_engine,
        org.id,
        "SELECT actor_type FROM audit_events WHERE action = :a AND entity_id = :e",
        a=DISABLED_ACTION,
        e=created["id"],
    )
    assert [r[0] for r in audit] == ["system"]

    # Re-enabling starts fresh.
    enabled = await api.client.patch(
        f"/v1/webhooks/endpoints/{created['id']}",
        json={"active": True},
        headers=api.h(org.tenant.subject, org.id),
    )
    assert enabled.json()["consecutive_failures"] == 0


async def test_old_deliveries_give_up(api: Api, org: Org, owner_engine: AsyncEngine) -> None:
    created = (await api.create_endpoint(org)).json()
    await upload(api, org, "too old")
    sender = FakeSender(status=503)
    await dispatcher(api, sender).fan_out(org.id)
    await owner_rows(
        owner_engine,
        org.id,
        "UPDATE webhook_deliveries SET created_at = now() - interval '23 hours 59 minutes' "
        "WHERE endpoint_id = :e RETURNING id",
        e=created["id"],
    )
    stats = await dispatcher(api, sender).deliver_due(org.id)
    assert stats.gave_up == 1
    ((status,),) = await owner_rows(
        owner_engine,
        org.id,
        "SELECT status FROM webhook_deliveries WHERE endpoint_id = :e",
        e=created["id"],
    )
    assert status == "failed"


# ------------------------------------------------------------------ inbound


async def test_inbound_events_are_processed_once(api: Api, org: Org) -> None:
    processed: list[str] = []

    async def handler(session: AsyncSession) -> None:
        processed.append("x")

    async def failing(session: AsyncSession) -> None:
        raise RuntimeError("downstream failed")

    sm = api.sessionmaker
    # A failure rolls the record back, so the provider's retry is processed.
    with pytest.raises(RuntimeError):
        await process_inbound(
            sm, org_id=org.id, provider="stripe", event_id="evt_1", handler=failing
        )
    first = await process_inbound(
        sm, org_id=org.id, provider="stripe", event_id="evt_1", handler=handler
    )
    second = await process_inbound(
        sm, org_id=org.id, provider="stripe", event_id="evt_1", handler=handler
    )
    assert (first.duplicate, second.duplicate, processed) == (False, True, ["x"])
    # The same event id from another provider or organisation is a different event.
    other = await process_inbound(
        sm, org_id=org.id, provider="slack", event_id="evt_1", handler=handler
    )
    assert not other.duplicate
