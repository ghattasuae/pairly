# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Documents on real Postgres: upload, dedupe, limits, RBAC on signed URLs, tenant
isolation, scan and parse jobs (fail closed), inbound email and the sweeper.

Tenant fixtures: ``a`` is an approver in org A (reads, cannot upload), ``b`` is an
admin in org B. Storage is in memory, the queue records enqueues, the scanner is fake.
"""

from __future__ import annotations

import dataclasses
import io
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from pairly_api.main import create_app
from pairly_api.routes.documents import DocumentServices
from pairly_core.config import Environment
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.documents.pipeline import ParseConfig, parse_document
from pairly_core.documents.registry import ParseOptions
from pairly_core.documents.sandbox import SandboxLimits, SandboxResult
from pairly_core.ingestion.email import ingest_email
from pairly_core.ingestion.models import Document
from pairly_core.ingestion.queue import PARSE_JOB, SCAN_JOB, RecordingJobQueue
from pairly_core.ingestion.scan import EICAR, FakeScanner, scan_document
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_core.plugins import WorkflowPlugin
from pairly_core.rbac import PermissionSpec
from pairly_core.workflow.spec import GraphSpec
from pairly_hire.plugin import plugin as hire_plugin
from pairly_pay.plugin import plugin as pay_plugin
from pairly_worker import documents as worker_docs

from .conftest import DbUrls, TokenFactory, TwoTenants, make_settings


@dataclasses.dataclass(frozen=True)
class ToyPlugin:
    """A workflow that owns its document permissions: only admins read or upload."""

    id: str = "toy"
    name: str = "Toy"
    permissions: tuple[PermissionSpec, ...] = (
        PermissionSpec("toy.document.read", "Read toy documents", frozenset({"admin"})),
        PermissionSpec("toy.document.upload", "Upload toy documents", frozenset({"admin"})),
    )
    graphs: tuple[GraphSpec, ...] = ()


PLUGINS: list[WorkflowPlugin] = [hire_plugin, pay_plugin, ToyPlugin()]
WORKFLOWS = frozenset({"hire", "pay", "toy"})
DOMAIN = "inbound.pairly.localhost"


def pdf_bytes(label: str) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.drawString(72, 720, f"Synthetic invoice {label}")
    pdf.save()
    return buf.getvalue()


def ingestion_settings(**kw: Any) -> IngestionSettings:
    values: dict[str, Any] = {
        "env": Environment.TEST,
        "s3_bucket": "test",
        "s3_access_key": "test",
        "s3_secret_key": "test-secret",
        "inbound_email_domain": DOMAIN,
        "intake_policies": {
            "hire": {"allowed_types": ["application/pdf"], "max_bytes": 4096},
        },
        "parse_memory_mb": 768,
        "ocr_languages": "eng",
    }
    values.update(kw)
    return IngestionSettings(**values)


@dataclasses.dataclass
class Live:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory
    store: InMemoryObjectStore
    queue: RecordingJobQueue
    settings: IngestionSettings

    def auth(self, subject: str, org: uuid.UUID) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tokens.token(subject)}",
            "X-Pairly-Org": str(org),
        }

    async def upload(
        self,
        subject: str,
        org: uuid.UUID,
        data: bytes,
        *,
        workflow: str = "pay",
        filename: str = "invoice.pdf",
    ) -> httpx.Response:
        return await self.client.post(
            f"/v1/documents?workflow={workflow}",
            files={"file": (filename, data, "application/pdf")},
            headers=self.auth(subject, org),
        )


@pytest.fixture
async def live(migrated: DbUrls, tokens: TokenFactory, tmp_path: Path) -> AsyncIterator[Live]:
    store, queue = InMemoryObjectStore(), RecordingJobQueue()
    settings = ingestion_settings()
    services = DocumentServices(settings=settings, store=store, queue=queue, tmp_dir=tmp_path)
    app = create_app(make_settings(migrated.app), plugins=PLUGINS, documents=services)
    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
            yield Live(app, c, tokens, store, queue, settings)


async def _in_org(conn: Any, org: uuid.UUID) -> None:
    await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)})


@pytest.fixture
async def orgs(owner_engine: AsyncEngine, tenants: TwoTenants) -> AsyncIterator[TwoTenants]:
    yield tenants
    for org in (tenants.a.org_id, tenants.b.org_id):
        async with owner_engine.begin() as conn:
            await _in_org(conn, org)
            await conn.execute(text("DELETE FROM documents"))
            await conn.execute(text("DELETE FROM inbound_emails"))
            await conn.execute(
                text(
                    "DELETE FROM memberships WHERE user_id IN "
                    "(SELECT id FROM users WHERE auth_subject LIKE 'sub-extra-%')"
                )
            )
            await conn.execute(text("DELETE FROM users WHERE auth_subject LIKE 'sub-extra-%'"))


async def add_member(owner_engine: AsyncEngine, org: uuid.UUID, role: str) -> str:
    subject = f"sub-extra-{uuid.uuid4().hex[:10]}"
    async with owner_engine.begin() as conn:
        await _in_org(conn, org)
        await conn.execute(text("SELECT set_config('app.auth_subject', :s, true)"), {"s": subject})
        user_id = uuid.uuid4()
        await conn.execute(
            text("INSERT INTO users (id, auth_subject, email) VALUES (:id, :s, :e)"),
            {"id": user_id, "s": subject, "e": f"{subject}@example.test"},
        )
        await conn.execute(
            text(
                "INSERT INTO memberships (org_id, user_id, role_id) "
                "SELECT :o, :u, r.id FROM roles r WHERE r.key = :role AND r.org_id IS NULL"
            ),
            {"o": org, "u": user_id, "role": role},
        )
    return subject


async def audit_actions(owner_engine: AsyncEngine, org: uuid.UUID) -> list[str]:
    async with owner_engine.begin() as conn:
        await _in_org(conn, org)
        rows = await conn.execute(text("SELECT action FROM audit_events ORDER BY seq"))
        return [r.action for r in rows]


async def document_rows(owner_engine: AsyncEngine, org: uuid.UUID) -> list[Any]:
    async with owner_engine.begin() as conn:
        await _in_org(conn, org)
        return list((await conn.execute(text("SELECT * FROM documents ORDER BY created_at"))).all())


def sessions(app_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(app_engine)


# ------------------------------------------------------------------ upload


async def test_upload_dedupe_and_audit(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine
) -> None:
    b = orgs.b
    data = pdf_bytes("upload")
    first = await live.upload(b.subject, b.org_id, data, filename="../../Invoice\u202e.pdf")
    assert first.status_code == 201, first.text
    body = first.json()
    doc = body["document"]
    assert body["duplicate"] is False
    assert doc["status"] == "received"
    assert doc["media_type"] == "application/pdf"
    assert doc["source"] == "upload"
    assert doc["original_filename"] == "Invoice.pdf"
    assert doc["size_bytes"] == len(data)
    assert doc["submitted_by"] == str(b.user_id)
    # Stored under org and hash, never under the client's name.
    key = f"orgs/{b.org_id}/blobs/sha256/{doc['sha256'][:2]}/{doc['sha256']}"
    assert live.store.objects[key].data == data
    # The scan was queued after the commit.
    assert live.queue.jobs == [(SCAN_JOB, b.org_id, uuid.UUID(doc["id"]), "0")]

    again = await live.upload(b.subject, b.org_id, data, filename="other-name.pdf")
    assert again.status_code == 200
    assert again.json()["document_id"] == doc["id"]
    assert again.json()["duplicate"] is True
    assert live.store.puts == 1
    assert len(live.queue.jobs) == 1
    assert len(await document_rows(owner_engine, b.org_id)) == 1
    actions = await audit_actions(owner_engine, b.org_id)
    assert actions.count("core.document.received") == 1
    assert actions.count("core.document.duplicate") == 1


async def test_duplicate_in_unreadable_workflow_hides_details(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine
) -> None:
    b = orgs.b
    data = pdf_bytes("toy")
    assert (await live.upload(b.subject, b.org_id, data, workflow="toy")).status_code == 201
    clerk = await add_member(owner_engine, b.org_id, "ap_clerk")
    response = await live.upload(clerk, b.org_id, data, workflow="pay")
    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert response.json()["document"] is None


async def test_type_and_size_limits(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine
) -> None:
    b = orgs.b
    exe = await live.upload(b.subject, b.org_id, b"MZ\x90\x00 synthetic", filename="cv.pdf")
    assert exe.status_code == 415
    assert exe.headers["content-type"] == "application/problem+json"
    # The hire policy allows 4 KiB PDFs only.
    big = pdf_bytes("x") + b"%" + b"0" * 8000
    too_big = await live.upload(b.subject, b.org_id, big, workflow="hire")
    assert too_big.status_code == 413
    empty = await live.upload(b.subject, b.org_id, b"")
    assert empty.status_code == 400

    # A body without Content-Length is cut off while streaming.
    async def stream() -> AsyncIterator[bytes]:
        boundary = b"--b0undary"
        yield (
            boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n\r\n'
        )
        for _ in range(20):
            yield b"%PDF-1.4" + b"0" * 1000
        yield b"\r\n" + boundary + b"--\r\n"

    headers = live.auth(b.subject, b.org_id) | {
        "Content-Type": "multipart/form-data; boundary=b0undary"
    }
    streamed = await live.client.post(
        "/v1/documents?workflow=hire", content=stream(), headers=headers
    )
    assert streamed.status_code == 413
    assert await document_rows(owner_engine, b.org_id) == []
    actions = await audit_actions(owner_engine, b.org_id)
    assert actions.count("core.document.rejected") == 4


async def test_malformed_requests(live: Live, orgs: TwoTenants) -> None:
    b = orgs.b
    auth = live.auth(b.subject, b.org_id)
    not_multipart = await live.client.post(
        "/v1/documents?workflow=pay", content=b"%PDF-1.4", headers=auth
    )
    assert not_multipart.status_code == 415
    urlencoded = await live.client.post(
        "/v1/documents?workflow=pay", data={"note": "hello"}, headers=auth
    )
    assert urlencoded.status_code == 415
    no_file = await live.client.post(
        "/v1/documents?workflow=pay",
        data={"note": "hello"},
        files={"other": ("a.txt", b"synthetic", "text/plain")},
        headers=auth,
    )
    assert no_file.status_code == 400
    oversized_field = await live.client.post(
        "/v1/documents?workflow=pay",
        data={"note": "x" * 10_000},
        files={"file": ("a.pdf", pdf_bytes("f"), "application/pdf")},
        headers=auth,
    )
    assert oversized_field.status_code == 400
    garbage = await live.client.post(
        "/v1/documents?workflow=pay",
        content=b"--b\r\nno headers end",
        headers=auth | {"Content-Type": "multipart/form-data; boundary=b"},
    )
    assert garbage.status_code == 400
    two_files = await live.client.post(
        "/v1/documents?workflow=pay",
        files=[("file", ("a.pdf", b"%PDF-1", "x/y")), ("file", ("b.pdf", b"%PDF-2", "x/y"))],
        headers=auth,
    )
    assert two_files.status_code == 400
    unknown = await live.upload(b.subject, b.org_id, pdf_bytes("u"), workflow="nope")
    assert unknown.status_code == 400
    bad_id = await live.upload(b.subject, b.org_id, pdf_bytes("u"), workflow="Bad-Id")
    assert bad_id.status_code == 422


async def test_upload_needs_permission(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine
) -> None:
    a = orgs.a  # approver: reads documents, never uploads
    response = await live.upload(a.subject, a.org_id, pdf_bytes("denied"))
    assert response.status_code == 403
    assert "core.authz.denied" in await audit_actions(owner_engine, a.org_id)
    assert live.store.objects == {}


async def test_documents_service_missing_is_503(
    migrated: DbUrls, tokens: TokenFactory, orgs: TwoTenants
) -> None:
    app = create_app(make_settings(migrated.app), plugins=PLUGINS)
    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
            response = await c.post(
                "/v1/documents?workflow=pay",
                files={"file": ("a.pdf", b"%PDF-1", "application/pdf")},
                headers={
                    "Authorization": f"Bearer {tokens.token(orgs.b.subject)}",
                    "X-Pairly-Org": str(orgs.b.org_id),
                },
            )
    assert response.status_code == 503


# ------------------------------------------------------------------ read, signed URLs, isolation


async def _uploaded(live: Live, subject: str, org: uuid.UUID, label: str, **kw: Any) -> str:
    response = await live.upload(subject, org, pdf_bytes(label), **kw)
    assert response.status_code == 201, response.text
    return str(response.json()["document_id"])


async def test_signed_url_only_after_clean_scan_and_rbac(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    b = orgs.b
    doc_id = await _uploaded(live, b.subject, b.org_id, "signed")
    auth = live.auth(b.subject, b.org_id)
    early = await live.client.get(f"/v1/documents/{doc_id}/content-url", headers=auth)
    assert early.status_code == 409

    result = await scan_document(
        sessions(app_engine),
        live.store,
        FakeScanner(),
        org_id=b.org_id,
        document_id=uuid.UUID(doc_id),
    )
    assert (result.status, result.parse_next) == ("clean", True)

    ok = await live.client.get(f"/v1/documents/{doc_id}/content-url", headers=auth)
    assert ok.status_code == 200
    payload = ok.json()
    assert live.store.verify_url(payload["url"])
    assert payload["media_type"] == "application/pdf"
    assert "content_url_issued" in " ".join(await audit_actions(owner_engine, b.org_id))

    # Same org, but the toy workflow lets admins only: an approver gets no URL.
    toy_id = await _uploaded(live, b.subject, b.org_id, "toy-signed", workflow="toy")
    await scan_document(
        sessions(app_engine),
        live.store,
        FakeScanner(),
        org_id=b.org_id,
        document_id=uuid.UUID(toy_id),
    )
    approver = await add_member(owner_engine, b.org_id, "approver")
    denied = await live.client.get(
        f"/v1/documents/{toy_id}/content-url", headers=live.auth(approver, b.org_id)
    )
    assert denied.status_code == 403
    readable = await live.client.get(
        f"/v1/documents/{doc_id}/content-url", headers=live.auth(approver, b.org_id)
    )
    assert readable.status_code == 200
    detail = await live.client.get(f"/v1/documents/{toy_id}", headers=live.auth(approver, b.org_id))
    assert detail.status_code == 403


async def test_tenant_isolation(live: Live, orgs: TwoTenants, app_engine: AsyncEngine) -> None:
    a, b = orgs.a, orgs.b
    doc_id = await _uploaded(live, b.subject, b.org_id, "isolated")
    own = await live.client.get(f"/v1/documents/{doc_id}", headers=live.auth(b.subject, b.org_id))
    assert own.status_code == 200
    # Org A's member asking for org B's document: not found, under either org header.
    foreign = await live.client.get(
        f"/v1/documents/{doc_id}", headers=live.auth(a.subject, a.org_id)
    )
    assert foreign.status_code == 404
    url = await live.client.get(
        f"/v1/documents/{doc_id}/content-url", headers=live.auth(a.subject, a.org_id)
    )
    assert url.status_code == 404
    spoofed = await live.client.get(
        f"/v1/documents/{doc_id}", headers=live.auth(a.subject, b.org_id)
    )
    assert spoofed.status_code == 403

    maker = sessions(app_engine)
    async with maker() as session:
        set_tenant(session, a.org_id, a.subject)
        async with session.begin():
            visible = (await session.execute(text("SELECT id FROM documents"))).all()
            assert visible == []
            updated = await session.execute(text("UPDATE documents SET status = 'clean'"))
            assert updated.rowcount == 0  # type: ignore[attr-defined]
    async with maker() as session:
        set_tenant(session, a.org_id, a.subject)
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO documents (org_id, workflow, source, media_type, size_bytes,"
                        " sha256, storage_key, status) VALUES (:o, 'pay', 'api', 'x', 1,"
                        " repeat('a', 64), 'k', 'received')"
                    ),
                    {"o": b.org_id},
                )
    async with maker() as session:
        set_tenant(session, None, None)
        async with session.begin():
            assert (await session.execute(text("SELECT id FROM documents"))).all() == []
            # The app role cannot delete documents at all.
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(text("DELETE FROM documents"))


# ------------------------------------------------------------------ scan and parse jobs


async def test_scan_fails_closed(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    b = orgs.b
    doc_id = uuid.UUID(await _uploaded(live, b.subject, b.org_id, "scan-down"))
    maker = sessions(app_engine)
    down = FakeScanner(fail="scanner_unavailable")
    first = await scan_document(
        maker, live.store, down, org_id=b.org_id, document_id=doc_id, max_attempts=2
    )
    assert (first.status, first.reason, first.parse_next) == (
        "scanning",
        "scanner_unavailable",
        False,
    )
    (row,) = await document_rows(owner_engine, b.org_id)
    assert (row.status, row.needs_review, row.status_reason, row.scan_attempts) == (
        "scanning",
        True,
        "scanner_unavailable",
        1,
    )
    second = await scan_document(
        maker, live.store, down, org_id=b.org_id, document_id=doc_id, max_attempts=2
    )
    assert second.reason == "scan_attempts_exhausted"
    third = await scan_document(
        maker, live.store, FakeScanner(), org_id=b.org_id, document_id=doc_id, max_attempts=2
    )
    assert third.skipped
    url = await live.client.get(
        f"/v1/documents/{doc_id}/content-url", headers=live.auth(b.subject, b.org_id)
    )
    assert url.status_code == 409

    # With attempts left, a later clean scan clears the review flag.
    doc2 = uuid.UUID(await _uploaded(live, b.subject, b.org_id, "scan-recovers"))
    await scan_document(maker, live.store, down, org_id=b.org_id, document_id=doc2)
    clean = await scan_document(maker, live.store, FakeScanner(), org_id=b.org_id, document_id=doc2)
    assert clean.status == "clean"
    rows = {r.id: r for r in await document_rows(owner_engine, b.org_id)}
    assert (rows[doc2].needs_review, rows[doc2].status_reason) == (False, None)
    actions = await audit_actions(owner_engine, b.org_id)
    assert actions.count("core.document.scan_failed") == 3
    assert "core.document.scan_clean" in actions


async def test_infected_and_tampered(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    b = orgs.b
    maker = sessions(app_engine)
    eicar_pdf = pdf_bytes("eicar") + b"\n" + EICAR
    infected_id = uuid.UUID(
        (await live.upload(b.subject, b.org_id, eicar_pdf)).json()["document_id"]
    )
    result = await scan_document(
        maker, live.store, FakeScanner(), org_id=b.org_id, document_id=infected_id
    )
    assert (result.status, result.parse_next) == ("infected", False)
    url = await live.client.get(
        f"/v1/documents/{infected_id}/content-url", headers=live.auth(b.subject, b.org_id)
    )
    assert url.status_code == 409

    tampered_id = uuid.UUID(await _uploaded(live, b.subject, b.org_id, "tampered"))
    rows = {r.id: r for r in await document_rows(owner_engine, b.org_id)}
    live.store.objects[rows[tampered_id].storage_key].data = b"%PDF-1.4 swapped"
    swapped = await scan_document(
        maker, live.store, FakeScanner(), org_id=b.org_id, document_id=tampered_id
    )
    assert (swapped.status, swapped.reason) == ("scanning", "integrity_mismatch")
    del live.store.objects[rows[tampered_id].storage_key]
    missing = await scan_document(
        maker, live.store, FakeScanner(), org_id=b.org_id, document_id=tampered_id
    )
    assert missing.reason == "object_missing"
    nothing = await scan_document(
        maker, live.store, FakeScanner(), org_id=b.org_id, document_id=uuid.uuid4()
    )
    assert nothing.skipped


async def test_parse_job(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    b = orgs.b
    maker = sessions(app_engine)
    doc_id = uuid.UUID(await _uploaded(live, b.subject, b.org_id, "parse-me"))
    config = ParseConfig(
        options=ParseOptions(ocr_languages="eng"),
        limits=SandboxLimits(wall_seconds=60, memory_bytes=768 * 1024 * 1024),
    )
    early = await parse_document(
        maker, live.store, org_id=b.org_id, document_id=doc_id, config=config
    )
    assert early.skipped  # not scanned yet
    await scan_document(maker, live.store, FakeScanner(), org_id=b.org_id, document_id=doc_id)
    result = await parse_document(
        maker, live.store, org_id=b.org_id, document_id=doc_id, config=config
    )
    assert (result.status, result.needs_review) == ("parsed", False)
    assert result.result_key is not None
    stored = live.store.objects[result.result_key].data
    assert b"Synthetic invoice parse-me" in stored
    (row,) = await document_rows(owner_engine, b.org_id)
    assert (row.parser, row.page_count, row.parse_warnings) == ("pdf", 1, [])
    again = await parse_document(
        maker, live.store, org_id=b.org_id, document_id=doc_id, config=config
    )
    assert again.skipped
    assert "core.document.parsed" in await audit_actions(owner_engine, b.org_id)


async def test_parse_failures_fail_closed(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    b = orgs.b
    maker = sessions(app_engine)
    config = ParseConfig(options=ParseOptions(), limits=SandboxLimits())

    async def killed(*_: Any, **__: Any) -> SandboxResult:
        return SandboxResult(document=None, error="timeout", detail="killed")

    async def broken(*_: Any, **__: Any) -> SandboxResult:
        raise RuntimeError("runner bug")

    for label, runner, reason in (("t", killed, "timeout"), ("r", broken, "sandbox_error")):
        doc_id = uuid.UUID(await _uploaded(live, b.subject, b.org_id, f"fail-{label}"))
        await scan_document(maker, live.store, FakeScanner(), org_id=b.org_id, document_id=doc_id)
        result = await parse_document(
            maker, live.store, org_id=b.org_id, document_id=doc_id, config=config, runner=runner
        )
        assert (result.status, result.reason, result.needs_review) == ("parse_failed", reason, True)
    actions = await audit_actions(owner_engine, b.org_id)
    assert actions.count("core.document.parse_failed") == 2


# ------------------------------------------------------------------ inbound email


async def _slug(owner_engine: AsyncEngine, org: uuid.UUID) -> str:
    async with owner_engine.begin() as conn:
        await _in_org(conn, org)
        return str(await conn.scalar(text("SELECT slug FROM organisations")))


def _email(
    to: str,
    *,
    attachments: tuple[tuple[str, bytes], ...] = (),
    body: str = "See attached.",
    message_id: str = "<m-1@vendor.example>",
) -> bytes:
    from email.message import EmailMessage
    from email.policy import SMTP

    msg = EmailMessage()
    msg["From"] = "Billing <billing@vendor.example>"
    msg["To"] = to
    msg["Subject"] = "Invoice"
    msg["Message-ID"] = message_id
    msg.set_content(body)
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return msg.as_bytes(policy=SMTP)


async def test_ingest_email(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    b = orgs.b
    maker = sessions(app_engine)
    slug = await _slug(owner_engine, b.org_id)
    address = f"pay+{slug}@{DOMAIN}"
    raw = _email(
        f"Accounts <{address}>",
        attachments=(("inv.pdf", pdf_bytes("mail")), ("tool.exe", b"MZ\x90\x00 synthetic")),
        message_id=f"<m-{uuid.uuid4().hex}@vendor.example>",
    )
    kwargs: dict[str, Any] = {
        "sessionmaker": maker,
        "store": live.store,
        "settings": live.settings,
        "workflows": WORKFLOWS,
    }
    result = await ingest_email(raw, **kwargs)
    assert (result.status, result.org_id, result.workflow) == ("ingested", b.org_id, "pay")
    outcomes = sorted((item.filename, item.outcome, item.reason) for item in result.items)
    assert outcomes == [("inv.pdf", "created", None), ("tool.exe", "rejected", "type_not_allowed")]
    (row,) = await document_rows(owner_engine, b.org_id)
    assert (row.source, row.inbound_email_id, row.submitted_by) == (
        "email",
        result.inbound_email_id,
        None,
    )
    duplicate = await ingest_email(raw, **kwargs)
    assert duplicate.status == "duplicate"
    assert duplicate.inbound_email_id == result.inbound_email_id

    body_only = _email(
        address,
        body="Invoice 55 for consulting, total 900 AED, due in 30 days.",
        message_id="<body-only@vendor.example>",
    )
    in_body = await ingest_email(body_only, **kwargs)
    assert [(i.filename, i.outcome) for i in in_body.items] == [("email-body.txt", "created")]

    unknown_org = await ingest_email(_email(f"pay+no-such-org@{DOMAIN}"), **kwargs)
    assert unknown_org.status == "unroutable"
    elsewhere = await ingest_email(_email("someone@example.test"), **kwargs)
    assert elsewhere.status == "unroutable"
    big = await ingest_email(
        raw, **{**kwargs, "settings": ingestion_settings(max_email_bytes=1024)}
    )
    assert big.status == "too_large"
    via_envelope = await ingest_email(
        _email("undisclosed-recipients:;", message_id="<env@vendor.example>", body="Short"),
        envelope_recipients=[address],
        **kwargs,
    )
    assert via_envelope.status == "ingested"
    actions = await audit_actions(owner_engine, b.org_id)
    assert actions.count("core.email.received") == 3
    assert "core.email.duplicate" in actions
    assert "core.document.rejected" in actions


async def test_inbound_org_lookup_is_narrow(
    orgs: TwoTenants, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    slug = await _slug(owner_engine, orgs.a.org_id)
    async with app_engine.begin() as conn:
        assert await conn.scalar(text("SELECT inbound_org_id(:s)"), {"s": slug}) == orgs.a.org_id
        assert await conn.scalar(text("SELECT inbound_org_id(:s)"), {"s": "Bad Slug!"}) is None
        # The flag never leaks out of the function.
        visible = (await conn.execute(text("SELECT id FROM organisations"))).all()
        assert visible == []
    async with owner_engine.begin() as conn:
        await _in_org(conn, orgs.a.org_id)
        await conn.execute(text("UPDATE organisations SET status = 'suspended'"))
    try:
        async with app_engine.begin() as conn:
            assert await conn.scalar(text("SELECT inbound_org_id(:s)"), {"s": slug}) is None
    finally:
        async with owner_engine.begin() as conn:
            await _in_org(conn, orgs.a.org_id)
            await conn.execute(text("UPDATE organisations SET status = 'active'"))


# ------------------------------------------------------------------ worker jobs and sweeper


class _Redis:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, str, str, str]] = []

    async def enqueue_job(self, job: str, org: str, doc: str, *, _job_id: str) -> object:
        self.jobs.append((job, org, doc, _job_id))
        return object()


async def test_worker_jobs_and_sweeper(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    b = orgs.b
    redis = _Redis()
    ctx: dict[str, Any] = {
        "sessionmaker": sessions(app_engine),
        "store": live.store,
        "scanner": FakeScanner(),
        "ingestion": live.settings,
        "parse_config": ParseConfig(
            options=ParseOptions(ocr_languages="eng"),
            limits=SandboxLimits(memory_bytes=768 * 1024 * 1024),
        ),
        "redis": redis,
    }
    doc_id = await _uploaded(live, b.subject, b.org_id, "worker")
    scanned = await worker_docs.scan_document_job(ctx, str(b.org_id), doc_id)
    assert scanned["status"] == "clean"
    assert redis.jobs == [(PARSE_JOB, str(b.org_id), doc_id, f"{PARSE_JOB}:{doc_id}:0")]
    parsed = await worker_docs.parse_document_job(ctx, str(b.org_id), doc_id)
    assert parsed["status"] == "parsed"

    stalled_id = await _uploaded(live, b.subject, b.org_id, "stalled")
    clean_id = await _uploaded(live, b.subject, b.org_id, "stalled-clean")
    await scan_document(
        ctx["sessionmaker"],
        live.store,
        FakeScanner(),
        org_id=b.org_id,
        document_id=uuid.UUID(clean_id),
    )
    redis.jobs.clear()
    assert await worker_docs.sweep_documents(ctx) == {"queued": 0}
    async with owner_engine.begin() as conn:
        await _in_org(conn, b.org_id)
        await conn.execute(
            text(
                "UPDATE documents SET created_at = now() - interval '1 hour', "
                "updated_at = now() - interval '1 hour' WHERE id IN (:a, :c)"
            ),
            {"a": uuid.UUID(stalled_id), "c": uuid.UUID(clean_id)},
        )
    swept = await worker_docs.sweep_documents(ctx)
    assert swept == {"queued": 2}
    queued = {(job, doc) for job, _, doc, _ in redis.jobs}
    assert queued == {(SCAN_JOB, stalled_id), (PARSE_JOB, clean_id)}
    assert await worker_docs.sweep_documents({}) == {"queued": 0}


async def test_document_row_model_matches(
    live: Live, app_engine: AsyncEngine, orgs: TwoTenants
) -> None:
    """The ORM model reads every column the migration created, with server defaults."""
    from sqlalchemy import select

    doc_id = uuid.UUID(await _uploaded(live, orgs.b.subject, orgs.b.org_id, "orm"))
    async with sessions(app_engine)() as session:
        set_tenant(session, orgs.b.org_id, None)
        async with session.begin():
            doc = (await session.execute(select(Document))).scalar_one()
    assert doc.id == doc_id
    assert (doc.needs_review, doc.scan_attempts, doc.parse_warnings) == (False, 0, [])
    assert doc.created_at is not None
    assert doc.updated_at is not None


# ------------------------------------------------------------------ service wiring (no database)


async def test_document_services_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from pairly_api.main import _document_services
    from pairly_core.config import ConfigError
    from pairly_core.ingestion.queue import NullJobQueue
    from pairly_core.ingestion.storage import S3ObjectStore

    for name in ("PAIRLY_S3_BUCKET", "PAIRLY_S3_ACCESS_KEY", "PAIRLY_S3_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PAIRLY_REDIS_URL", raising=False)
    assert await _document_services(make_settings()) is None
    production = make_settings().model_copy(update={"env": Environment.PRODUCTION})
    with pytest.raises(ConfigError):
        await _document_services(production)
    monkeypatch.setenv("PAIRLY_S3_BUCKET", "documents")
    monkeypatch.setenv("PAIRLY_S3_ACCESS_KEY", "api")
    monkeypatch.setenv("PAIRLY_S3_SECRET_KEY", "synthetic-secret")
    services = await _document_services(make_settings())
    assert services is not None
    assert isinstance(services.store, S3ObjectStore)
    assert isinstance(services.queue, NullJobQueue)
