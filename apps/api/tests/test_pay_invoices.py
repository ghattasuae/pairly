# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay invoice intake and extraction (WP4.4, WP4.5) on Postgres, offline.

The model router runs in ``recorded`` mode with the Pay recordings bundle (answers
scripted from the synthetic ground truth); its providers have no script, so a request
without a recording fails closed to a person. The scan uses the fake scanner and the
parse runs in the real sandbox.

* intake per source: upload (person), API (API key with the upload scope), email;
* file hash dedupe across sources;
* email cases: several attachments, a supporting file, an invoice in the body, a
  forwarded chain, marketing ignored, the receipt reply, a retried delivery;
* the ``pay_invoice`` graph: a recorded PDF to ``ready``, an e-invoice read exactly, a
  guardrail-flagged invoice and an unrecorded one to ``needs_review``;
* corrections filed per vendor and fed back as hints; vendor matching by TRN, IBAN,
  sender domain and name, and a conflict;
* the intake job, permissions and tenant isolation.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator
from functools import cache
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
from pairly_core.ingestion.addresses import inbound_address, rotate_inbound_token
from pairly_core.ingestion.queue import RecordingJobQueue
from pairly_core.ingestion.scan import FakeScanner
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.jobs.registry import JobContext
from pairly_core.llm.factory import build_router
from pairly_core.llm.providers.fake import FakeProvider
from pairly_core.llm.settings import ModelMode, load_llm_settings
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_core.workflow.services import GraphServices
from pairly_pay.intake import EmailIntake, receive_email
from pairly_pay.invoice_start import _org_ids, start_invoice_run, start_pending_org
from pairly_pay.learning import vendor_hints
from pairly_pay.matching import match_vendor
from pairly_pay.recordings import import_pay_recordings
from synth.pay.dataset import Dataset, build
from synth.pay.emails import render_email
from synth.pay.recordings import subset_specs

from .conftest import DbUrls, TokenFactory, make_settings
from .hire_support import ingestion_settings
from .notify_support import notify_settings
from .pay_support import (
    PLUGINS,
    PayWorld,
    audit_actions,
    new_vendor,
    owner_rows,
    retire_documents,
    seed_pay_world,
    synth_vendors,
    vendor_body,
)

DOMAIN = "inbound.pairly.localhost"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MEDIA = {"pdf": "application/pdf", "xml": "application/xml", "docx": DOCX}


@cache
def dataset() -> Dataset:
    return build()


@cache
def blob(doc_id: str) -> bytes:
    return dataset().doc(doc_id).render()


def demo(kind: str) -> str:
    return next(s.record.id for s in dataset().docs if s.record.demo == kind)


def vendor_index(synth_vendor_id: str) -> int:
    return next(i for i, v in enumerate(synth_vendors()) if v["id"] == synth_vendor_id)


@dataclasses.dataclass
class Api:
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

    def headers(self, world: PayWorld, name: str, *, otp: bool = True) -> dict[str, str]:
        claims: dict[str, Any] = {"amr": ["otp"]} if otp else {}
        token = self.tokens.token(world.people[name][1], **claims)
        return {"Authorization": f"Bearer {token}", "X-Pairly-Org": str(world.org_id)}

    async def ok(
        self,
        method: str,
        path: str,
        world: PayWorld,
        name: str,
        *,
        status: int = 200,
        json: Any = None,
    ) -> Any:
        response = await self.client.request(
            method, path, json=json, headers=self.headers(world, name)
        )
        assert response.status_code == status, (path, response.status_code, response.text)
        return response.json()

    async def upload(
        self, headers: dict[str, str], name: str, data: bytes, media: str
    ) -> httpx.Response:
        return await self.client.post(
            "/v1/pay/invoices", files={"file": (name, data, media)}, headers=headers
        )

    async def upload_doc(self, world: PayWorld, doc_id: str, name: str = "clerk") -> dict[str, Any]:
        record = dataset().doc(doc_id).record
        response = await self.upload(
            self.headers(world, name), Path(record.file).name, blob(doc_id), MEDIA[record.format]
        )
        assert response.status_code == 202, response.text
        return dict(response.json())

    async def invoice(self, world: PayWorld, invoice_id: str, name: str = "clerk") -> Any:
        return await self.ok("GET", f"/v1/pay/invoices/{invoice_id}", world, name)

    async def vendor(self, world: PayWorld, synth_vendor_id: str) -> str:
        created = await new_vendor(self, world, vendor_body(vendor_index(synth_vendor_id)))
        return str(created["id"])


