# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Support for the Pay approval and payment run tests (WP4.7) on Postgres, offline.

* ``Flow`` / ``flow``: the API with the recorded extraction of ``test_pay_invoices``,
  plus the approval notification hook switched on (email links and Slack buttons are
  issued), a fake Slack transport and the notification test settings.
* ``FlowWorld`` / ``fworld``, ``fother``: an organisation with two of every Pay role
  that decides (two approvers, two finance controllers), so chains and segregation of
  duties have people.
* ``ready_invoice``: a recorded synthetic invoice read to ``ready`` (a person reviews
  it when the checks asked for one), with its vendor created first.
* ``approve_all``: decide every step of an approval with the first eligible person.
* ``seed_po``: the synthetic purchase order an invoice cites, with its goods receipts.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pairly_api.main import create_app
from pairly_api.uploads import DocumentServices
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.documents.pipeline import ParseConfig
from pairly_core.documents.registry import ParseOptions
from pairly_core.documents.sandbox import SandboxLimits
from pairly_core.ingestion.queue import RecordingJobQueue
from pairly_core.ingestion.scan import FakeScanner
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.llm.factory import build_router
from pairly_core.llm.providers.fake import FakeProvider
from pairly_core.llm.settings import ModelMode, load_llm_settings
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_core.workflow.services import GraphServices
from pairly_pay.recordings import import_pay_recordings

from .conftest import DbUrls, TokenFactory, make_settings
from .hire_support import ingestion_settings
from .notify_support import approval_hooks, notify_settings
from .pay_support import PLUGINS, PayWorld, new_vendor, owner_rows, seed_pay_world, vendor_body
from .test_pay_invoices import MEDIA, blob, dataset, vendor_index

FLOW_PEOPLE: dict[str, tuple[tuple[str, str | None], ...]] = {
    "clerk": (("ap_clerk", "pay"),),
    "clerk2": (("ap_clerk", "pay"),),
    "approver": (("approver", "pay"),),
    "approver2": (("approver", None),),
    "finance": (("finance_controller", None),),
    "finance2": (("finance_controller", None),),
    "admin": (("admin", None),),
    "auditor": (("auditor", None),),
}
DECIDERS = ("approver", "approver2", "finance", "finance2")
# Recorded regular invoices in AED from different vendors (synth/pay recordings).
AED_DOCS = ("inv-0001", "inv-0030", "inv-0037", "inv-0055", "inv-0073")
USD_DOC = "inv-0014"


@dataclasses.dataclass
class Flow:
    app: Any
    client: httpx.AsyncClient
    tokens: TokenFactory
    engine: AsyncEngine
    store: InMemoryObjectStore

    @property
    def runtime(self) -> WorkflowRuntime:
        runtime = self.app.state.workflow_runtime
        assert isinstance(runtime, WorkflowRuntime)
        return runtime

    def headers(
        self, world: PayWorld, name: str, *, otp: bool = True, org: bool = True
    ) -> dict[str, str]:
        claims: dict[str, Any] = {"amr": ["otp"]} if otp else {}
        token = self.tokens.token(world.people[name][1], **claims)
        out = {"Authorization": f"Bearer {token}"}
        if org:
            out["X-Pairly-Org"] = str(world.org_id)
        return out

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

    async def invoice(self, world: PayWorld, invoice_id: str, name: str = "clerk") -> Any:
        return await self.ok("GET", f"/v1/pay/invoices/{invoice_id}", world, name)

    async def decide(
        self,
        world: PayWorld,
        name: str,
        approval_id: str,
        decision: str = "approve",
        reason: str = "Checked the invoice, the findings and the risk.",
        *,
        otp: bool = True,
    ) -> httpx.Response:
        headers = self.headers(world, name, otp=otp) | {"Idempotency-Key": f"k-{uuid.uuid4().hex}"}
        return await self.client.post(
            f"/v1/approvals/{approval_id}/decision",
            json={"decision": decision, "reason": reason},
            headers=headers,
        )


@pytest.fixture
async def flow(migrated: DbUrls, tokens: TokenFactory, tmp_path: Path) -> AsyncIterator[Flow]:
    store = InMemoryObjectStore()
    documents = DocumentServices(
        settings=ingestion_settings(), store=store, queue=RecordingJobQueue(), tmp_dir=tmp_path
    )
    engine = create_async_engine(migrated.app, pool_size=4, max_overflow=4)
    router = build_router(
        load_llm_settings(model_mode=ModelMode.RECORDED),
        create_sessionmaker(engine),
        providers={"anthropic": FakeProvider("anthropic"), "openai": FakeProvider("openai")},
    )
    services = GraphServices(
        store=store,
        router=router,
        scanner=FakeScanner(),
        parse_config=ParseConfig(
            options=ParseOptions(ocr_languages="eng"),
            limits=SandboxLimits(wall_seconds=60, memory_bytes=768 * 1024 * 1024),
        ),
    )
    settings = notify_settings()
    app = create_app(
        make_settings(migrated.app),
        plugins=PLUGINS,
        documents=documents,
        approval_hooks=approval_hooks(settings),
        notification_settings=settings,
        slack_transport=FakeSlackTransport(),
        graph_services=services,
    )
    try:
        async with app.router.lifespan_context(app):
            app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
                yield Flow(app, c, tokens, engine, store)
    finally:
        await engine.dispose()


async def flow_world(owner: AsyncEngine) -> PayWorld:
    """An organisation with two of every deciding role and the Pay recordings."""
    world = await seed_pay_world(owner, FLOW_PEOPLE)
    session = create_sessionmaker(owner)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            await import_pay_recordings(session, world.org_id)
    return world


