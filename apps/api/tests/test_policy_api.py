# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/policies`` over HTTP: rule sets, dry runs, policy documents, search, RBAC."""

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
from pydantic import SecretStr
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_api.main import create_app
from pairly_api.routes.documents import DocumentServices
from pairly_core.config import Environment
from pairly_core.ingestion.queue import SCAN_JOB, RecordingJobQueue
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import InMemoryObjectStore, derived_key
from pairly_core.retrieval.embeddings import HashingEmbedder

from .conftest import DbUrls, TokenFactory, make_settings
from .policy_support import SPEND_POLICY, Org, build_document, new_org
from .test_policy_engine import RULES_V1

OTP = {"amr": ["otp"]}


@dataclasses.dataclass
class Api:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory
    store: InMemoryObjectStore
    queue: RecordingJobQueue

    def h(self, subject: str, org: uuid.UUID, *, otp: bool = True) -> dict[str, str]:
        claims = OTP if otp else {}
        return {
            "Authorization": f"Bearer {self.tokens.token(subject, **claims)}",
            "X-Pairly-Org": str(org),
        }


@pytest.fixture
async def api(migrated: DbUrls, tokens: TokenFactory, tmp_path: Path) -> AsyncIterator[Api]:
    store, queue = InMemoryObjectStore(), RecordingJobQueue()
    settings = IngestionSettings(
        env=Environment.TEST,
        s3_bucket="test",
        s3_access_key=SecretStr("test"),
        s3_secret_key=SecretStr("test-secret"),
        inbound_email_domain="inbound.pairly.localhost",
    )
    services = DocumentServices(settings=settings, store=store, queue=queue, tmp_dir=tmp_path)
    app = create_app(make_settings(migrated.app), plugins=[], documents=services)
    app.state.policy_embedder = HashingEmbedder()
    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield Api(app, client, tokens, store, queue)


async def add_member(owner_engine: AsyncEngine, org: uuid.UUID, role: str) -> str:
    subject = f"sub-pol-{uuid.uuid4().hex[:10]}"
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "SELECT set_config('app.org_id', :o, true), "
                "set_config('app.auth_subject', :s, true)"
            ),
            {"o": str(org), "s": subject},
        )
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


async def owner_scalar(owner_engine: AsyncEngine, org: uuid.UUID, sql: str, **kw: Any) -> Any:
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)})
        return (await conn.execute(text(sql), kw)).scalar()


@pytest.fixture
async def org(owner_engine: AsyncEngine) -> Org:
    return await new_org(owner_engine, "admin")


# ------------------------------------------------------------------ rule sets


async def test_rule_set_publish_read_and_rbac(
    api: Api, org: Org, owner_engine: AsyncEngine
) -> None:
    admin = org.tenant.subject
    approver = await add_member(owner_engine, org.id, "approver")
    url = "/v1/policies/rule-sets/pay.spend/versions"

    no_otp = await api.client.post(
        url, json={"content": RULES_V1}, headers=api.h(admin, org.id, otp=False)
    )
    assert no_otp.status_code == 403, no_otp.text
    assert no_otp.json()["code"] == "second_factor_required"
    denied = await api.client.post(url, json={"content": RULES_V1}, headers=api.h(approver, org.id))
    assert denied.status_code == 403

    created = await api.client.post(url, json={"content": RULES_V1}, headers=api.h(admin, org.id))
    assert created.status_code == 201, created.text
    assert created.json()["created"] is True
    assert created.json()["version"]["version"] == 1
    same = await api.client.post(url, json={"content": RULES_V1}, headers=api.h(admin, org.id))
    assert same.status_code == 200
    assert same.json()["created"] is False

    invalid = await api.client.post(
        url,
        json={"content": RULES_V1.replace("amount > 10000", "__import__('os')")},
        headers=api.h(admin, org.id),
    )
    assert invalid.status_code == 400
    assert "unexpected character" in invalid.json()["detail"]
    wrong_name = await api.client.post(
        "/v1/policies/rule-sets/pay.other/versions",
        json={"content": RULES_V1},
        headers=api.h(admin, org.id),
    )
    assert wrong_name.status_code == 400

    latest = await api.client.get(
        "/v1/policies/rule-sets/pay.spend", headers=api.h(approver, org.id)
    )
    assert latest.status_code == 200
    assert latest.json()["content"] == RULES_V1
    versions = await api.client.get(
        "/v1/policies/rule-sets/pay.spend/versions", headers=api.h(approver, org.id)
    )
    assert [v["version"] for v in versions.json()] == [1]
    missing = await api.client.get(
        "/v1/policies/rule-sets/pay.spend?version=9", headers=api.h(approver, org.id)
    )
    assert missing.status_code == 404
    bad = await api.client.get("/v1/policies/rule-sets/Bad Name", headers=api.h(admin, org.id))
    assert bad.status_code == 422

    other = await new_org(owner_engine)
    hidden = await api.client.get(
        "/v1/policies/rule-sets/pay.spend", headers=api.h(other.tenant.subject, other.id)
    )
    assert hidden.status_code == 404