@pytest.fixture
async def api(migrated: DbUrls, tokens: TokenFactory, tmp_path: Path) -> AsyncIterator[Api]:
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
    app = create_app(
        make_settings(migrated.app),
        plugins=PLUGINS,
        documents=documents,
        notification_settings=notify_settings(),
        slack_transport=FakeSlackTransport(),
        graph_services=services,
    )
    try:
        async with app.router.lifespan_context(app):
            app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
                yield Api(app, c, tokens, engine, store)
    finally:
        await engine.dispose()


async def _with_recordings(owner: AsyncEngine) -> PayWorld:
    world = await seed_pay_world(owner)
    session = create_sessionmaker(owner)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            await import_pay_recordings(session, world.org_id)
    return world


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> PayWorld:
    return await _with_recordings(owner_engine)


@pytest.fixture
async def other(owner_engine: AsyncEngine) -> PayWorld:
    return await seed_pay_world(owner_engine)


def field(detail: dict[str, Any], path: str) -> dict[str, Any]:
    (found,) = [f for f in detail["extraction"]["fields"] if f["path"] == path]
    return dict(found)


# ------------------------------------------------------------------ upload, recorded PDF


async def test_upload_recorded_pdf_to_ready_with_evidence(
    api: Api, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    doc_id = demo("clean")
    truth = dataset().doc(doc_id).record.truth
    assert truth.vendor_id is not None
    vendor_id = await api.vendor(world, truth.vendor_id)
    created = await api.upload_doc(world, doc_id)
    assert created["duplicate"] is False
    assert created["source"] == "upload"
    assert created["reference"].startswith("AP-")
    detail = await api.invoice(world, created["id"])
    assert detail["status"] == "ready", detail["review_reasons"]
    assert detail["vendor"]["id"] == vendor_id
    assert detail["vendor_match"]["method"] == "trn"
    assert detail["extraction"]["source"] == "model"
    assert detail["extraction"]["from_recording"] is True
    # Exact money, as Decimal strings.
    assert detail["total"] == truth.total
    assert detail["invoice_number"] == truth.invoice_number
    total = field(detail, "total")
    assert total["value"] == truth.total
    assert total["flags"] == []
    (span,) = total["spans"]
    assert span["page"] == 1
    assert span["bboxes"]
    lines = [f for f in detail["extraction"]["fields"] if f["pattern"] == "line_items[].line_total"]
    assert [f["value"] for f in lines] == [li.line_total for li in truth.lines]
    actions = await audit_actions(owner_engine, world.org_id, created["id"])
    for action in (
        "pay.invoice.received",
        "pay.invoice.extracting",
        "pay.invoice.guardrails_scanned",
        "pay.invoice.sanitized",
        "pay.invoice.extracted",
        "pay.invoice.vendor_matched",
        "pay.invoice.ready",
    ):
        assert action in actions

    # The same file again, by API key with the upload scope: the same invoice.
    key = await api.client.post(
        "/v1/api-keys",
        json={"name": "erp", "scopes": ["pay.invoice.upload", "pay.invoice.read"]},
        headers=api.headers(world, "admin"),
    )
    assert key.status_code == 201, key.text
    secret = key.json()["key"]
    again = await api.upload(
        {"Authorization": f"Bearer {secret}"}, "copy.pdf", blob(doc_id), "application/pdf"
    )
    assert again.status_code == 202, again.text
    assert again.json()["duplicate"] is True
    assert again.json()["id"] == created["id"]
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT count(*) AS n FROM pay_invoices WHERE org_id = :o",
        {"o": world.org_id},
    )
    assert rows[0].n == 1
    # An API key without the scope cannot submit.
    weak = await api.client.post(
        "/v1/api-keys",
        json={"name": "reader", "scopes": ["pay.invoice.read"]},
        headers=api.headers(world, "admin"),
    )
    refused = await api.upload(
        {"Authorization": f"Bearer {weak.json()['key']}"}, "x.pdf", b"%PDF-1.4", "application/pdf"
    )
    assert refused.status_code == 403


