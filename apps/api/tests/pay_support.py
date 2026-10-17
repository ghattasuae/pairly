# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Support for the Pairly Pay database tests (WP4.2 vendors, WP4.3 bank-detail changes).

* ``PayWorld``: an organisation with one member per Pay role (two AP clerks, so one can
  request and the other verify; two approvers plus a finance controller, so the dual
  approval has people), and helpers to call the API as each of them.
* ``pay_api``: the API with in-memory document storage and a recording job queue.
* ``vendor_body``: a vendor from the synthetic vendor master (``synth/pay``).
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pairly_api.main import create_app
from pairly_api.uploads import DocumentServices
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.ingestion.queue import RecordingJobQueue
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.plugins import WorkflowPlugin, discover_plugins
from pairly_core.workflow.services import GraphServices

from .conftest import DbUrls, TokenFactory, make_settings
from .hire_support import ingestion_settings
from .notify_support import notify_settings

PLUGINS: list[WorkflowPlugin] = list(discover_plugins().values())
VENDORS = Path(__file__).resolve().parents[3] / "synth" / "pay" / "manifests" / "vendors.json"

# name -> memberships (role, workflow).
PEOPLE: dict[str, tuple[tuple[str, str | None], ...]] = {
    "clerk": (("ap_clerk", "pay"),),
    "clerk2": (("ap_clerk", "pay"),),
    "approver": (("approver", "pay"),),
    "approver2": (("approver", None),),
    "finance": (("finance_controller", None),),
    "admin": (("admin", None),),
    "auditor": (("auditor", None),),
    "recruiter": (("recruiter", "hire"),),
}


@dataclasses.dataclass
class PayWorld:
    org_id: uuid.UUID
    slug: str
    name: str
    people: dict[str, tuple[uuid.UUID, str]] = dataclasses.field(default_factory=dict)

    def id(self, name: str) -> uuid.UUID:
        return self.people[name][0]


async def seed_pay_world(
    owner: AsyncEngine,
    people: dict[str, tuple[tuple[str, str | None], ...]] | None = None,
) -> PayWorld:
    org_id = uuid.uuid4()
    world = PayWorld(org_id=org_id, slug=f"pay-{org_id.hex[:12]}", name="Acme Trading")
    async with owner.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)})
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug) VALUES (:id, :n, :s)"),
            {"id": org_id, "n": world.name, "s": world.slug},
        )
        for name, memberships in (people or PEOPLE).items():
            user_id = uuid.uuid4()
            subject = f"sub-pay-{name}-{user_id.hex[:10]}"
            await conn.execute(
                text("SELECT set_config('app.auth_subject', :s, true)"), {"s": subject}
            )
            await conn.execute(
                text("INSERT INTO users (id, auth_subject, email) VALUES (:id, :s, :e)"),
                {"id": user_id, "s": subject, "e": f"{name}-{user_id.hex[:6]}@example.test"},
            )
            for role, workflow in memberships:
                await conn.execute(
                    text(
                        "INSERT INTO memberships (org_id, user_id, role_id, workflow) "
                        "SELECT :o, :u, r.id, :w FROM roles r "
                        "WHERE r.key = :role AND r.org_id IS NULL"
                    ),
                    {"o": org_id, "u": user_id, "w": workflow, "role": role},
                )
            world.people[name] = (user_id, subject)
    return world


async def owner_rows(
    owner: AsyncEngine, org_id: uuid.UUID, sql: str, params: dict[str, Any] | None = None
) -> list[Any]:
    async with owner.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)})
        result = await conn.execute(text(sql), params or {})
        return list(result.all()) if result.returns_rows else []


async def retire_documents(owner: AsyncEngine, org_id: uuid.UUID) -> None:
    """Park this organisation's documents, so the stalled-document sweeper other tests
    run over every organisation never picks them up (nothing scans them here)."""
    await owner_rows(
        owner,
        org_id,
        "UPDATE documents SET status = 'parse_failed', updated_at = now() "
        "WHERE org_id = :o AND status IN ('received', 'scanning', 'clean')",
        {"o": org_id},
    )


@dataclasses.dataclass
class PayApi:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory
    engine: AsyncEngine
    store: InMemoryObjectStore
    queue: RecordingJobQueue

    def headers(
        self, world: PayWorld, name: str, *, otp: bool = True, extra: dict[str, str] | None = None
    ) -> dict[str, str]:
        claims: dict[str, Any] = {"amr": ["otp"]} if otp else {}
        token = self.tokens.token(world.people[name][1], **claims)
        return {"Authorization": f"Bearer {token}", "X-Pairly-Org": str(world.org_id)} | (
            extra or {}
        )

    async def call(
        self,
        method: str,
        path: str,
        world: PayWorld,
        name: str,
        *,
        otp: bool = True,
        json: Any = None,
    ) -> httpx.Response:
        return await self.client.request(
            method, path, json=json, headers=self.headers(world, name, otp=otp)
        )

    async def ok(
        self, method: str, path: str, world: PayWorld, name: str, *, status: int = 200, **kw: Any
    ) -> Any:
        response = await self.call(method, path, world, name, **kw)
        assert response.status_code == status, (path, response.status_code, response.text)
        return response.json()

    async def decide(
        self,
        world: PayWorld,
        name: str,
        approval_id: str,
        decision: str = "approve",
        reason: str = "Checked the call record and the documents.",
        *,
        otp: bool = True,
    ) -> httpx.Response:
        return await self.client.post(
            f"/v1/approvals/{approval_id}/decision",
            json={"decision": decision, "reason": reason},
            headers=self.headers(
                world, name, otp=otp, extra={"Idempotency-Key": f"k-{uuid.uuid4().hex}"}
            ),
        )

    async def in_org(self, world: PayWorld) -> Any:
        """A session of the application role bound to the organisation."""
        session = create_sessionmaker(self.engine)()
        set_tenant(session, world.org_id, None)
        return session