async def test_dry_run_stores_nothing(api: Api, org: Org, owner_engine: AsyncEngine) -> None:
    admin = org.tenant.subject
    approver = await add_member(owner_engine, org.id, "approver")
    await api.client.post(
        "/v1/policies/rule-sets/pay.spend/versions",
        json={"content": RULES_V1},
        headers=api.h(admin, org.id),
    )
    events_before = await owner_scalar(owner_engine, org.id, "SELECT count(*) FROM audit_events")

    published = await api.client.post(
        "/v1/policies/rule-sets/dry-run",
        json={"name": "pay.spend", "facts": {"amount": "15000", "category": "software"}},
        headers=api.h(approver, org.id, otp=False),
    )
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["overall"] == "needs_review"
    assert body["requires_human"] is True
    assert body["version"] == 1
    by_id = {r["rule_id"]: r for r in body["results"]}
    assert (
        by_id["software_it_approval"]["reason"]
        == "Software purchase of 15000 AED needs IT approval"
    )

    draft = await api.client.post(
        "/v1/policies/rule-sets/dry-run",
        json={
            "content": RULES_V1.replace("amount > 10000", "amount > 20000"),
            "facts": {"category": "software"},
            "as_of": "2026-10-07",
        },
        headers=api.h(approver, org.id, otp=False),
    )
    assert draft.status_code == 200
    assert draft.json()["overall"] == "error"
    assert {r["error_code"] for r in draft.json()["results"]} == {"missing_fact"}

    for payload, status in (
        ({"facts": {}}, 400),
        ({"name": "pay.spend", "content": RULES_V1, "facts": {}}, 400),
        ({"name": "pay.none", "facts": {}}, 404),
        ({"content": "rules: [", "facts": {}}, 400),
        ({"content": RULES_V1.replace("amount > 10000", "eval('1')"), "facts": {}}, 400),
    ):
        response = await api.client.post(
            "/v1/policies/rule-sets/dry-run", json=payload, headers=api.h(approver, org.id)
        )
        assert response.status_code == status, (payload, response.text)

    assert await owner_scalar(owner_engine, org.id, "SELECT count(*) FROM rule_results") == 0
    events_after = await owner_scalar(owner_engine, org.id, "SELECT count(*) FROM audit_events")
    assert events_after == events_before
    results = await api.client.get(
        f"/v1/policies/rule-results?evaluation_id={uuid.uuid4()}", headers=api.h(approver, org.id)
    )
    assert results.status_code == 200
    assert results.json() == []


# ------------------------------------------------------------------ documents


def pdf_bytes(label: str) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.drawString(72, 720, f"Synthetic spend policy {label}")
    pdf.save()
    return buf.getvalue()


async def _upload(api: Api, subject: str, org: uuid.UUID, data: bytes, **kw: Any) -> httpx.Response:
    name = kw.get("name", "spend_policy")
    return await api.client.post(
        f"/v1/policies/documents?name={name}&title=Spend%20policy",
        files={"file": (kw.get("filename", "policy.pdf"), data, "application/pdf")},
        headers=api.h(subject, org, otp=kw.get("otp", True)),
    )


async def _mark_parsed(
    api: Api, owner_engine: AsyncEngine, org: uuid.UUID, document: dict[str, Any]
) -> None:
    document_id = document["id"]
    key = derived_key(org, document["sha256"], "normalized-synthetic-1.json")
    await api.store.put_bytes(
        key,
        build_document(SPEND_POLICY).model_dump_json().encode(),
        content_type="application/json",
    )
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)})
        await conn.execute(
            text(
                "UPDATE documents SET status = 'parsed', parse_result_key = :k, "
                "parsed_at = now() WHERE id = :i"
            ),
            {"k": key, "i": document_id},
        )