async def test_api_key_upload_is_source_api(api: Api, world: PayWorld) -> None:
    key = await api.client.post(
        "/v1/api-keys",
        json={"name": "erp", "scopes": ["pay.invoice.upload"]},
        headers=api.headers(world, "admin"),
    )
    spec = next(s for s in subset_specs(dataset()) if s.record.format == "docx")
    response = await api.upload(
        {"Authorization": f"Bearer {key.json()['key']}"}, "inv.docx", blob(spec.record.id), DOCX
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["source"] == "api"
    detail = await api.invoice(world, body["id"])
    # Read from the recording; no vendor in this organisation, so a person looks.
    assert detail["extraction"]["from_recording"] is True
    assert detail["status"] == "needs_review"
    assert "vendor_not_found" in detail["review_reasons"]
    # No pages to draw: the viewer gets the parsed text and marks evidence by offsets.
    text = detail["document"]["text"]
    number = field(detail, "invoice_number")
    (span,) = number["spans"]
    assert text[span["start"] : span["end"]] == span["quote"]


# ------------------------------------------------------------------ e-invoice, flagged, unrecorded


async def test_einvoice_is_read_exactly_without_a_model(api: Api, world: PayWorld) -> None:
    spec = next(s for s in dataset().regular() if s.record.format == "xml")
    truth = spec.record.truth
    assert truth.vendor_id is not None
    await api.vendor(world, truth.vendor_id)
    created = await api.upload_doc(world, spec.record.id)
    detail = await api.invoice(world, created["id"])
    assert detail["status"] == "ready", detail["review_reasons"]
    assert detail["extraction"]["source"] == "einvoice"
    assert detail["extraction"]["model_name"] is None
    assert detail["subtotal"] == truth.subtotal
    assert detail["vat_amount"] == truth.vat_amount
    assert detail["total"] == truth.total
    assert field(detail, "vendor_trn")["value"] == truth.vendor_trn
    assert field(detail, "total")["confidence"] == 1.0


async def test_guardrail_flagged_invoice_goes_to_review(api: Api, world: PayWorld) -> None:
    doc_id = demo("bank_change")
    truth = dataset().doc(doc_id).record.truth
    assert truth.vendor_id is not None
    await api.vendor(world, truth.vendor_id)
    created = await api.upload_doc(world, doc_id)
    detail = await api.invoice(world, created["id"])
    assert detail["status"] == "needs_review"
    assert any(r.startswith("guardrails_flagged") for r in detail["review_reasons"])
    assert detail["findings"]
    assert all(f["evidence"] is not None for f in detail["findings"])  # a processor
    assert detail["removed_spans"]
    # An approver reads the invoice, never the flagged text.
    seen = await api.invoice(world, created["id"], "approver")
    assert all(f["evidence"] is None for f in seen["findings"])
    assert seen["can_process"] is False


async def test_unrecorded_invoice_fails_closed_to_review(api: Api, world: PayWorld) -> None:
    recorded = {s.record.id for s in subset_specs(dataset())}
    spec = next(
        s for s in dataset().regular() if s.record.format == "pdf" and s.record.id not in recorded
    )
    created = await api.upload_doc(world, spec.record.id)
    detail = await api.invoice(world, created["id"])
    assert detail["status"] == "needs_review"
    assert any("model_unavailable" in r for r in detail["review_reasons"])
    # A person cannot mark it reviewed while the fields are empty.
    refused = await api.client.post(
        f"/v1/pay/invoices/{created['id']}/review", json={}, headers=api.headers(world, "clerk")
    )
    assert refused.status_code == 409
    rejected = await api.ok(
        "POST",
        f"/v1/pay/invoices/{created['id']}/reject",
        world,
        "clerk",
        json={"reason": "Sent to the wrong company."},
    )
    assert rejected["status"] == "rejected"


# ------------------------------------------------------------------ corrections and hints


async def test_correction_is_filed_per_vendor_and_feeds_the_next_prompt(
    api: Api, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    doc_id = demo("clean")
    truth = dataset().doc(doc_id).record.truth
    assert truth.vendor_id is not None
    vendor_id = await api.vendor(world, truth.vendor_id)
    created = await api.upload_doc(world, doc_id)
    corrected = await api.ok(
        "POST",
        f"/v1/pay/invoices/{created['id']}/corrections",
        world,
        "clerk",
        json={"field_path": "po_number", "new_value": "PO-CORRECTED-1", "reason": "Typo on PO"},
    )
    assert corrected["status"] == "needs_review"
    assert "corrected_after_ready" in corrected["review_reasons"]
    assert field(corrected, "po_number")["corrected"] is True
    assert corrected["extraction"]["corrections"][0]["new_value"] == "PO-CORRECTED-1"
    # Bank details are corrected but never kept as hints.
    await api.ok(
        "POST",
        f"/v1/pay/invoices/{created['id']}/corrections",
        world,
        "clerk",
        json={"field_path": "bank.swift", "new_value": "XQAAAEAD", "reason": "Check"},
    )
    session = create_sessionmaker(api.engine)()
    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        hints = await vendor_hints(session, uuid.UUID(vendor_id))
    assert [(h.field_pattern, h.new_value) for h in hints] == [("po_number", "PO-CORRECTED-1")]
    # A person reviews it back to ready.
    reviewed = await api.ok(
        "POST", f"/v1/pay/invoices/{created['id']}/review", world, "clerk", json={"note": "ok"}
    )
    assert reviewed["status"] == "ready"

    # The next invoice from that vendor (the text names its TRN) carries the hint, so
    # its request differs from any recording: a person reads it (fail closed).
    recorded = {s.record.id for s in subset_specs(dataset())}
    nxt = next(
        s
        for s in dataset().regular()
        if s.record.truth.vendor_id == truth.vendor_id
        and s.record.format == "pdf"
        and s.record.id not in recorded
    )
    second = await api.upload_doc(world, nxt.record.id)
    detail = await api.invoice(world, second["id"])
    assert detail["status"] == "needs_review"
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT payload FROM audit_events "
        "WHERE entity_id = :e AND action = 'pay.invoice.hints_used'",
        {"e": second["id"]},
    )
    assert rows[0].payload["hints"] == 1
    assert rows[0].payload["vendor_id"] == vendor_id


async def test_correction_permissions(api: Api, world: PayWorld) -> None:
    created = await api.upload_doc(world, demo("clean"))
    for name in ("approver", "auditor"):
        response = await api.client.post(
            f"/v1/pay/invoices/{created['id']}/corrections",
            json={"field_path": "po_number", "new_value": "X", "reason": "r"},
            headers=api.headers(world, name),
        )
        assert response.status_code == 403, name
    upload = await api.upload(
        api.headers(world, "approver"), "a.pdf", blob(demo("clean")), "application/pdf"
    )
    assert upload.status_code == 403
    listed = await api.client.get("/v1/pay/invoices", headers=api.headers(world, "recruiter"))
    assert listed.status_code == 403
    assert (await api.ok("GET", "/v1/pay/invoices", world, "auditor"))["items"]


# ------------------------------------------------------------------ tenant isolation


async def test_tenant_isolation(api: Api, world: PayWorld, other: PayWorld) -> None:
    created = await api.upload_doc(world, demo("near_duplicate"))
    missing = await api.client.get(
        f"/v1/pay/invoices/{created['id']}", headers=api.headers(other, "clerk")
    )
    assert missing.status_code == 404
    listed = await api.ok("GET", "/v1/pay/invoices", other, "clerk")
    assert listed["items"] == []
    for path, body in (
        ("reject", {"reason": "not mine"}),
        ("review", {}),
        ("corrections", {"field_path": "po_number", "new_value": "X", "reason": "r"}),
    ):
        response = await api.client.post(
            f"/v1/pay/invoices/{created['id']}/{path}",
            json=body,
            headers=api.headers(other, "clerk"),
        )
        assert response.status_code == 404, path
    # The same bytes in another organisation are another document and invoice.
    theirs = await api.upload_doc(other, demo("near_duplicate"))
    assert theirs["id"] != created["id"]
    assert theirs["duplicate"] is False


# ------------------------------------------------------------------ email intake


async def _address(api: Api, world: PayWorld, owner: AsyncEngine) -> str:
    """Rotated as ``pairly-admin inbound rotate`` does (the owner role)."""
    session = create_sessionmaker(owner)()
    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        token = await rotate_inbound_token(
            session, workflow="pay", actor_type="system", actor_id="test"
        )
    return inbound_address("pay", world.slug, token, DOMAIN)


async def _email(api: Api, address: str, email_id: str) -> EmailIntake:
    ds = dataset()
    record = next(e for e in ds.emails if e.id == email_id)
    raw = render_email(record, ds, {i: blob(i) for i in record.invoice_ids})
    return await receive_email(
        raw,
        sessionmaker=create_sessionmaker(api.engine),
        store=api.store,
        settings=ingestion_settings(),
        notification_settings=notify_settings(),
        envelope_recipients=[address],
    )


def _first(kind: str, *, attachments: int | None = None) -> str:
    for e in dataset().emails:
        if e.kind != kind or (attachments is not None and len(e.attachments) != attachments):
            continue
        formats = {dataset().doc(i).record.format for i in e.invoice_ids}
        if formats & {"png", "jpg", "scan_pdf"}:
            continue
        return e.id
    raise LookupError(kind)


async def test_email_intake_cases(api: Api, world: PayWorld, owner_engine: AsyncEngine) -> None:
    address = await _address(api, world, owner_engine)

    multi = await _email(api, address, _first("multi_attachment", attachments=2))
    assert multi.status == "ingested"
    assert multi.classification == "invoice"
    assert len(multi.invoice_ids) == 2
    assert multi.reference is not None
    assert multi.reference.startswith("REF-")
    assert multi.receipt is not None
    assert multi.receipt.sent is True
    assert multi.receipt.reference == multi.reference

    extra = await _email(api, address, _first("with_extra"))
    assert len(extra.invoice_ids) == 1
    assert len(extra.supporting_document_ids) == 1

    body = await _email(api, address, _first("body"))
    assert len(body.invoice_ids) == 1
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT d.media_type, i.source, i.intake_reference FROM pay_invoices i "
        "JOIN documents d ON d.id = i.document_id WHERE i.id = :i",
        {"i": body.invoice_ids[0]},
    )
    assert rows[0].media_type == "text/plain"
    assert rows[0].source == "email"
    assert rows[0].intake_reference == body.reference

    forwarded_id = _first("forwarded")
    forwarded = await _email(api, address, forwarded_id)
    record = next(e for e in dataset().emails if e.id == forwarded_id)
    assert record.original_sender is not None
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT sender_domains FROM pay_invoices WHERE id = :i",
        {"i": forwarded.invoice_ids[0]},
    )
    assert rows[0].sender_domains[0] == record.original_sender.split("@")[1]

    marketing = await _email(api, address, _first("marketing"))
    assert marketing.classification == "marketing"
    assert marketing.invoice_ids == []
    assert marketing.receipt is None

    # A retried delivery creates nothing new and sends no second receipt.
    again = await _email(api, address, _first("multi_attachment", attachments=2))
    assert again.status == "duplicate"
    assert again.invoice_ids == []
    assert again.receipt is None

    # A wrong token is dropped without a bounce.
    wrong = await _email(
        api, inbound_address("pay", world.slug, "a" * 16, DOMAIN), _first("single")
    )
    assert wrong.status == "unroutable"

    # The intake job finds the organisation and starts the runs.
    ctx = JobContext(
        sessionmaker=create_sessionmaker(api.engine),
        runtime=api.runtime,
        registry=api.runtime.registry,
    )
    assert world.org_id in await _org_ids(ctx)
    started = await start_pending_org(ctx, world.org_id)
    assert started >= 5
    statuses = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT status FROM pay_invoices WHERE org_id = :o",
        {"o": world.org_id},
    )
    assert {r.status for r in statuses} <= {"ready", "needs_review"}
    # Marketing bodies and supporting files are never scanned here: park them.
    await retire_documents(owner_engine, world.org_id)


