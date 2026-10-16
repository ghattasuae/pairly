# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""API keys over HTTP on real Postgres: create, scopes, use, revoke, expiry, sandbox,
rate limit, tenant isolation, audit attribution and ``Idempotency-Key`` on uploads.

Each test makes its own organisations (API keys cannot be deleted, so nothing is torn
down; the organisations are unique and never reused).
"""

from __future__ import annotations

import dataclasses
import io
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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_api.main import create_app
from pairly_api.uploads import DocumentServices
from pairly_core.config import Environment
from pairly_core.ingestion.queue import RecordingJobQueue
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_pay.plugin import plugin as pay_plugin

from .conftest import DbUrls, TokenFactory, make_settings
from .policy_support import Org

OTP = {"amr": ["otp"]}
READ = "core.document.read"
UPLOAD = "core.document.upload"


def pdf_bytes(label: str) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.drawString(72, 720, f"Synthetic invoice {label}")
    pdf.save()
    return buf.getvalue()


@dataclasses.dataclass
class Api:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory

    def h(self, subject: str, org: uuid.UUID, *, otp: bool = True) -> dict[str, str]:
        claims = OTP if otp else {}
        return {
            "Authorization": f"Bearer {self.tokens.token(subject, **claims)}",
            "X-Pairly-Org": str(org),
        }

    async def create_key(self, org: Org, **body: Any) -> httpx.Response:
        payload = {"name": "ci", "scopes": [READ, UPLOAD]} | body
        return await self.client.post(
            "/v1/api-keys", json=payload, headers=self.h(org.tenant.subject, org.id)
        )

    async def new_key(self, org: Org, **body: Any) -> tuple[str, dict[str, Any]]:
        created = await self.create_key(org, **body)
        assert created.status_code == 201, created.text
        data = created.json()
        return data["key"], data

    async def upload(
        self, key: str, data: bytes, *, idem: str | None = None, **headers: str
    ) -> httpx.Response:
        h = {"Authorization": f"Bearer {key}"} | headers
        if idem is not None:
            h["Idempotency-Key"] = idem
        return await self.client.post(
            "/v1/documents?workflow=pay",
            files={"file": ("invoice.pdf", data, "application/pdf")},
            headers=h,
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
        settings=settings,
        store=InMemoryObjectStore(),
        queue=RecordingJobQueue(),
        tmp_dir=tmp_path,
    )
    app = create_app(make_settings(migrated.app), plugins=[pay_plugin], documents=services)
    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield Api(app, client, tokens)


OrgFactory = Callable[[str], Awaitable[Org]]


@pytest.fixture
async def org(make_org: OrgFactory) -> Org:
    return await make_org("admin")


async def add_member_role(owner_engine: AsyncEngine, org: uuid.UUID, role: str) -> str:
    """Add a person with a built-in role to ``org``. Returns their token subject."""
    subject = f"sub-key-{uuid.uuid4().hex[:10]}"
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


async def owner_rows(owner_engine: AsyncEngine, org: uuid.UUID, sql: str, **kw: Any) -> list[Any]:
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)})
        return list((await conn.execute(text(sql), kw)).all())


async def owner_exec(owner_engine: AsyncEngine, org: uuid.UUID, sql: str, **kw: Any) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)})
        await conn.execute(text(sql), kw)


# ------------------------------------------------------------------ management


async def test_create_list_and_revoke(api: Api, org: Org, owner_engine: AsyncEngine) -> None:
    admin = org.tenant.subject
    no_otp = await api.client.post(
        "/v1/api-keys",
        json={"name": "ci", "scopes": [READ]},
        headers=api.h(admin, org.id, otp=False),
    )
    assert no_otp.status_code == 403
    assert no_otp.json()["code"] == "second_factor_required"

    key, created = await api.new_key(org, name="  ci  ", expires_in_days=30)
    assert created["name"] == "ci"
    assert created["prefix"].startswith("pk_live_")
    assert key.startswith(created["prefix"] + "_")
    assert created["scopes"] == sorted([READ, UPLOAD])

    listed = await api.client.get("/v1/api-keys", headers=api.h(admin, org.id))
    assert listed.status_code == 200
    (row,) = listed.json()
    assert "key" not in row
    assert key not in listed.text
    # The database holds only the prefix and an HMAC, never the key.
    ((stored_hash,),) = await owner_rows(
        owner_engine, org.id, "SELECT secret_hash FROM api_keys WHERE id = :i", i=created["id"]
    )
    assert len(stored_hash) == 64
    assert key[len(created["prefix"]) + 1 :] not in stored_hash

    assert (await api.upload(key, pdf_bytes("before revoke"))).status_code == 201
    revoked = await api.client.post(
        f"/v1/api-keys/{created['id']}/revoke", headers=api.h(admin, org.id)
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    refused = await api.upload(key, pdf_bytes("after revoke"))
    assert refused.status_code == 401
    assert refused.json()["code"] == "api_key_revoked"
    # Revoking twice is harmless; un-revoking is impossible even for the owner role.
    again = await api.client.post(
        f"/v1/api-keys/{created['id']}/revoke", headers=api.h(admin, org.id)
    )
    assert again.status_code == 200
    with pytest.raises(DBAPIError, match="stays revoked"):
        await owner_exec(
            owner_engine,
            org.id,
            "UPDATE api_keys SET revoked_at = NULL, revoked_by = NULL WHERE id = :i",
            i=created["id"],
        )
    with pytest.raises(DBAPIError, match="cannot be deleted"):
        await owner_exec(
            owner_engine, org.id, "DELETE FROM api_keys WHERE id = :i", i=created["id"]
        )

    actions = [
        r[0]
        for r in await owner_rows(
            owner_engine,
            org.id,
            "SELECT action FROM audit_events WHERE entity_type = 'api_key' ORDER BY seq",
        )
    ]
    assert actions == ["core.api_key.created", "core.api_key.revoked"]


async def test_scope_rules(api: Api, org: Org, make_org: OrgFactory) -> None:
    final = await api.create_key(org, scopes=["core.api_keys.manage"])
    assert final.status_code == 400
    assert "only read, propose and upload" in final.json()["detail"]
    unknown = await api.create_key(org, scopes=["pay.nothing.read"])
    assert unknown.status_code == 400
    too_long = await api.create_key(org, expires_in_days=4000)
    assert too_long.status_code == 422
    over_max = await api.create_key(org, expires_in_days=400)
    assert over_max.status_code == 400
    # Only admins manage keys: an approver is refused outright.
    approver = await make_org("approver")
    denied = await api.create_key(approver)
    assert denied.status_code == 403


async def test_key_permissions_are_its_scopes_only(api: Api, org: Org) -> None:
    read_only, _ = await api.new_key(org, scopes=[READ])
    refused = await api.upload(read_only, pdf_bytes("read only"))
    assert refused.status_code == 403
    key, _ = await api.new_key(org)
    # A key never manages keys or webhooks, even with an admin creator.
    for path in ("/v1/api-keys", "/v1/webhooks/endpoints"):
        response = await api.client.get(path, headers={"Authorization": f"Bearer {key}"})
        assert response.status_code == 403, path


async def test_use_audit_and_tenant_isolation(
    api: Api, org: Org, owner_engine: AsyncEngine, make_org: OrgFactory
) -> None:
    # Pay documents are read with Pay's own permission (pay.document.read); uploads to
    # /v1/documents stay on core.document.upload.
    key, created = await api.new_key(org, scopes=[READ, UPLOAD, "pay.document.read"])
    uploaded = await api.upload(key, pdf_bytes("isolation"))
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["document_id"]
    read = await api.client.get(
        f"/v1/documents/{document_id}", headers={"Authorization": f"Bearer {key}"}
    )
    assert read.status_code == 200
    (row,) = await owner_rows(
        owner_engine,
        org.id,
        "SELECT actor_type, actor_id FROM audit_events "
        "WHERE action = 'core.document.received' AND entity_id = :d",
        d=document_id,
    )
    assert tuple(row) == ("api_key", created["id"])
    ((submitted_by, last_used),) = await owner_rows(
        owner_engine,
        org.id,
        "SELECT d.submitted_by, k.last_used_at FROM documents d, api_keys k "
        "WHERE d.id = :d AND k.id = :k",
        d=document_id,
        k=created["id"],
    )
    assert submitted_by is None
    assert last_used is not None

    other = await make_org("admin")
    other_key, _ = await api.new_key(other)
    hidden = await api.client.get(
        f"/v1/documents/{document_id}", headers={"Authorization": f"Bearer {other_key}"}
    )
    assert hidden.status_code == 404
    # A key bound to one organisation cannot be pointed at another.
    crossed = await api.upload(key, pdf_bytes("crossed"), **{"X-Pairly-Org": str(other.id)})
    assert crossed.status_code == 403
    same = await api.upload(key, pdf_bytes("same org"), **{"X-Pairly-Org": str(org.id)})
    assert same.status_code == 201


async def test_invalid_and_expired_keys(api: Api, org: Org, owner_engine: AsyncEngine) -> None:
    key, created = await api.new_key(org)
    prefix, secret = key[:16], key[17:]
    wrong_secret = prefix + "_" + ("A" if secret[0] != "A" else "B") + secret[1:]
    for token in ("pk_live_nothing", wrong_secret, "pk_live_zzzzzzzz_" + secret):
        response = await api.upload(token, pdf_bytes("bad key"))
        assert response.status_code == 401, token
        assert response.json()["code"] == "invalid_api_key"
        assert response.headers["www-authenticate"].startswith("Bearer")
    # A wrong secret with a foreign org header is still 401: the prefix leaks nothing.
    foreign = await api.upload(
        wrong_secret, pdf_bytes("bad key"), **{"X-Pairly-Org": str(uuid.uuid4())}
    )
    assert foreign.status_code == 401

    await owner_exec(
        owner_engine,
        org.id,
        "UPDATE api_keys SET created_at = now() - interval '2 days', "
        "expires_at = now() - interval '1 second' WHERE id = :i",
        i=created["id"],
    )
    expired = await api.upload(key, pdf_bytes("expired"))
    assert expired.status_code == 401
    assert expired.json()["code"] == "api_key_expired"


async def test_test_keys_need_a_sandbox(
    api: Api, org: Org, owner_engine: AsyncEngine, make_org: OrgFactory
) -> None:
    refused = await api.create_key(org, mode="test")
    assert refused.status_code == 403
    assert refused.json()["code"] == "test_key_not_allowed"

    sandbox = await make_org("admin")
    await owner_exec(
        owner_engine,
        sandbox.id,
        "UPDATE organisations SET sandbox = true WHERE id = :o",
        o=sandbox.id,
    )
    # /v1/me tells the web app, which then offers test keys only (the API still checks).
    me = await api.client.get("/v1/me", headers=api.h(sandbox.tenant.subject, sandbox.id))
    assert me.status_code == 200
    assert me.json()["org"]["sandbox"] is True
    other = await api.client.get("/v1/me", headers=api.h(org.tenant.subject, org.id))
    assert other.json()["org"]["sandbox"] is False
    key, _ = await api.new_key(sandbox, mode="test")
    assert key.startswith("pk_test_")
    assert (await api.upload(key, pdf_bytes("sandbox"))).status_code == 201
    # The organisation leaves the sandbox: its test keys stop working.
    await owner_exec(
        owner_engine,
        sandbox.id,
        "UPDATE organisations SET sandbox = false WHERE id = :o",
        o=sandbox.id,
    )
    stopped = await api.upload(key, pdf_bytes("sandbox gone"))
    assert stopped.status_code == 403
    assert stopped.json()["code"] == "test_key_not_allowed"


async def test_app_role_cannot_flip_sandbox(app_engine: AsyncEngine, org: Org) -> None:
    async with app_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.id)})
        with pytest.raises(DBAPIError, match="permission denied"):
            await conn.execute(text("UPDATE organisations SET sandbox = true"))


async def test_rate_limit(api: Api, org: Org) -> None:
    key, _ = await api.new_key(org, rate_limit_per_minute=2)
    headers = {"Authorization": f"Bearer {key}"}
    missing = f"/v1/documents/{uuid.uuid4()}"
    first = await api.client.get(missing, headers=headers)
    assert first.status_code == 404, first.text
    assert (await api.client.get(missing, headers=headers)).status_code == 404
    limited = await api.client.get(missing, headers=headers)
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) >= 1


# ------------------------------------------------------------------ Idempotency-Key


async def test_idempotent_upload_replays(api: Api, org: Org, owner_engine: AsyncEngine) -> None:
    key, _ = await api.new_key(org)
    data = pdf_bytes("idempotent")
    first = await api.upload(key, data, idem="upload-0001")
    assert first.status_code == 201, first.text
    assert "idempotent-replayed" not in first.headers
    second = await api.upload(key, data, idem="upload-0001")
    assert second.status_code == 201
    assert second.headers["idempotent-replayed"] == "true"
    assert second.json() == first.json()
    # Same key, different request: refused, nothing created.
    reused = await api.upload(key, pdf_bytes("something else"), idem="upload-0001")
    assert reused.status_code == 409
    assert reused.json()["code"] == "idempotency_key_reused"
    bad = await api.upload(key, data, idem="no spaces allowed")
    assert bad.status_code == 400
    ((documents,),) = await owner_rows(
        owner_engine, org.id, "SELECT count(*) FROM documents WHERE org_id = :o", o=org.id
    )
    assert documents == 1
    # Keys are per actor: another key may use the same Idempotency-Key value.
    other, _ = await api.new_key(org)
    fresh = await api.upload(other, pdf_bytes("other actor"), idem="upload-0001")
    assert fresh.status_code == 201
    assert "idempotent-replayed" not in fresh.headers
    # The stored response body is encrypted at rest.
    rows = await owner_rows(
        owner_engine,
        org.id,
        "SELECT response_status, response_body FROM idempotency_keys WHERE key = 'upload-0001'",
    )
    assert len(rows) == 2
    for status, body in rows:
        assert status == 201
        assert first.json()["document_id"] not in body
