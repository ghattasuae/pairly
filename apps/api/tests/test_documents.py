# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Documents on real Postgres: upload, dedupe, limits, RBAC on signed URLs, tenant
isolation, scan and parse jobs (fail closed), inbound email and the sweeper.

Tenant fixtures: ``a`` is an approver in org A (reads, cannot upload), ``b`` is an
admin in org B. Storage is in memory, the queue records enqueues, the scanner is fake.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import re
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
from pairly_api.uploads import DocumentServices
from pairly_core.audit import ActorType
from pairly_core.config import Environment
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.documents.pipeline import ParseConfig, parse_document
from pairly_core.documents.registry import ParseOptions
from pairly_core.documents.sandbox import SandboxLimits, SandboxResult
from pairly_core.ingestion.addresses import (
    ROTATED_ACTION,
    hash_inbound_token,
    inbound_address,
    rotate_inbound_token,
)
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
            await conn.execute(text("DELETE FROM inbound_addresses"))
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


async def test_dedupe_is_scoped_per_workflow(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine
) -> None:
    b = orgs.b
    data = pdf_bytes("toy")
    toy = await live.upload(b.subject, b.org_id, data, workflow="toy")
    assert toy.status_code == 201
    toy_id = toy.json()["document"]["id"]
    # The same bytes in another workflow are a new document there: an upload never
    # reveals that another workflow (here one the clerk cannot read) holds the file.
    clerk = await add_member(owner_engine, b.org_id, "ap_clerk")
    pay = await live.upload(clerk, b.org_id, data, workflow="pay")
    assert pay.status_code == 201, pay.text
    pay_doc = pay.json()["document"]
    assert pay.json()["duplicate"] is False
    assert pay_doc["id"] != toy_id
    assert pay_doc["workflow"] == "pay"
    # Both rows share one stored object, keyed by org and hash.
    assert live.store.puts == 1
    rows = await document_rows(owner_engine, b.org_id)
    assert {r.workflow for r in rows} == {"toy", "pay"}
    assert len({r.storage_key for r in rows}) == 1
    # A duplicate inside the same workflow still returns the existing id.
    again = await live.upload(clerk, b.org_id, data, workflow="pay", filename="again.pdf")
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["document_id"] == pay_doc["id"]
    assert again.json()["document"]["id"] == pay_doc["id"]
    assert len(await document_rows(owner_engine, b.org_id)) == 2


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


def _docx_with_remote_template() -> bytes:
    import zipfile

    import docx  # type: ignore[import-untyped]

    buf = io.BytesIO()
    document = docx.Document()
    document.add_paragraph("Synthetic CV")
    document.save(buf)
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/attachedTemplate" Target="https://templates.example.invalid/t.dotm" '
        'TargetMode="External"/></Relationships>'
    )
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(buf.getvalue())) as src,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("word/_rels/settings.xml.rels", rels)
    return out.getvalue()


async def test_a_docx_with_a_remote_template_is_refused_with_its_reason(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine
) -> None:
    """Security review 2026-10 (L-12): refused at intake like macros, nothing stored,
    and the problem and the audit event say why."""
    b = orgs.b
    refused = await live.upload(
        b.subject, b.org_id, _docx_with_remote_template(), filename="cv.docx"
    )
    assert refused.status_code == 415
    problem = refused.json()
    assert problem["code"] == "unsupported_media_type"
    assert "remote template" in problem["title"]
    assert await document_rows(owner_engine, b.org_id) == []
    assert live.store.puts == 0
    async with owner_engine.begin() as conn:
        await _in_org(conn, b.org_id)
        payload = (
            await conn.execute(
                text(
                    "SELECT payload FROM audit_events WHERE action = 'core.document.rejected' "
                    "ORDER BY seq DESC LIMIT 1"
                )
            )
        ).scalar_one()
    assert payload["reason"] == "active_content"
    assert payload["detail"] == "external_template"
    assert payload["media_type"] == "application/zip"


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


async def _rotate(owner_engine: AsyncEngine, org: uuid.UUID, workflow: str = "pay") -> str:
    session = create_sessionmaker(owner_engine)()
    set_tenant(session, org, None)
    async with session, session.begin():
        return await rotate_inbound_token(
            session, workflow=workflow, actor_type=ActorType.SYSTEM, actor_id="pairly.test"
        )


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
    token = await _rotate(owner_engine, b.org_id)
    address = inbound_address("pay", slug, token, DOMAIN)
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

    unknown_org = await ingest_email(_email(f"pay+no-such-org.{token}@{DOMAIN}"), **kwargs)
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