async def test_unsure_email_goes_to_a_person(
    api: Api, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    address = await _address(api, world, owner_engine)
    raw = (
        f"From: Someone <someone@vendor-x.example>\r\nTo: {address}\r\nSubject: Hello\r\n"
        "Message-ID: <unsure-1@vendor-x.example>\r\n\r\n"
        "Please see the details we discussed on the phone last week.\r\n"
    ).encode()
    outcome = await receive_email(
        raw,
        sessionmaker=create_sessionmaker(api.engine),
        store=api.store,
        settings=ingestion_settings(),
        notification_settings=notify_settings(),
    )
    assert outcome.classification == "unsure"
    (invoice_id,) = outcome.invoice_ids
    detail = await api.invoice(world, str(invoice_id))
    assert any(r.startswith("intake_unsure") for r in detail["review_reasons"])
    view = await start_invoice_run(
        api.runtime, world.org_id, invoice_id, uuid.UUID(detail["document_id"])
    )
    assert view is not None
    detail = await api.invoice(world, str(invoice_id))
    assert detail["status"] == "needs_review"


# ------------------------------------------------------------------ vendor matching


async def test_vendor_matching_signals(api: Api, world: PayWorld) -> None:
    first, second = synth_vendors()[0], synth_vendors()[1]
    a = await api.vendor(world, first["id"])
    b = await api.vendor(world, second["id"])
    session = create_sessionmaker(api.engine)()
    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        org = world.org_id
        by_trn = await match_vendor(
            session, org, trn=first["trn"], iban=None, name=None, domains=[]
        )
        assert (str(by_trn.vendor_id), by_trn.method, by_trn.reasons) == (a, "trn", [])
        by_iban = await match_vendor(
            session, org, trn=None, iban=first["bank"]["iban"], name=None, domains=[]
        )
        assert (str(by_iban.vendor_id), by_iban.method) == (a, "iban")
        by_domain = await match_vendor(
            session, org, trn=None, iban=None, name=None, domains=[second["email_domains"][0]]
        )
        assert (str(by_domain.vendor_id), by_domain.method) == (b, "domain")
        assert by_domain.reasons == ["vendor_weak_match:domain"]
        lookalike = "x" + second["email_domains"][0]
        none = await match_vendor(session, org, trn=None, iban=None, name=None, domains=[lookalike])
        assert none.vendor_id is None
        by_name = await match_vendor(
            session, org, trn=None, iban=None, name=first["legal_name"].upper(), domains=[]
        )
        assert (str(by_name.vendor_id), by_name.method) == (a, "name")
        conflict = await match_vendor(
            session, org, trn=first["trn"], iban=second["bank"]["iban"], name=None, domains=[]
        )
        assert conflict.vendor_id is None
        assert conflict.conflict is True
