# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The batch API on real Postgres: files and URLs, outcomes, ``batch.completed``,
SSRF refusals, idempotency, the per-caller documents-per-minute limit, permissions and
tenant isolation.

URLs are fetched through a ``UrlFetcher`` whose resolver and transport are fakes (no
network): the SSRF checks run for real on the addresses the fake resolver returns.
"""

from __future__ import annotations

import dataclasses
import io
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_api.main import create_app
from pairly_api.uploads import DocumentServices
from pairly_core.config import Environment
from pairly_core.ingestion.fetch import UrlFetcher
from pairly_core.ingestion.queue import RecordingJobQueue
from pairly_core.ingestion.settings import IngestionSettings, IntakePolicy
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_pay.plugin import plugin as pay_plugin

from .conftest import DbUrls, TokenFactory, make_settings
from .policy_support import Org, new_org

OTP = {"amr": ["otp"]}
PUBLIC_IP = "93.184.216.34"


def pdf_bytes(label: str) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.drawString(72, 720, f"Synthetic invoice {label}")
    pdf.save()
    return buf.getvalue()


SERVED = {
    "/inv-a.pdf": pdf_bytes("served-a"),
    "/inv-big.pdf": pdf_bytes("big") + b"0" * 9000,
}


def remote(request: httpx.Request) -> httpx.Response:
    """The fake internet: what the fetcher reaches after the SSRF check."""
    assert request.url.host == PUBLIC_IP  # the checked address, never the name
    assert request.headers["host"] == "files.example.com"
    if request.url.path == "/moved.pdf":
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})
    if request.url.path in SERVED:
        return httpx.Response(200, content=SERVED[request.url.path])
    return httpx.Response(404)


async def resolver(host: str, port: int) -> list[str]:
    return {"files.example.com": [PUBLIC_IP], "evil.example.com": ["10.0.0.5"]}.get(host, [])


@dataclasses.dataclass
class Api:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory
    queue: RecordingJobQueue

    def h(self, org: Org) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tokens.token(org.tenant.subject, **OTP)}",
            "X-Pairly-Org": str(org.id),
        }

    async def key(self, org: Org, scopes: list[str]) -> str:
        created = await self.client.post(
            "/v1/api-keys", json={"name": "batch", "scopes": scopes}, headers=self.h(org)
        )
        assert created.status_code == 201, created.text
        return str(created.json()["key"])

    async def files(
        self, key: str, files: list[tuple[str, bytes, str]], **data: str
    ) -> httpx.Response:
        return await self.client.post(
            "/v1/documents/batch?workflow=pay",
            files=[("file", f) for f in files],
            data=data,
            headers={"Authorization": f"Bearer {key}"},
        )


@pytest.fixture
async def api(migrated: DbUrls, tokens: TokenFactory, tmp_path: Path) -> AsyncIterator[Api]:
    settings = IngestionSettings(
        env=Environment.TEST,
        s3_bucket="test",
        s3_access_key=SecretStr("test"),
        s3_secret_key=SecretStr("test-secret"),
        inbound_email_domain="inbound.pairly.localhost",
        intake_policies={
            "pay": IntakePolicy(allowed_types=frozenset({"application/pdf"}), max_bytes=8000)
        },
    )
    queue = RecordingJobQueue()
    services = DocumentServices(
        settings=settings, store=InMemoryObjectStore(), queue=queue, tmp_dir=tmp_path
    )
    app_settings = make_settings(migrated.app).model_copy(update={"batch_items_per_minute": 12})
    app = create_app(app_settings, plugins=[pay_plugin], documents=services)
    app.state.url_fetcher_factory = lambda: UrlFetcher(
        resolver=resolver, transport=httpx.MockTransport(remote)
    )
    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield Api(app, client, tokens, queue)


OrgFactory = Callable[[str], Awaitable[Org]]


@pytest.fixture
async def make_org(owner_engine: AsyncEngine) -> AsyncIterator[OrgFactory]:
    """Organisations that keep their documents (invoices reference them; the database is
    throwaway and organisations are unique). Their documents are settled afterwards, so
    the stalled-document sweeper in other tests, which scans every organisation, never
    picks them up."""
    made: list[Org] = []

    async def make(role: str = "admin") -> Org:
        made.append(await new_org(owner_engine, role))
        return made[-1]

    yield make
    for org in made:
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.id)}
            )
            await conn.execute(
                text(
                    "UPDATE documents SET status = 'rejected', status_reason = 'test_teardown' "
                    "WHERE org_id = :o AND status IN ('received', 'scanning', 'clean')"
                ),
                {"o": org.id},
            )


SCOPES = ["pay.invoice.upload", "pay.invoice.read"]


async def _events(owner_engine: AsyncEngine, org: Org) -> list[dict[str, Any]]:
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.id)})
        rows = await conn.execute(
            text(
                "SELECT data FROM webhook_outbox WHERE org_id = :o "
                "AND event_type = 'batch.completed' ORDER BY created_at"
            ),
            {"o": org.id},
        )
        return [dict(r[0]) for r in rows]


async def test_a_file_batch_makes_invoices_and_completes(
    api: Api, make_org: OrgFactory, owner_engine: AsyncEngine
) -> None:
    org = await make_org("admin")
    key = await api.key(org, SCOPES)
    first, second = pdf_bytes(f"a-{uuid.uuid4()}"), pdf_bytes(f"b-{uuid.uuid4()}")
    sent = await api.files(
        key,
        [
            ("a.pdf", first, "application/pdf"),
            ("b.pdf", second, "application/pdf"),
            ("again.pdf", first, "application/pdf"),
            ("notes.txt", b"just some text, not a PDF", "application/pdf"),
            ("big.pdf", first + b"0" * 9000, "application/pdf"),
        ],
        metadata=json.dumps({"source": "erp-export", "run": 7}),
    )
    assert sent.status_code == 202, sent.text
    body = sent.json()
    assert body["status"] == "completed"
    assert body["metadata"] == {"source": "erp-export", "run": 7}
    statuses = [(i["status"], i["reason"]) for i in body["items"]]
    assert statuses == [
        ("accepted", None),
        ("accepted", None),
        ("duplicate", None),
        ("rejected", "type_not_allowed"),
        ("rejected", "too_large"),
    ]
    items = body["items"]
    assert {i["entity_type"] for i in items[:3]} == {"pay.invoice"}
    assert items[2]["entity_id"] == items[0]["entity_id"]
    assert items[2]["document_id"] == items[0]["document_id"]
    assert body["counts"] == {
        "pending": 0,
        "accepted": 2,
        "duplicate": 1,
        "rejected": 2,
        "failed": 0,
    }
    # The two new documents are queued for their virus scan.
    scanned = {str(doc) for _, o, doc, _ in api.queue.jobs if o == org.id}
    assert scanned == {items[0]["document_id"], items[1]["document_id"]}
    # The invoices exist, from the API source.
    invoice = await api.client.get(
        f"/v1/pay/invoices/{items[0]['entity_id']}", headers={"Authorization": f"Bearer {key}"}
    )
    assert invoice.status_code == 200
    assert invoice.json()["source"] == "api"
    events = await _events(owner_engine, org)
    assert len(events) == 1
    assert events[0]["batch_id"] == body["id"]
    assert events[0]["counts"]["accepted"] == 2
    assert events[0]["metadata"]["source"] == "erp-export"


async def test_urls_are_fetched_safely_and_the_batch_completes(
    api: Api, make_org: OrgFactory, owner_engine: AsyncEngine
) -> None:
    org = await make_org("admin")
    key = await api.key(org, SCOPES)
    sent = await api.client.post(
        "/v1/documents/batch?workflow=pay",
        json={
            "urls": [
                "https://files.example.com/inv-a.pdf",
                "https://files.example.com/moved.pdf",
                "https://evil.example.com/inv.pdf",
                "https://files.example.com/missing.pdf",
                "https://files.example.com/inv-big.pdf",
                "https://nowhere.example.com/inv.pdf",
            ],
            "metadata": {"ref": "urls"},
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert sent.status_code == 202, sent.text
    batch_id = sent.json()["id"]
    shown = await api.client.get(
        f"/v1/batches/{batch_id}", headers={"Authorization": f"Bearer {key}"}
    )
    body = shown.json()
    assert body["status"] == "completed"
    outcomes = [(i["status"], i["reason"]) for i in body["items"]]
    assert outcomes == [
        ("accepted", None),
        ("failed", "redirect_not_followed"),
        ("failed", "unsafe_url"),
        ("failed", "http_404"),
        ("rejected", "too_large"),
        ("failed", "unsafe_url"),
    ]
    assert body["items"][0]["entity_type"] == "pay.invoice"
    events = await _events(owner_engine, org)
    assert [e["batch_id"] for e in events] == [batch_id]


@pytest.mark.parametrize(
    "url",
    [
        "http://files.example.com/inv.pdf",
        "https://localhost/inv.pdf",
        "https://user:pw@files.example.com/inv.pdf",
        "https://files.internal/inv.pdf",
        "file:///etc/passwd",
        "https://files.example.com/inv.pdf#frag",
    ],
)
async def test_unsafe_url_shapes_refuse_the_whole_batch(
    api: Api, make_org: OrgFactory, url: str
) -> None:
    org = await make_org("admin")
    key = await api.key(org, SCOPES)
    sent = await api.client.post(
        "/v1/documents/batch?workflow=pay",
        json={"urls": ["https://files.example.com/inv-a.pdf", url]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert sent.status_code == 400, sent.text
    assert "urls[1]" in sent.json()["detail"]


async def test_a_retry_with_the_same_idempotency_key_replays(
    api: Api, make_org: OrgFactory, owner_engine: AsyncEngine
) -> None:
    org = await make_org("admin")
    key = await api.key(org, SCOPES)
    data = pdf_bytes(f"idem-{uuid.uuid4()}")
    headers = {"Authorization": f"Bearer {key}", "Idempotency-Key": f"batch-{uuid.uuid4().hex}"}
    calls = []
    for _ in range(2):
        calls.append(
            await api.client.post(
                "/v1/documents/batch?workflow=pay",
                files=[("file", ("a.pdf", data, "application/pdf"))],
                headers=headers,
            )
        )
    assert [c.status_code for c in calls] == [202, 202]
    assert calls[0].json()["id"] == calls[1].json()["id"]
    assert calls[1].headers.get("idempotent-replayed") == "true"
    assert len(await _events(owner_engine, org)) == 1


async def test_documents_per_minute_are_limited_per_caller(api: Api, make_org: OrgFactory) -> None:
    org = await make_org("admin")
    key = await api.key(org, SCOPES)
    urls = [f"https://files.example.com/missing-{n}.pdf" for n in range(10)]
    first = await api.client.post(
        "/v1/documents/batch?workflow=pay",
        json={"urls": urls},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert first.status_code == 202
    second = await api.client.post(
        "/v1/documents/batch?workflow=pay",
        json={"urls": urls[:5]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert second.status_code == 429
    assert int(second.headers["retry-after"]) >= 1
    # Another key has its own budget.
    other = await api.key(org, SCOPES)
    third = await api.client.post(
        "/v1/documents/batch?workflow=pay",
        json={"urls": urls[:5]},
        headers={"Authorization": f"Bearer {other}"},
    )
    assert third.status_code == 202


async def test_batches_need_the_workflow_intake_permission(api: Api, make_org: OrgFactory) -> None:
    org = await make_org("admin")
    key = await api.key(org, ["core.document.upload", "pay.invoice.read"])
    sent = await api.files(key, [("a.pdf", pdf_bytes("perm"), "application/pdf")])
    assert sent.status_code == 403
    too_many = await api.client.post(
        "/v1/documents/batch?workflow=pay",
        json={"urls": [f"https://files.example.com/{n}.pdf" for n in range(51)]},
        headers={"Authorization": f"Bearer {await api.key(org, SCOPES)}"},
    )
    assert too_many.status_code == 400
    empty = await api.client.post(
        "/v1/documents/batch?workflow=pay",
        json={"urls": []},
        headers={"Authorization": f"Bearer {await api.key(org, SCOPES)}"},
    )
    assert empty.status_code == 400
    unknown = await api.client.post(
        "/v1/documents/batch?workflow=nope",
        json={"urls": ["https://files.example.com/inv-a.pdf"]},
        headers={"Authorization": f"Bearer {await api.key(org, SCOPES)}"},
    )
    assert unknown.status_code == 400


async def test_another_organisation_cannot_read_a_batch(api: Api, make_org: OrgFactory) -> None:
    org, other = await make_org("admin"), await make_org("admin")
    key = await api.key(org, SCOPES)
    sent = await api.files(key, [("a.pdf", pdf_bytes(f"t-{uuid.uuid4()}"), "application/pdf")])
    batch_id = sent.json()["id"]
    foreign = await api.client.get(
        f"/v1/batches/{batch_id}",
        headers={"Authorization": f"Bearer {await api.key(other, SCOPES)}"},
    )
    assert foreign.status_code == 404
    # The person with core.document.read in the same org may read it.
    own_org = await api.client.get(f"/v1/batches/{batch_id}", headers=api.h(org))
    assert own_org.status_code == 200


def test_a_plugin_intake_must_use_its_own_permission() -> None:
    from pairly_core.batches.intake import (
        DocumentIntake,
        IntakeRegistryError,
        IntakeResult,
        build_intake_registry,
    )

    async def handle(*_: Any) -> IntakeResult:  # pragma: no cover - never called
        raise AssertionError

    @dataclasses.dataclass
    class Plugin:
        id: str
        document_intake: Any

    assert build_intake_registry([pay_plugin])["pay"].permission == "pay.invoice.upload"
    with pytest.raises(IntakeRegistryError, match="its own"):
        build_intake_registry([Plugin("other", DocumentIntake("pay.invoice.upload", handle))])
    with pytest.raises(IntakeRegistryError, match="DocumentIntake"):
        build_intake_registry([Plugin("other", object())])


# ------------------------------------------------------------------ the URL sweeper


async def _stale_batch(
    owner_engine: AsyncEngine, org: Org, *, minutes_ago: int, urls: int
) -> uuid.UUID:
    """A batch whose URL items stayed pending (the API process that fetched them died),
    with one file item that already has its outcome."""
    batch_id = uuid.uuid4()
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.id)})
        await conn.execute(
            text(
                "INSERT INTO document_batches (id, org_id, workflow, status, item_count, "
                "created_by_type, created_by_id, created_at) VALUES (:b, :o, 'pay', "
                "'processing', :n, 'api_key', 'key-test', now() - make_interval(mins => :m))"
            ),
            {"b": batch_id, "o": org.id, "n": urls + 1, "m": minutes_ago},
        )
        await conn.execute(
            text(
                "INSERT INTO document_batch_items (org_id, batch_id, position, source, "
                "filename, status, reason, completed_at) VALUES (:o, :b, 0, 'file', "
                "'a.pdf', 'rejected', 'empty', now())"
            ),
            {"o": org.id, "b": batch_id},
        )
        for position in range(1, urls + 1):
            await conn.execute(
                text(
                    "INSERT INTO document_batch_items (org_id, batch_id, position, source, "
                    "url, status) VALUES (:o, :b, :p, 'url', :u, 'pending')"
                ),
                {"o": org.id, "b": batch_id, "p": position, "u": f"https://x.test/{position}"},
            )
    return batch_id


async def test_the_sweeper_fails_url_items_left_pending_and_completes_the_batch(
    api: Api, make_org: OrgFactory, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    from pairly_core.db import create_sessionmaker
    from pairly_worker.batch_jobs import sweep_batch_urls

    org, other = await make_org("admin"), await make_org("admin")
    stale = await _stale_batch(owner_engine, org, minutes_ago=20, urls=2)
    fresh = await _stale_batch(owner_engine, org, minutes_ago=1, urls=1)
    foreign = await _stale_batch(owner_engine, other, minutes_ago=30, urls=1)

    ctx = {"sessionmaker": create_sessionmaker(app_engine)}
    summary = await sweep_batch_urls(ctx)
    assert summary["errors"] == []
    assert summary["items"] >= 3
    assert summary["completed"] >= 2

    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.id)})
        items = (
            await conn.execute(
                text(
                    "SELECT batch_id, position, status, reason FROM document_batch_items "
                    "WHERE org_id = :o ORDER BY batch_id, position"
                ),
                {"o": org.id},
            )
        ).all()
        rows = await conn.execute(
            text("SELECT id, status FROM document_batches WHERE org_id = :o"), {"o": org.id}
        )
        batches: dict[uuid.UUID, str] = {r.id: r.status for r in rows}
        audit = (
            await conn.execute(
                text(
                    "SELECT entity_id, actor_type, actor_id, payload FROM audit_events "
                    "WHERE org_id = :o AND action = 'core.batch.items_timed_out'"
                ),
                {"o": org.id},
            )
        ).all()
    by_batch = {(r.batch_id, r.position): (r.status, r.reason) for r in items}
    assert by_batch[(stale, 0)] == ("rejected", "empty")  # an outcome is never changed
    assert by_batch[(stale, 1)] == by_batch[(stale, 2)] == ("failed", "timed_out")
    assert by_batch[(fresh, 1)] == ("pending", None)  # still within the timeout
    assert batches == {stale: "completed", fresh: "processing"}
    assert [(a.entity_id, a.actor_type, a.actor_id) for a in audit] == [
        (str(stale), "system", "pairly-worker.batch_url_sweep")
    ]
    assert audit[0].payload["items"] == 2
    assert audit[0].payload["reason"] == "timed_out"
    # The client hears about it: batch.completed with the timed_out outcomes.
    events = await _events(owner_engine, org)
    assert [e["batch_id"] for e in events] == [str(stale)]
    assert events[0]["counts"]["failed"] == 2
    # The other organisation's batch was handled in its own tenant, its own chain.
    others = await _events(owner_engine, other)
    assert [e["batch_id"] for e in others] == [str(foreign)]
    # A second run finds nothing more of these.
    again = await sweep_batch_urls(ctx)
    assert again["errors"] == []
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.id)})
        count = await conn.scalar(
            text(
                "SELECT count(*) FROM audit_events WHERE org_id = :o "
                "AND action = 'core.batch.items_timed_out'"
            ),
            {"o": org.id},
        )
    assert count == 1


async def test_the_app_role_cannot_list_stale_batches_of_other_tenants_directly(
    app_engine: AsyncEngine, make_org: OrgFactory, owner_engine: AsyncEngine
) -> None:
    org = await make_org("admin")
    await _stale_batch(owner_engine, org, minutes_ago=20, urls=1)
    async with app_engine.begin() as conn:
        # Without a tenant, RLS shows nothing; only the SECURITY DEFINER listing (ids) does.
        assert await conn.scalar(text("SELECT count(*) FROM document_batch_items")) == 0
        ids = (await conn.execute(text("SELECT batch_stale_org_ids(15)"))).scalars().all()
        assert org.id in ids
        with pytest.raises(Exception, match="minutes must be at least 1"):
            await conn.execute(text("SELECT batch_stale_org_ids(0)"))