async def test_inbound_address_needs_the_current_token(
    live: Live,
    orgs: TwoTenants,
    owner_engine: AsyncEngine,
    app_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    b = orgs.b
    slug = await _slug(owner_engine, b.org_id)
    kwargs: dict[str, Any] = {
        "sessionmaker": sessions(app_engine),
        "store": live.store,
        "settings": live.settings,
        "workflows": WORKFLOWS,
    }

    def mail(to: str) -> bytes:
        return _email(
            to,
            body="Invoice 77 for consulting, total 900 AED, due in 30 days.",
            message_id=f"<m-{uuid.uuid4().hex}@vendor.example>",
        )

    # No token configured yet: even the slug alone never routes.
    assert (await ingest_email(mail(f"pay+{slug}@{DOMAIN}"), **kwargs)).status == "unroutable"
    old = await _rotate(owner_engine, b.org_id)
    new = await _rotate(owner_engine, b.org_id)
    assert old != new
    other_workflow = await _rotate(owner_engine, b.org_id, "hire")
    caplog.clear()
    for to in (
        f"pay+{slug}@{DOMAIN}",
        inbound_address("pay", slug, old, DOMAIN),  # rotated away
        inbound_address("pay", slug, other_workflow, DOMAIN),  # another workflow's token
        inbound_address("pay", f"{slug}x", new, DOMAIN),  # another slug
    ):
        result = await ingest_email(mail(to), **kwargs)
        assert (result.status, result.org_id) == ("unroutable", None), to
    logs = [r.msg for r in caplog.records if isinstance(r.msg, dict)]
    unroutable = [e for e in logs if e.get("event") == "email_unroutable"]
    assert [e["reason"] for e in unroutable] == ["no_address"] + ["unknown_address"] * 3
    # Dropped and logged, never with the token.
    for token in (old, new, other_workflow):
        assert token not in repr(logs)
    assert unroutable[1]["addresses"] == [f"pay+{slug}"]
    # A stale address next to the current one still delivers to the current one.
    both = mail(
        f"{inbound_address('pay', slug, old, DOMAIN)}, {inbound_address('pay', slug, new, DOMAIN)}"
    )
    result = await ingest_email(both, **kwargs)
    assert (result.status, result.org_id, result.workflow) == ("ingested", b.org_id, "pay")
    assert (await audit_actions(owner_engine, b.org_id)).count(ROTATED_ACTION) == 3
    async with owner_engine.begin() as conn:
        await _in_org(conn, b.org_id)
        rows = (
            await conn.execute(text("SELECT workflow, token_hash FROM inbound_addresses"))
        ).all()
    assert sorted(rows) == sorted(
        [("pay", hash_inbound_token(new)), ("hire", hash_inbound_token(other_workflow))]
    )


async def test_cli_inbound_rotate(
    migrated: DbUrls,
    orgs: TwoTenants,
    owner_engine: AsyncEngine,
    app_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pairly_api import admin as admin_cli

    org = orgs.a.org_id
    slug = await _slug(owner_engine, org)
    monkeypatch.setenv(admin_cli.OWNER_URL_ENV, migrated.owner)
    monkeypatch.setenv(admin_cli.INBOUND_DOMAIN_ENV, DOMAIN)

    def run(*argv: str) -> tuple[int, str, str]:
        code = admin_cli.main(["inbound", "rotate", *argv])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    code, out, err = await asyncio.to_thread(run, "--org", str(org), "--workflow", "pay")
    assert code == 0, err
    match = re.search(rf"pay\+{re.escape(slug)}\.([a-z2-7]{{16}})@{re.escape(DOMAIN)}", out)
    assert match is not None, out
    first = match.group(1)
    code, out, _ = await asyncio.to_thread(run, "--org", str(org), "--workflow", "pay")
    assert code == 0
    second = re.search(r"\.([a-z2-7]{16})@", out).group(1)  # type: ignore[union-attr]
    assert second != first
    # Only the hash is stored, and the newest token is the one that routes.
    async with owner_engine.begin() as conn:
        await _in_org(conn, org)
        stored = (await conn.execute(text("SELECT token_hash FROM inbound_addresses"))).all()
    assert stored == [(hash_inbound_token(second),)]
    lookup = text("SELECT inbound_org_id('pay', :s, :h)")
    async with app_engine.begin() as conn:
        assert await conn.scalar(lookup, {"s": slug, "h": hash_inbound_token(second)}) == org
        assert await conn.scalar(lookup, {"s": slug, "h": hash_inbound_token(first)}) is None

    code, _, err = await asyncio.to_thread(run, "--org", str(org), "--workflow", "nope")
    assert code == 2
    assert "unknown workflow" in err
    code, _, err = await asyncio.to_thread(run, "--org", str(uuid.uuid4()), "--workflow", "pay")
    assert code == 1
    assert "organisation not found" in err


async def test_inbound_org_lookup_is_narrow(
    orgs: TwoTenants, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = orgs.a.org_id
    slug = await _slug(owner_engine, org)
    token_hash = hash_inbound_token(await _rotate(owner_engine, org))
    lookup = text("SELECT inbound_org_id(:w, :s, :h)")
    async with app_engine.begin() as conn:
        assert await conn.scalar(lookup, {"w": "pay", "s": slug, "h": token_hash}) == org
        for w, s_, h in (
            ("hire", slug, token_hash),
            ("pay", slug, "0" * 64),
            ("pay", "Bad Slug!", token_hash),
            ("pay", slug, "not-a-hash"),
            ("pay", slug, None),
        ):
            assert await conn.scalar(lookup, {"w": w, "s": s_, "h": h}) is None
        # The flag never leaks out of the function.
        assert (await conn.execute(text("SELECT id FROM organisations"))).all() == []
        assert (await conn.execute(text("SELECT id FROM inbound_addresses"))).all() == []
    # The application role can read its own org's row but never write a token.
    async with app_engine.connect() as conn:
        await _in_org(conn, org)
        assert len((await conn.execute(text("SELECT id FROM inbound_addresses"))).all()) == 1
        with pytest.raises(DBAPIError, match="permission denied"):
            await conn.execute(
                text("UPDATE inbound_addresses SET token_hash = :h"), {"h": "0" * 64}
            )
        await conn.rollback()
    async with owner_engine.begin() as conn:
        await _in_org(conn, org)
        await conn.execute(text("UPDATE organisations SET status = 'suspended'"))
    try:
        async with app_engine.begin() as conn:
            assert await conn.scalar(lookup, {"w": "pay", "s": slug, "h": token_hash}) is None
    finally:
        async with owner_engine.begin() as conn:
            await _in_org(conn, org)
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
