# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The Python SDK (``pairly``) against the real API in process, on real Postgres.

``documents.submit`` makes a Pay invoice and returns the job; ``approvals.list_mine``
answers from a personal key; ``proposals.create`` proposes; the ``batch.completed``
event in the outbox, wrapped and signed as the worker sends it, verifies with
``pairly.webhooks.construct_event``; and the SDK raises typed errors for refusals.
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
from pydantic import SecretStr
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import pairly
from pairly.webhooks import construct_event
from pairly_api.devseed import seed_approval
from pairly_api.main import create_app
from pairly_api.uploads import DocumentServices
from pairly_core.config import Environment
from pairly_core.ingestion.queue import RecordingJobQueue
from pairly_core.ingestion.settings import IngestionSettings
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_core.webhooks.events import envelope, serialise
from pairly_core.webhooks.signature import sign_payload
from pairly_pay.plugin import plugin as pay_plugin

from .conftest import DbUrls, TokenFactory, make_settings
from .policy_support import Org, new_org

OTP = {"amr": ["otp"]}
SCOPES = [
    "core.approval.read",
    "core.proposal.propose",
    "pay.invoice.read",
    "pay.invoice.upload",
]


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

    async def key(self, org: Org, scopes: list[str]) -> str:
        created = await self.client.post(
            "/v1/api-keys",
            json={"name": "sdk", "scopes": scopes},
            headers={
                "Authorization": f"Bearer {self.tokens.token(org.tenant.subject, **OTP)}",
                "X-Pairly-Org": str(org.id),
            },
        )
        assert created.status_code == 201, created.text
        return str(created.json()["key"])

    def sdk(self, key: str) -> pairly.AsyncPairly:
        return pairly.AsyncPairly(
            api_key=key,
            base_url="http://api.test",
            allow_http=True,
            transport=httpx.ASGITransport(app=self.app),
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
    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield Api(app, client, tokens)


@pytest.fixture
async def org(owner_engine: AsyncEngine) -> AsyncIterator[Org]:
    made = await new_org(owner_engine, "admin")
    yield made
    # Settle its documents so the stalled-document sweeper in other tests skips them.
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(made.id)})
        await conn.execute(
            text(
                "UPDATE documents SET status = 'rejected', status_reason = 'test_teardown' "
                "WHERE org_id = :o AND status IN ('received', 'scanning', 'clean')"
            ),
            {"o": made.id},
        )


async def _slug_and_email(owner_engine: AsyncEngine, org: Org) -> tuple[str, str]:
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.id)})
        slug = await conn.scalar(
            text("SELECT slug FROM organisations WHERE id = :o"), {"o": org.id}
        )
        email = await conn.scalar(
            text("SELECT email FROM users WHERE id = :u"), {"u": org.tenant.user_id}
        )
    return str(slug), str(email)


async def test_submit_list_mine_propose_and_verify_the_webhook(
    api: Api, org: Org, owner_engine: AsyncEngine, migrated: DbUrls
) -> None:
    slug, email = await _slug_and_email(owner_engine, org)
    seeded = await seed_approval(
        migrated.owner,
        org_slug=slug,
        approver_email=email,
        title="Invoice INV-SDK-1",
        overdue=False,
        link=None,
        decision=False,
    )
    async with api.sdk(await api.key(org, SCOPES)) as client:
        job = await client.documents.submit(
            pdf_bytes(f"sdk-{uuid.uuid4()}"),
            workflow="pay",
            filename="inv.pdf",
            metadata={"order": "PO-1"},
        )
        assert job.status == "accepted"
        assert job.entity_type == "pay.invoice"
        assert job.batch["status"] == "completed"
        again = await client.documents.get_batch(job.job_id)
        assert again["items"][0]["entity_id"] == job.entity_id

        mine = await client.approvals.list_mine()
        assert [a["id"] for a in mine] == [seeded["approval_id"]]

        proposal = await client.proposals.create(
            "approval", str(seeded["approval_id"]), "Matches the PO.", action="approve"
        )
        assert proposal["kind"] == "action"
        assert proposal["workflow"] == "pay"

        # API keys never decide: the SDK reports the refusal as a typed error.
        with pytest.raises(pairly.PermissionDeniedError):
            await client.request(
                "POST",
                f"/v1/approvals/{seeded['approval_id']}/decision",
                json={"decision": "approve"},
            )
        with pytest.raises(pairly.NotFoundError):
            await client.documents.get_batch(uuid.uuid4())

    # The batch.completed event, as the worker would deliver it.
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.id)})
        row = (
            await conn.execute(
                text(
                    "SELECT id, event_type, created_at, data FROM webhook_outbox "
                    "WHERE org_id = :o AND event_type = 'batch.completed'"
                ),
                {"o": org.id},
            )
        ).one()
    body = serialise(
        envelope(
            event_id=row.id,
            event_type=row.event_type,
            created_at=row.created_at,
            org_id=org.id,
            data=row.data,
        )
    )
    secret = "whsec_" + "s" * 40
    event = construct_event(body, sign_payload(secret, body), secret)
    completed: Any = event.batch_completed()
    assert completed["batch_id"] == job.job_id
    assert completed["metadata"] == {"order": "PO-1"}
    assert completed["items"][0]["entity_id"] == job.entity_id