async def test_policy_document_lifecycle(api: Api, org: Org, owner_engine: AsyncEngine) -> None:
    admin = org.tenant.subject
    approver = await add_member(owner_engine, org.id, "approver")

    assert (await _upload(api, admin, org.id, pdf_bytes("x"), otp=False)).status_code == 403
    assert (await _upload(api, approver, org.id, pdf_bytes("x"))).status_code == 403
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    assert (await _upload(api, admin, org.id, png, filename="p.png")).status_code == 415

    data = pdf_bytes("v1")
    uploaded = await _upload(api, admin, org.id, data)
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    pdoc = body["policy_document"]
    assert (pdoc["name"], pdoc["version"], pdoc["status"]) == ("spend_policy", 1, "pending")
    assert body["document"]["workflow"] == "policy"
    assert [j[0] for j in api.queue.jobs] == [SCAN_JOB]

    again = await _upload(api, admin, org.id, data)
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["policy_document"]["id"] == pdoc["id"]

    index_url = f"/v1/policies/documents/{pdoc['id']}/index"
    early = await api.client.post(index_url, headers=api.h(admin, org.id))
    assert early.status_code == 409

    await _mark_parsed(api, owner_engine, org.id, body["document"])
    indexed = await api.client.post(index_url, headers=api.h(admin, org.id))
    assert indexed.status_code == 200, indexed.text
    assert indexed.json()["status"] == "indexed"
    assert indexed.json()["chunks"] >= 4
    assert indexed.json()["policy_document"]["embedding_model"] == "pairly-hashing"

    search = await api.client.post(
        "/v1/policies/search",
        json={"query": "software purchase IT approval"},
        headers=api.h(approver, org.id, otp=False),
    )
    assert search.status_code == 200, search.text
    top = search.json()[0]
    assert top["section"] == "1 Purchasing > 1.1 Software"
    assert (top["name"], top["version"], top["page_start"]) == ("spend_policy", 1, 1)

    listed = await api.client.get("/v1/policies/documents", headers=api.h(approver, org.id))
    assert [d["id"] for d in listed.json()] == [pdoc["id"]]
    one = await api.client.get(
        f"/v1/policies/documents/{pdoc['id']}", headers=api.h(approver, org.id)
    )
    assert one.json()["status"] == "indexed"

    other = await new_org(owner_engine)
    for method, path in (
        ("get", f"/v1/policies/documents/{pdoc['id']}"),
        ("post", f"/v1/policies/documents/{pdoc['id']}/archive"),
        ("post", index_url),
    ):
        response = await api.client.request(
            method, path, headers=api.h(other.tenant.subject, other.id)
        )
        assert response.status_code == 404, path
    foreign = await api.client.post(
        "/v1/policies/search",
        json={"query": "software purchase IT approval"},
        headers=api.h(other.tenant.subject, other.id),
    )
    assert foreign.json() == []

    archived = await api.client.post(
        f"/v1/policies/documents/{pdoc['id']}/archive", headers=api.h(admin, org.id)
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    after = await api.client.post(
        "/v1/policies/search",
        json={"query": "software purchase IT approval"},
        headers=api.h(approver, org.id),
    )
    assert after.json() == []

    actions = await owner_scalar(
        owner_engine,
        org.id,
        "SELECT string_agg(action, ',' ORDER BY seq) FROM audit_events",
    )
    for action in (
        "core.document.received",
        "core.policy.document_registered",
        "core.policy.document_indexed",
        "core.policy.document_archived",
    ):
        assert action in actions


async def test_policy_document_parse_failure_is_not_indexed(
    api: Api, org: Org, owner_engine: AsyncEngine
) -> None:
    admin = org.tenant.subject
    body = (await _upload(api, admin, org.id, pdf_bytes("broken"))).json()
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.id)})
        await conn.execute(
            text("UPDATE documents SET status = 'parse_failed', needs_review = true WHERE id = :i"),
            {"i": body["document"]["id"]},
        )
    response = await api.client.post(
        f"/v1/policies/documents/{body['policy_document']['id']}/index",
        headers=api.h(admin, org.id),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["reason"] == "document_parse_failed"
