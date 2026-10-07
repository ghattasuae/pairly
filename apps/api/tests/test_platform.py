# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""WP1.12 on real Postgres: feature flags (API, audit, isolation, the webhook switch),
job runs (recorded through SECURITY DEFINER functions only), the retention dry run
(counts, never deletes) and unified search (kinds, permissions, the document flag, PII
redaction)."""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from pairly_api.main import create_app
from pairly_api.uploads import DocumentServices
from pairly_core.config import Environment
from pairly_core.db import set_tenant
from pairly_core.flags.registry import SEARCH_DOCUMENTS, WEBHOOKS_OUTBOUND
from pairly_core.flags.service import is_enabled
from pairly_core.ingestion.models import Document, DocumentStatus
from pairly_core.ingestion.queue import RecordingJobQueue
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import InMemoryObjectStore, blob_key, derived_key
from pairly_core.jobs.runs import finish_run, recent_runs, start_run, tracked
from pairly_core.plugins import WorkflowPlugin
from pairly_core.rbac import PermissionSpec
from pairly_core.retention.rules import RetentionRegistry
from pairly_core.retention.service import run_dry_run
from pairly_core.retrieval.embeddings import HashingEmbedder
from pairly_core.search.documents import index_document_after_parse, remove_document
from pairly_core.webhooks.delivery import OutgoingWebhook, SendResult, WebhookDispatcher
from pairly_core.webhooks.events import DOCUMENT_RECEIVED, publish_event
from pairly_core.webhooks.settings import load_webhook_settings
from pairly_core.workflow.spec import GraphSpec
from pairly_pay.plugin import plugin as pay_plugin

from .conftest import DbUrls, TokenFactory, make_settings
from .policy_support import SPEND_POLICY, Org, add_policy, build_document
from .test_api_keys import OrgFactory, add_member_role, owner_exec, owner_rows

OTP = {"amr": ["otp"]}


@dataclasses.dataclass(frozen=True)
class ToyPlugin:
    """A workflow whose documents only admins may read."""

    id: str = "toy"
    name: str = "Toy"
    permissions: tuple[PermissionSpec, ...] = (
        PermissionSpec("toy.document.read", "Read toy documents", frozenset({"admin"})),
        PermissionSpec("toy.document.upload", "Upload toy documents", frozenset({"admin"})),
    )
    graphs: tuple[GraphSpec, ...] = ()


PLUGINS: list[WorkflowPlugin] = [pay_plugin, ToyPlugin()]


@dataclasses.dataclass
class Api:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory
    store: InMemoryObjectStore

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


@pytest.fixture
async def api(migrated: DbUrls, tokens: TokenFactory, tmp_path: Path) -> AsyncIterator[Api]:
    settings = IngestionSettings(
        env=Environment.TEST,
        s3_bucket="test",
        s3_access_key=SecretStr("test"),
        s3_secret_key=SecretStr("test-secret"),
        inbound_email_domain="inbound.pairly.localhost",
    )
    store = InMemoryObjectStore()
    services = DocumentServices(
        settings=settings, store=store, queue=RecordingJobQueue(), tmp_dir=tmp_path
    )
    app = create_app(make_settings(migrated.app), plugins=PLUGINS, documents=services)
    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        # Offline vectors: search and indexing need no provider.
        app.state.policy_embedder = HashingEmbedder()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield Api(app, client, tokens, store)


@pytest.fixture
async def org(make_org: OrgFactory) -> Org:
    return await make_org("admin")


# ------------------------------------------------------------------ flags


async def test_flags_api(
    api: Api, org: Org, make_org: OrgFactory, owner_engine: AsyncEngine
) -> None:
    admin = org.tenant.subject
    listed = await api.client.get("/v1/flags", headers=api.h(admin, org.id))
    assert listed.status_code == 200
    flags = {f["key"]: f for f in listed.json()}
    assert flags[SEARCH_DOCUMENTS] == {
        "key": SEARCH_DOCUMENTS,
        "description": flags[SEARCH_DOCUMENTS]["description"],
        "default": False,
        "enabled": False,
        "overridden": False,
    }
    assert flags[WEBHOOKS_OUTBOUND]["enabled"] is True

    no_otp = await api.client.put(
        f"/v1/flags/{SEARCH_DOCUMENTS}",
        json={"enabled": True},
        headers=api.h(admin, org.id, otp=False),
    )
    assert no_otp.status_code == 403
    on = await api.client.put(
        f"/v1/flags/{SEARCH_DOCUMENTS}", json={"enabled": True}, headers=api.h(admin, org.id)
    )
    assert on.status_code == 200, on.text
    assert (on.json()["enabled"], on.json()["overridden"]) == (True, True)
    # Setting the same value again changes nothing and writes no second audit event.
    again = await api.client.put(
        f"/v1/flags/{SEARCH_DOCUMENTS}", json={"enabled": True}, headers=api.h(admin, org.id)
    )
    assert again.status_code == 200
    unknown = await api.client.put(
        "/v1/flags/nope.nothing", json={"enabled": True}, headers=api.h(admin, org.id)
    )
    assert unknown.status_code == 404
    malformed = await api.client.put(
        "/v1/flags/NOT-VALID", json={"enabled": True}, headers=api.h(admin, org.id)
    )
    assert malformed.status_code == 422

    # Another organisation keeps its defaults.
    other = await make_org("admin")
    theirs = await api.client.get("/v1/flags", headers=api.h(other.tenant.subject, other.id))
    assert {f["key"]: f["enabled"] for f in theirs.json()}[SEARCH_DOCUMENTS] is False

    auditor = await add_member_role(owner_engine, org.id, "auditor")
    assert (await api.client.get("/v1/flags", headers=api.h(auditor, org.id))).status_code == 200
    denied = await api.client.put(
        f"/v1/flags/{SEARCH_DOCUMENTS}", json={"enabled": False}, headers=api.h(auditor, org.id)
    )
    assert denied.status_code == 403

    reset = await api.client.delete(f"/v1/flags/{SEARCH_DOCUMENTS}", headers=api.h(admin, org.id))
    assert reset.status_code == 200
    assert (reset.json()["enabled"], reset.json()["overridden"]) == (False, False)

    audit = await owner_rows(
        owner_engine,
        org.id,
        "SELECT payload->>'from', payload->>'to' FROM audit_events "
        "WHERE action = 'core.flag.changed' ORDER BY seq",
    )
    assert [tuple(r) for r in audit] == [("false", "true"), ("true", "false")]


async def test_webhook_flag_pauses_delivery(api: Api, org: Org, owner_engine: AsyncEngine) -> None:
    sent: list[OutgoingWebhook] = []

    class Sender:
        async def send(self, webhook: OutgoingWebhook) -> SendResult:
            sent.append(webhook)
            return SendResult(200, 1)

    async def outbound(session: AsyncSession) -> bool:
        return await is_enabled(session, WEBHOOKS_OUTBOUND)

    api.app.state.webhook_resolver = _public_resolver
    created = await api.client.post(
        "/v1/webhooks/endpoints",
        json={"url": "https://hooks.example.com/in", "event_types": [DOCUMENT_RECEIVED]},
        headers=api.h(org.tenant.subject, org.id),
    )
    assert created.status_code == 201, created.text
    off = await api.client.put(
        f"/v1/flags/{WEBHOOKS_OUTBOUND}",
        json={"enabled": False},
        headers=api.h(org.tenant.subject, org.id),
    )
    assert off.status_code == 200
    async with api.sessionmaker() as session:
        set_tenant(session, org.id, None)
        async with session.begin():
            await publish_event(session, DOCUMENT_RECEIVED, {"document_id": "paused"})

    dispatcher = WebhookDispatcher(
        api.sessionmaker,
        Sender(),
        load_webhook_settings(env=Environment.TEST),
        enabled_for=outbound,
    )
    paused = await dispatcher.run_org(org.id)
    assert (paused.skipped, paused.fanned_out, paused.sent) == (True, 0, 0)
    ((waiting,),) = await owner_rows(
        owner_engine,
        org.id,
        "SELECT count(*) FROM webhook_outbox WHERE org_id = :o AND dispatched_at IS NULL",
        o=org.id,
    )
    assert waiting == 1  # kept, not dropped

    await api.client.delete(
        f"/v1/flags/{WEBHOOKS_OUTBOUND}", headers=api.h(org.tenant.subject, org.id)
    )
    resumed = await dispatcher.run_org(org.id)
    assert (resumed.skipped, resumed.fanned_out, resumed.succeeded) == (False, 1, 1)
    assert len(sent) == 1


async def _public_resolver(host: str, port: int) -> list[str]:
    return ["93.184.215.14"]


# ------------------------------------------------------------------ job runs


async def test_job_runs(api: Api, app_engine: AsyncEngine) -> None:
    sm = api.sessionmaker
    name = f"test_job_{uuid.uuid4().hex[:8]}"

    async def ok(ctx: dict[str, Any]) -> dict[str, Any]:
        return {"orgs": 2, "broken": ["a", "b", "c"]}

    async def boom(ctx: dict[str, Any]) -> None:
        raise KeyError("secret detail that must not be stored")

    assert await tracked(name, ok)({"sessionmaker": sm}) == {"orgs": 2, "broken": ["a", "b", "c"]}
    with pytest.raises(KeyError):
        await tracked(name, boom)({"sessionmaker": sm})

    async with sm() as session, session.begin():
        runs = await recent_runs(session, job_name=name)
    assert [r.status for r in runs] == ["failed", "succeeded"]
    failed, succeeded = runs
    assert succeeded.summary == {"orgs": 2, "broken": 3}
    assert failed.error == "KeyError"
    assert failed.summary is None
    assert all(r.finished_at is not None for r in runs)

    # A finished run never changes again.
    assert await finish_run(sm, succeeded.id, status="failed", error="Late") is False
    run_id = await start_run(sm, name)
    with pytest.raises(DBAPIError):
        await finish_run(sm, run_id, status="running")

    # The application role cannot touch the table directly.
    for sql in (
        "SELECT count(*) FROM job_runs",
        "UPDATE job_runs SET status = 'succeeded'",
        "DELETE FROM job_runs",
        "INSERT INTO job_runs (job_name) VALUES ('forged')",
    ):
        async with app_engine.connect() as conn:
            with pytest.raises(DBAPIError, match="permission denied"):
                await conn.execute(text(sql))


# ------------------------------------------------------------------ retention


async def test_retention_dry_run(api: Api, org: Org, owner_engine: AsyncEngine) -> None:
    # Old rows (as the owner, backdated) and fresh ones, for two rules.
    await owner_exec(
        owner_engine,
        org.id,
        "INSERT INTO inbound_events (org_id, provider, event_id, received_at) VALUES "
        "(:o, 'stripe', 'old-1', now() - interval '100 days'), "
        "(:o, 'stripe', 'old-2', now() - interval '91 days'), "
        "(:o, 'stripe', 'new-1', now() - interval '1 day')",
        o=org.id,
    )
    await owner_exec(
        owner_engine,
        org.id,
        "INSERT INTO webhook_outbox (org_id, event_type, data, created_at, dispatched_at) VALUES "
        "(:o, 'document.received', '{}', now() - interval '40 days', now() - interval '40 days'), "
        "(:o, 'document.received', '{}', now(), NULL)",
        o=org.id,
    )
    summary = await run_dry_run(api.sessionmaker, RetentionRegistry())
    assert summary.errors == 0
    assert summary.orgs >= 1
    assert summary.as_dict()["dry_run"] is True

    report = await api.client.get("/v1/retention/report", headers=api.h(org.tenant.subject, org.id))
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["run_id"] == str(summary.run_id)
    counts = {r["rule"]: r["would_delete"] for r in body["rules"]}
    assert counts["inbound_events.seen"] == 2
    assert counts["webhook_outbox.dispatched"] == 1
    assert counts["webhook_deliveries.finished"] == 0
    # A dry run deletes nothing.
    ((events,),) = await owner_rows(
        owner_engine, org.id, "SELECT count(*) FROM inbound_events WHERE org_id = :o", o=org.id
    )
    assert events == 3

    rules = await api.client.get("/v1/retention/rules", headers=api.h(org.tenant.subject, org.id))
    assert {r["key"]: r["days"] for r in rules.json()}["inbound_events.seen"] == 90
    approver = await add_member_role(owner_engine, org.id, "approver")
    denied = await api.client.get("/v1/retention/report", headers=api.h(approver, org.id))
    assert denied.status_code == 403
    # Reports can only ever be dry runs.
    with pytest.raises(DBAPIError, match="dry_run_only"):
        await owner_exec(
            owner_engine,
            org.id,
            "INSERT INTO retention_reports (org_id, run_id, rule, cutoff, would_delete, dry_run) "
            "VALUES (:o, gen_random_uuid(), 'x.y', now(), 1, false)",
            o=org.id,
        )


# ------------------------------------------------------------------ search

INVOICE_LINES = [
    [
        ("Invoice 4471 from Gulf Office Supplies", 1),
        (
            "Twelve ergonomic office chairs delivered to the Dubai warehouse. Payment to "
            "AE07 0331 2345 6789 0123 456, questions to billing@gulf-office.example.",
            0,
        ),
    ]
]


async def _parsed_document(api: Api, org: uuid.UUID, workflow: str, label: str) -> uuid.UUID:
    normalized = build_document(INVOICE_LINES)
    raw = normalized.model_dump_json().encode()
    sha = hashlib.sha256(raw + label.encode()).hexdigest()
    key = derived_key(org, sha, "normalized.json")
    await api.store.put_bytes(key, raw, content_type="application/json")
    async with api.sessionmaker() as session:
        set_tenant(session, org, None)
        async with session.begin():
            document = Document(
                org_id=org,
                workflow=workflow,
                source="upload",
                original_filename=f"{label}.pdf",
                media_type="application/pdf",
                size_bytes=len(raw),
                sha256=sha,
                storage_key=blob_key(org, sha),
                status=DocumentStatus.PARSED.value,
                parse_result_key=key,
            )
            session.add(document)
            await session.flush()
            return document.id


async def test_unified_search(api: Api, org: Org, owner_engine: AsyncEngine) -> None:
    admin = org.tenant.subject
    embedder = HashingEmbedder()
    await add_policy(
        owner_engine,
        org.id,
        name="spend",
        title="Spend policy",
        pages=SPEND_POLICY,
        embedder=embedder,
    )
    pay_doc = await _parsed_document(api, org.id, "pay", "invoice-4471")
    toy_doc = await _parsed_document(api, org.id, "toy", "toy-4471")

    kinds = await api.client.get("/v1/search/kinds", headers=api.h(admin, org.id))
    assert [k["kind"] for k in kinds.json()] == ["policy"]
    # The document flag is off: indexing is skipped and the kind cannot be searched.
    skipped = await index_document_after_parse(
        api.sessionmaker, api.store, org_id=org.id, document_id=pay_doc, embedder=embedder
    )
    assert (skipped.status, skipped.reason) == ("skipped", "flag_off")
    refused = await api.client.post(
        "/v1/search",
        json={"query": "office chairs", "kinds": ["document"]},
        headers=api.h(admin, org.id),
    )
    assert refused.status_code == 400

    on = await api.client.put(
        f"/v1/flags/{SEARCH_DOCUMENTS}", json={"enabled": True}, headers=api.h(admin, org.id)
    )
    assert on.status_code == 200
    for document_id in (pay_doc, toy_doc):
        outcome = await index_document_after_parse(
            api.sessionmaker, api.store, org_id=org.id, document_id=document_id, embedder=embedder
        )
        assert outcome.status == "indexed", outcome
        assert outcome.chunks >= 1
    # PII never reaches the index (or the embedding provider).
    stored = await owner_rows(
        owner_engine,
        org.id,
        "SELECT text FROM search_chunks WHERE kind = 'document' AND source_id = :d",
        d=pay_doc,
    )
    joined = " ".join(r[0] for r in stored)
    assert "AE07" not in joined
    assert "billing@gulf-office.example" not in joined
    assert "[IBAN]" in joined

    found = await api.client.post(
        "/v1/search", json={"query": "ergonomic office chairs"}, headers=api.h(admin, org.id)
    )
    assert found.status_code == 200, found.text
    hits = found.json()
    assert {(h["kind"], h["source_id"]) for h in hits if h["kind"] == "document"} == {
        ("document", str(pay_doc)),
        ("document", str(toy_doc)),
    }
    pay_hit = next(h for h in hits if h["source_id"] == str(pay_doc))
    assert (pay_hit["title"], pay_hit["workflow"]) == ("invoice-4471.pdf", "pay")
    policy = await api.client.post(
        "/v1/search",
        json={"query": "software purchases approval", "kinds": ["policy"]},
        headers=api.h(admin, org.id),
    )
    assert policy.status_code == 200
    assert policy.json()
    assert {h["kind"] for h in policy.json()} == {"policy"}
    assert policy.json()[0]["title"] == "Spend policy"

    # An approver reads pay documents but not toy ones: toy hits never reach them.
    approver = await add_member_role(owner_engine, org.id, "approver")
    theirs = await api.client.post(
        "/v1/search",
        json={"query": "ergonomic office chairs", "kinds": ["document"]},
        headers=api.h(approver, org.id),
    )
    assert theirs.status_code == 200, theirs.text
    assert {h["source_id"] for h in theirs.json()} == {str(pay_doc)}

    # Removing the document from the index removes its hits.
    async with api.sessionmaker() as session:
        set_tenant(session, org.id, None)
        async with session.begin():
            assert await remove_document(session, pay_doc) >= 1
    after = await api.client.post(
        "/v1/search",
        json={"query": "ergonomic office chairs", "kinds": ["document"]},
        headers=api.h(approver, org.id),
    )
    assert after.json() == []

    # Turning the flag off hides the kind again at once.
    await api.client.delete(f"/v1/flags/{SEARCH_DOCUMENTS}", headers=api.h(admin, org.id))
    kinds_after = await api.client.get("/v1/search/kinds", headers=api.h(admin, org.id))
    assert [k["kind"] for k in kinds_after.json()] == ["policy"]


async def test_turning_document_search_off_purges_the_index(
    api: Api, org: Org, owner_engine: AsyncEngine
) -> None:
    from pairly_api.decision_services import DecisionServices
    from pairly_core.decisions.jobs import NullTaskQueue, RecordingTaskQueue
    from pairly_core.search.documents import PURGE_JOB
    from pairly_worker.documents import purge_document_search_job

    admin = org.tenant.subject
    embedder = HashingEmbedder()
    doc = await _parsed_document(api, org.id, "pay", "invoice-purge")
    queue = RecordingTaskQueue()
    api.app.state.decision_services = DecisionServices(queue=queue)

    async def chunks() -> int:
        rows = await owner_rows(
            owner_engine, org.id, "SELECT count(*) FROM search_chunks WHERE kind = 'document'"
        )
        return int(rows[0][0])

    async def index() -> None:
        outcome = await index_document_after_parse(
            api.sessionmaker, api.store, org_id=org.id, document_id=doc, embedder=embedder
        )
        assert outcome.status == "indexed", outcome

    async def flag(enabled: bool | None) -> None:
        url = f"/v1/flags/{SEARCH_DOCUMENTS}"
        if enabled is None:
            response = await api.client.delete(url, headers=api.h(admin, org.id))
        else:
            response = await api.client.put(
                url, json={"enabled": enabled}, headers=api.h(admin, org.id)
            )
        assert response.status_code == 200, response.text

    await flag(True)
    assert queue.jobs == []  # turning it on queues nothing
    await index()
    assert await chunks() >= 1

    # Off: the purge is queued for the worker (after the commit), which deletes it all.
    await flag(False)
    ((job, args, _),) = queue.jobs
    assert (job, args) == (PURGE_JOB, (str(org.id),))
    assert await chunks() >= 1
    result = await purge_document_search_job({"sessionmaker": api.sessionmaker}, str(org.id))
    assert result["chunks"] >= 1
    assert await chunks() == 0
    purged = await owner_rows(
        owner_engine,
        org.id,
        "SELECT payload->>'chunks' FROM audit_events WHERE action = 'core.search.documents_purged'",
    )
    assert [int(r[0]) for r in purged] == [result["chunks"]]

    # A purge that runs after the flag came back on deletes nothing.
    await flag(True)
    await index()
    stale = await purge_document_search_job({"sessionmaker": api.sessionmaker}, str(org.id))
    assert stale == {"chunks": None, "skipped": True}
    assert await chunks() >= 1

    # No queue: the request purges itself (also through DELETE back to the default).
    api.app.state.decision_services = DecisionServices(queue=NullTaskQueue())
    await flag(None)
    assert await chunks() == 0