@pytest.fixture
async def pay_api(migrated: DbUrls, tokens: TokenFactory, tmp_path: Path) -> AsyncIterator[PayApi]:
    store = InMemoryObjectStore()
    queue = RecordingJobQueue()
    documents = DocumentServices(
        settings=ingestion_settings(), store=store, queue=queue, tmp_dir=tmp_path
    )
    engine = create_async_engine(migrated.app, pool_size=4, max_overflow=4)
    app = create_app(
        make_settings(migrated.app),
        plugins=PLUGINS,
        documents=documents,
        notification_settings=notify_settings(),
        slack_transport=FakeSlackTransport(),
        graph_services=GraphServices(store=store),
    )
    try:
        async with app.router.lifespan_context(app):
            app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
                yield PayApi(app, c, tokens, engine, store, queue)
    finally:
        await engine.dispose()


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> PayWorld:
    return await seed_pay_world(owner_engine)


@pytest.fixture
async def other(owner_engine: AsyncEngine) -> PayWorld:
    return await seed_pay_world(owner_engine)


def synth_vendors() -> list[dict[str, Any]]:
    data = json.loads(VENDORS.read_text(encoding="utf-8"))
    return [*data["vendors"], *data["unknown_vendors"]]


def vendor_body(index: int = 0, **overrides: Any) -> dict[str, Any]:
    """The API body for a synthetic vendor of ``synth/pay``."""
    v = synth_vendors()[index]
    body: dict[str, Any] = {
        "legal_name": v["legal_name"],
        "legal_name_ar": v["legal_name_ar"],
        "trade_name": v["trade_name"],
        "country": v["country"],
        "trade_licence": v["trade_licence"],
        "trn": v["trn"],
        "address": {k: val for k, val in v["address"].items() if val is not None},
        "contacts": [
            {"name": c["name"], "role": c["role"], "email": c["email"], "phone": c["phone"]}
            for c in v["contacts"]
        ],
        "phone_on_file": v["phone_on_file"],
        # Where the number on file comes from (security review 2026-10, M-03).
        "phone_source": {
            "source": "trade_licence",
            "reference": f"Trade licence {v['trade_licence']}",
        },
        "email_domains": list(v["email_domains"]),
        "category": v["category"],
        "bank": {
            "iban": v["bank"]["iban"],
            "account_name": v["bank"]["account_name"],
            "bank_name": v["bank"]["bank_name"],
            "swift": v["bank"]["bic"],
        },
    }
    body.update(overrides)
    return body


def other_bank(index: int = 5) -> dict[str, Any]:
    """Valid bank details of another synthetic vendor (a different IBAN)."""
    v = synth_vendors()[index]
    return {
        "iban": v["bank"]["iban"],
        "account_name": v["bank"]["account_name"],
        "bank_name": v["bank"]["bank_name"],
        "swift": v["bank"]["bic"],
    }


async def audit_actions(
    owner: AsyncEngine, org_id: uuid.UUID, entity_id: str | uuid.UUID
) -> list[str]:
    rows = await owner_rows(
        owner,
        org_id,
        "SELECT action FROM audit_events WHERE org_id = :o AND entity_id = :e ORDER BY seq",
        {"o": org_id, "e": str(entity_id)},
    )
    return [r.action for r in rows]


def has_all(haystack: Sequence[str], *needles: str) -> bool:
    return all(n in haystack for n in needles)


def iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def verify_new_vendor(
    api: Any, world: PayWorld, vendor: dict[str, Any], *, verifier: str = "finance"
) -> dict[str, Any]:
    """Make a new vendor's staff-entered bank details active the way the product does
    (security review 2026-10, M-03): the ticket that opened with the vendor is called
    back by someone who neither entered the vendor nor recorded its number, and approved
    by two people. No cooling-off (set on the ticket, so the world's settings stay).
    ``api`` is any test client with ``client``, ``headers(world, name)``, ``ok`` and
    ``engine``."""
    ticket_id = vendor.get("open_ticket_id")
    assert ticket_id, vendor
    session = create_sessionmaker(api.engine)()
    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        await session.execute(
            text("UPDATE pay_bank_change_tickets SET cooling_off_hours = 0 WHERE id = :t"),
            {"t": ticket_id},
        )
    called = await api.client.post(
        f"/v1/pay/bank-changes/{ticket_id}/calls",
        json={
            "result": "confirmed",
            "notes": "Called the number on file.",
            "called_number_on_file": True,
        },
        headers=api.headers(world, verifier),
    )
    assert called.status_code == 200, called.text
    approval_id = called.json()["approval_id"]
    assert approval_id, called.text
    for who in ("approver", "approver2"):
        decided = await api.client.post(
            f"/v1/approvals/{approval_id}/decision",
            json={"decision": "approve", "reason": "Checked the call record."},
            headers={**api.headers(world, who), "Idempotency-Key": f"k-{uuid.uuid4().hex}"},
        )
        assert decided.status_code == 200, decided.text
    done = await api.ok("GET", f"/v1/pay/vendors/{vendor['id']}", world, "clerk")
    assert done["payment_hold"] is False, done
    assert done["iban_masked"], done
    return dict(done)


async def new_vendor(api: Any, world: PayWorld, body: dict[str, Any]) -> dict[str, Any]:
    """A vendor the AP clerk creates with bank details, verified and payable."""
    created = await api.ok("POST", "/v1/pay/vendors", world, "clerk", status=201, json=body)
    return await verify_new_vendor(api, world, dict(created))