@pytest.fixture
async def fworld(owner_engine: AsyncEngine) -> PayWorld:
    return await flow_world(owner_engine)


@pytest.fixture
async def fother(owner_engine: AsyncEngine) -> PayWorld:
    return await flow_world(owner_engine)


async def make_vendor(
    flow: Flow,
    world: PayWorld,
    doc_id: str,
    *,
    age_days: int = 0,
    owner: AsyncEngine | None = None,
    **overrides: Any,
) -> str:
    truth = dataset().doc(doc_id).record.truth
    assert truth.vendor_id is not None
    created = await new_vendor(flow, world, vendor_body(vendor_index(truth.vendor_id), **overrides))
    if age_days and owner is not None:
        await owner_rows(
            owner,
            world.org_id,
            "UPDATE pay_vendors SET created_at = now() - make_interval(days => :d) WHERE id = :v",
            {"d": age_days, "v": created["id"]},
        )
    return str(created["id"])


async def upload(flow: Flow, world: PayWorld, doc_id: str) -> str:
    record = dataset().doc(doc_id).record
    response = await flow.client.post(
        "/v1/pay/invoices",
        files={"file": (Path(record.file).name, blob(doc_id), MEDIA[record.format])},
        headers=flow.headers(world, "clerk"),
    )
    assert response.status_code == 202, response.text
    return str(response.json()["id"])


async def ready_invoice(
    flow: Flow,
    world: PayWorld,
    doc_id: str,
    *,
    owner: AsyncEngine | None = None,
    age_days: int = 0,
    reviewer: str = "clerk2",
    with_po: bool = False,
) -> tuple[str, str]:
    """(invoice id, vendor id) of ``doc_id`` read to ``ready``; with ``with_po`` its
    purchase order and goods receipts are on file first, so it can be matched."""
    vendor_id = await make_vendor(flow, world, doc_id, age_days=age_days, owner=owner)
    if with_po:
        await seed_po(flow, world, doc_id, vendor_id)
    invoice_id = await upload(flow, world, doc_id)
    detail = await flow.invoice(world, invoice_id)
    if detail["status"] == "needs_review":
        detail = await flow.ok(
            "POST",
            f"/v1/pay/invoices/{invoice_id}/review",
            world,
            reviewer,
            json={"note": "Checked against the paper copy."},
        )
    assert detail["status"] == "ready", detail["review_reasons"]
    assert detail["checks_state"] == "current"
    return invoice_id, vendor_id


async def submit(flow: Flow, world: PayWorld, invoice_id: str, who: str = "clerk") -> Any:
    return await flow.ok(
        "POST", f"/v1/pay/invoices/{invoice_id}/submit", world, who, json={"note": "Please"}
    )


async def approval_row(owner: AsyncEngine, world: PayWorld, approval_id: str) -> Any:
    (row,) = await owner_rows(
        owner,
        world.org_id,
        "SELECT id, status, chain_name, payload, context, preparer_ids FROM approvals "
        "WHERE id = :a",
        {"a": approval_id},
    )
    return row


async def current_assignees(owner: AsyncEngine, world: PayWorld, approval_id: str) -> list[str]:
    rows = await owner_rows(
        owner,
        world.org_id,
        "SELECT s.assignee_ids FROM approval_steps s JOIN approvals a ON a.id = s.approval_id "
        "WHERE a.id = :a AND s.position = a.current_step",
        {"a": approval_id},
    )
    return [str(u) for u in rows[0].assignee_ids] if rows else []


async def approve_all(
    flow: Flow,
    owner: AsyncEngine,
    world: PayWorld,
    approval_id: str,
    people: Sequence[str] = DECIDERS,
) -> list[str]:
    """Approve every step with the first eligible person not used yet. Returns them."""
    used: list[str] = []
    for _ in range(5):
        row = await approval_row(owner, world, approval_id)
        if row.status != "pending":
            break
        assignees = await current_assignees(owner, world, approval_id)
        who = next(p for p in people if p not in used and str(world.id(p)) in assignees)
        response = await flow.decide(world, who, approval_id)
        assert response.status_code == 200, response.text
        used.append(who)
    return used


async def seed_po(
    flow: Flow, world: PayWorld, doc_id: str, vendor_id: str, *, receipts: bool = True
) -> str:
    """The synthetic PO ``doc_id`` cites (``synth/pay``), with its goods receipts, created
    through the API by the AP clerk. Returns the PO id."""
    ds = dataset()
    number = ds.doc(doc_id).record.truth.po_number
    (po,) = [p for p in ds.purchase_orders if p.po_number == number]
    created = await flow.ok(
        "POST",
        "/v1/pay/purchase-orders",
        world,
        "clerk",
        status=201,
        json={
            "po_number": po.po_number,
            "vendor_id": vendor_id,
            "currency": po.currency,
            "order_date": po.date,
            "match_type": po.match_type,
            "department": po.department,
            "requester": po.requester,
            "lines": [
                {
                    "line_no": x.line_no,
                    "description": x.description,
                    "quantity": x.quantity,
                    "unit_price": x.unit_price,
                }
                for x in po.lines
            ],
        },
    )
    for grn in (r for r in ds.receipts if r.po_number == number and receipts):
        await flow.ok(
            "POST",
            "/v1/pay/goods-receipts",
            world,
            "clerk",
            status=201,
            json={
                "grn_number": grn.grn_number,
                "po_id": created["id"],
                "received_date": grn.date,
                "received_by": grn.received_by,
                "lines": [
                    {"po_line_no": x.po_line_no, "quantity": x.quantity_received} for x in grn.lines
                ],
            },
        )
    return str(created["id"])
