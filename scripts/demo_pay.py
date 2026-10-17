# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The Pay part of ``make demo`` (``scripts/demo_seed.py``). Development only, offline.

Everything goes through Pay's own code: the people, vendors, invoices, approvals,
tickets and payment runs through the real API routes (an in-process copy of the API on
the same database, store and settings as the running one, signing its own short-lived
tokens for the demo people, so nothing here can bypass a permission, a second factor or
the segregation of duties), and the invoice history through the engine's ingestion.
Recorded model mode, no API key. Everything is synthetic (``synth.pay``).

1. Pay people: an AP clerk, an approver and a finance controller (``seed_people``,
   invited with the rest of the demo people, then accepted by ``demo_seed``), next to
   the existing admin.
2. Pay's recorded model responses, the payer account for bank files, and the 34
   synthetic vendors with their bank accounts and phones on file, as on file for a
   year (no "new vendor" signal).
3. A history of past, paid invoices per vendor (the latest ``HISTORY_PER_VENDOR``
   before September 2026, plus the original of the near-duplicate demo invoice), each
   stored as a document with the keys later invoices are compared with (number, lines,
   template fingerprint), so the amount, duplicate and template checks have history.
   Each was paid to the vendor's account on file in a closed history run per currency
   (``HIST-<currency>``), so every paid invoice names its run like a live one does.
4. Recorded invoices driven to every status: ready, needs review, awaiting approval,
   approved, scheduled (in a payment run awaiting the finance controller), paid
   (approved, exported and marked paid) and rejected; and one just received (the
   worker starts reading it within a minute, as it would any received invoice).
5. Two bank-detail change tickets: one waiting for its call-back, one approved by two
   people and cooling off.
6. The organisation's Pay inbound address rotated (printed once) and the three demo
   emails (``synth.pay``'s demo trio, addressed to it) written to
   ``<PAIRLY_LOCAL_DIR>/inbound-demo/`` for ``make inbound-drop``. They are not
   delivered here.

Idempotent: what exists is found and moved on from where it is, never made twice. The
inbound address is the exception: each run rotates it (the token is shown only once)
and writes the demo emails again for the new address.

Purchase orders and goods receipts (``PO_DOCS``): the synthetic POs, with their goods
receipts, that the clean demo invoice and a few recorded invoices cite, created by the AP
clerk through the API before any invoice is read, so they are matched. Approval by a
rule is on in the organisation's Pay settings for matched, low-risk AED invoices under
``RULE_LIMIT``: after ``make inbound-drop``, the clean demo invoice reads to ready,
matched to PO-2026-0079 with its goods receipt; sent for approval, the rule approves it;
then it can be scheduled in a new payment run proposal.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import random
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jwt.algorithms import OKPAlgorithm
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_api.main import create_app
from pairly_core.auth import JWKSCache, TokenVerifier
from pairly_core.db import set_tenant
from pairly_core.documents.docx import DocxParser
from pairly_core.documents.model import NormalizedDocument
from pairly_core.documents.pdf import PdfParser
from pairly_core.documents.registry import ParseOptions
from pairly_core.documents.ubl import UblInvoiceParser
from pairly_core.ingestion.addresses import inbound_address, rotate_inbound_token
from pairly_core.ingestion.models import DocumentSource
from pairly_core.ingestion.service import AuditBy, ingest_spooled, spool_bytes
from pairly_core.ingestion.settings import IngestionSettings, load_ingestion_settings
from pairly_core.ingestion.storage import object_store_from_settings
from pairly_core.invitations import invite_as_system
from pairly_core.rbac import RoleRegistry
from pairly_pay.ai_recordings import import_ai_recordings
from pairly_pay.checks.document import document_facts
from pairly_pay.checks.run import keys_of
from pairly_pay.checks.types import InvoiceFacts, Line
from pairly_pay.coding_demo import cc_csv, gl_csv
from pairly_pay.common import By, audit
from pairly_pay.intake import pay_policy
from pairly_pay.invoices import INVOICE_ENTITY, create_invoice
from pairly_pay.models import (
    DEFAULT_COOLING_OFF_HOURS,
    InvoiceSource,
    InvoiceStatus,
    PayInvoice,
    PaymentRunStatus,
    PayPaymentRun,
    PayPaymentRunItem,
    RunItemStatus,
)
from pairly_pay.payability import account_digest
from pairly_pay.portal.auth import INVITE_TTL as PORTAL_INVITE_TTL
from pairly_pay.portal.auth import issue_login_token
from pairly_pay.portal.contacts import contact_email
from pairly_pay.portal.models import PayPortalContact
from pairly_pay.recordings import import_pay_recordings
from pairly_pay.vendors import active_account
from synth.pay.banking import BANK_BY_KEY, make_iban
from synth.pay.dataset import Dataset, DocSpec, build
from synth.pay.emails import render_email
from synth.pay.recordings import subset_specs

SEED_ACTOR: Final = "pairly.devseed.demo"
ORG_SLUG: Final = "demo"
ADMIN: Final = "admin"
CLERK: Final = "clerk"
APPROVER: Final = "approver"
FINANCE: Final = "finance"
# name -> (email, role, workflow)
PEOPLE: Final = {
    CLERK: ("demo-pay-clerk@example.test", "ap_clerk", "pay"),
    APPROVER: ("demo-pay-approver@example.test", "approver", "pay"),
    FINANCE: ("demo-pay-finance@example.test", "finance_controller", "pay"),
}
ESTABLISHED_DAYS: Final = 400
HISTORY_PER_VENDOR: Final = 6
HISTORY_BEFORE: Final = "2026-09-01"
HISTORY_FORMATS: Final = frozenset({"pdf", "docx", "xml"})
HISTORY_CASH_LIMIT: Final = Decimal("1000000000000")
DEMO_KINDS: Final = ("clean", "near_duplicate", "bank_change")
DEMO_FILES: Final = {
    "clean": "1-clean.eml",
    "near_duplicate": "2-near-duplicate.eml",
    "bank_change": "3-bank-change.eml",
}
PARSE: Final = ParseOptions(ocr_languages="eng")
# Approval by a rule (``pairly_pay.payment_settings``): matched, low-risk AED invoices
# under this amount. The clean demo invoice is AED 6,507.27.
RULE_LIMIT: Final = "10000"
# Invoices whose synthetic PO (and goods receipts) are seeded: the clean demo invoice,
# and recorded invoices of TARGETS (exact 3-way, price within and over the tolerance,
# 2-way services, the first invoice of a PO with two).
CLEAN_DOC: Final = "inv-0165"
PO_DOCS: Final = (CLEAN_DOC, "inv-0001", "inv-0030", "inv-0037", "inv-0047", "inv-0073")


@dataclasses.dataclass(frozen=True, slots=True)
class Target:
    doc_id: str
    status: str


# Recorded invoices (``synth.pay`` recordings) and the status each is left in. The
# vendors of the demo trio (VEN-015, VEN-021, VEN-022) and of the tickets are not used.
PAID: Final = ("inv-0001", "inv-0030")
SCHEDULED: Final = ("inv-0055", "inv-0073")
TARGETS: Final = (
    *(Target(d, "approved") for d in PAID),  # then paid through a payment run
    *(Target(d, "approved") for d in SCHEDULED),  # then in the run awaiting approval
    Target("inv-0037", "approved"),
    Target("inv-0047", "awaiting_approval"),
    Target("inv-0014", "ready"),
    Target("inv-0069", "ready"),
    Target("inv-0111", "needs_review"),
    Target("inv-0194", "rejected"),
)
RECEIVED: Final = "inv-0203"
# Vendor index in synth.pay (VEN-012, VEN-013): a ticket waiting for its call-back and
# one cooling off after two approvals.
TICKET_WAITING: Final = "VEN-012"
TICKET_COOLING: Final = "VEN-013"
REQUEST_PHONE: Final = "+971 4 555 0142"
MEDIA: Final = {
    "pdf": "application/pdf",
    "xml": "application/xml",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class SeedError(RuntimeError):
    pass


@dataclasses.dataclass
class PaySeed:
    lines: list[str] = dataclasses.field(default_factory=list)
    address: str = ""
    emails: list[Path] = dataclasses.field(default_factory=list)
    counts: dict[str, Any] = dataclasses.field(default_factory=dict)
    # A fresh vendor portal sign-in link for the person running the demo (fragment only).
    portal_token: str = ""
    portal_email: str = ""


# ------------------------------------------------------------------ people


async def seed_people(session: AsyncSession, registry: RoleRegistry) -> dict[str, str]:
    """Invite the Pay people (in the organisation's session). Returns the new tokens."""
    tokens: dict[str, str] = {}
    for email, role, workflow in PEOPLE.values():
        created = await invite_as_system(
            session, registry, email=email, role=role, workflow=workflow, actor_id=SEED_ACTOR
        )
        if created is not None:
            tokens[email] = created.token
    return tokens


# ------------------------------------------------------------------ the in-process API


@dataclasses.dataclass
class PayApi:
    client: httpx.AsyncClient
    key: Ed25519PrivateKey
    issuer: str
    audience: str
    org_id: uuid.UUID
    subjects: dict[str, str]
    ids: dict[str, uuid.UUID]

    def headers(self, name: str) -> dict[str, str]:
        now = int(time.time())
        claims = {
            "sub": self.subjects[name],
            "iss": self.issuer,
            "aud": self.audience,
            "iat": now,
            "exp": now + 300,
            "amr": ["otp"],
        }
        token = jwt.encode(claims, self.key, algorithm="EdDSA", headers={"kid": "demo-seed"})
        return {"Authorization": f"Bearer {token}", "X-Pairly-Org": str(self.org_id)}

    async def call(
        self, method: str, path: str, name: str, *, json: Any = None, **kw: Any
    ) -> httpx.Response:
        headers = self.headers(name) | {"Idempotency-Key": f"demo-{uuid.uuid4().hex}"}
        return await self.client.request(method, path, json=json, headers=headers, **kw)

    async def ok(
        self, method: str, path: str, name: str, *, status: int = 200, json: Any = None
    ) -> Any:
        response = await self.call(method, path, name, json=json)
        if response.status_code != status:
            raise SeedError(f"{method} {path} as {name}: {response.status_code} {response.text}")
        return response.json()


def _verifier(key: Ed25519PrivateKey, url: str, issuer: str, audience: str) -> TokenVerifier:
    jwk = json.loads(OKPAlgorithm.to_jwk(key.public_key()))
    keys = {"keys": [jwk | {"kid": "demo-seed", "alg": "EdDSA", "use": "sig"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=keys)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JWKSCache(url, allowed_algorithms=["EdDSA"], client=client)
    return TokenVerifier(jwks=cache, issuer=issuer, audience=audience, algorithms=["EdDSA"])


@asynccontextmanager
async def pay_api(
    org_id: uuid.UUID, people: dict[str, tuple[uuid.UUID, str]]
) -> AsyncIterator[PayApi]:
    """The API in this process, with a verifier for a key only this process holds."""
    app = create_app()
    key = Ed25519PrivateKey.generate()
    async with app.router.lifespan_context(app):
        settings = app.state.pairly.settings
        verifier = _verifier(
            key, settings.auth_jwks_url, settings.auth_issuer, settings.auth_audience
        )
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=verifier)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://demo-seed.local", timeout=600
        ) as client:
            yield PayApi(
                client,
                key,
                settings.auth_issuer,
                settings.auth_audience,
                org_id,
                {n: s for n, (_i, s) in people.items()},
                {n: i for n, (i, _s) in people.items()},
            )
        await verifier.aclose()


# ------------------------------------------------------------------ helpers


async def _rows(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    sql: str,
    params: dict[str, Any] | None = None,
) -> list[Any]:
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            result = await session.execute(text(sql), params or {})
            return list(result.all()) if sql.lstrip().upper().startswith("SELECT") else []


async def _people(
    sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID, admin_email: str
) -> dict[str, tuple[uuid.UUID, str]]:
    emails = {ADMIN: admin_email} | {n: e for n, (e, _r, _w) in PEOPLE.items()}
    out: dict[str, tuple[uuid.UUID, str]] = {}
    for name, email in emails.items():
        rows = await _rows(
            sessionmaker,
            org_id,
            "SELECT id, auth_subject FROM users WHERE lower(email) = :e",
            {"e": email.lower()},
        )
        if not rows or not rows[0].auth_subject:
            raise SeedError(f"no member {email}")
        out[name] = (rows[0].id, str(rows[0].auth_subject))
    return out


def _vendor_body(vendor: Any) -> dict[str, Any]:
    return {
        "legal_name": vendor.legal_name,
        "legal_name_ar": vendor.legal_name_ar,
        "trade_name": vendor.trade_name,
        "country": vendor.country,
        "trade_licence": vendor.trade_licence,
        "trn": vendor.trn,
        "address": {
            k: v for k, v in vendor.address.model_dump().items() if v is not None and v != ""
        },
        "contacts": [
            {"name": c.name, "role": c.role, "email": c.email, "phone": c.phone}
            for c in vendor.contacts
        ],
        "phone_on_file": vendor.phone_on_file,
        # The number on file is taken from the vendor's trade licence (M-03).
        "phone_source": {
            "source": "trade_licence",
            "reference": f"Trade licence {vendor.trade_licence or vendor.legal_name}",
        },
        "email_domains": list(vendor.email_domains),
        "category": vendor.category,
        "bank": {
            "iban": vendor.bank.iban,
            "account_name": vendor.bank.account_name,
            "bank_name": vendor.bank.bank_name,
            "swift": vendor.bank.bic,
        },
    }


def _new_bank(vendor: Any, bank_key: str) -> dict[str, Any]:
    """Synthetic new details for a ticket: a seeded IBAN at a synthetic bank."""
    bank = BANK_BY_KEY[bank_key]
    rng = random.Random(f"demo-ticket-{vendor.id}")  # noqa: S311 (synthetic data)
    return {
        "iban": make_iban(bank, rng),
        "account_name": vendor.legal_name,
        "bank_name": bank.name,
        "swift": bank.bic,
    }


async def _vendor_ids(
    sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    rows = await _rows(sessionmaker, org_id, "SELECT id, legal_name FROM pay_vendors")
    return {str(r.legal_name): r.id for r in rows}


# ------------------------------------------------------------------ vendors


async def seed_vendors(
    api: PayApi, sessionmaker: async_sessionmaker[AsyncSession], ds: Dataset
) -> tuple[dict[str, uuid.UUID], list[str]]:
    """The synthetic vendors by synth id, created by the admin where missing. Their bank
    details go through the change workflow like any new vendor's (security review
    2026-10, M-03): the AP clerk (who holds the verify permission and neither created
    the vendor nor recorded its number) calls the number on file, two people approve,
    and no cooling-off applies while the demo is seeded."""
    known = await _vendor_ids(sessionmaker, api.org_id)
    created: list[uuid.UUID] = []
    await api.ok("PUT", "/v1/pay/settings", ADMIN, json={"cooling_off_hours": 0})
    try:
        for vendor in ds.vendors:
            if vendor.legal_name in known:
                continue
            body = await api.ok(
                "POST", "/v1/pay/vendors", ADMIN, status=201, json=_vendor_body(vendor)
            )
            created.append(uuid.UUID(str(body["id"])))
            await _verify_new_vendor(api, sessionmaker, str(body["open_ticket_id"]))
    finally:
        await api.ok(
            "PUT", "/v1/pay/settings", ADMIN, json={"cooling_off_hours": DEFAULT_COOLING_OFF_HOURS}
        )
    if created:
        # On file for a year: an established vendor, not a "new vendor" signal. (The
        # bank accounts stay as they are: their details are locked by the database.)
        params = {"ids": created, "d": ESTABLISHED_DAYS}
        await _rows(
            sessionmaker,
            api.org_id,
            "UPDATE pay_vendors SET created_at = created_at - make_interval(days => :d), "
            "activated_at = activated_at - make_interval(days => :d), "
            "updated_at = updated_at - make_interval(days => :d) WHERE id = ANY(:ids)",
            params,
        )
    known = await _vendor_ids(sessionmaker, api.org_id)
    by_synth = {v.id: known[v.legal_name] for v in ds.vendors if v.legal_name in known}
    if len(by_synth) != len(ds.vendors):
        raise SeedError("not every synthetic vendor is on file")
    return by_synth, [f"pay vendors: {len(created)} created, {len(by_synth)} on file"]


async def _verify_new_vendor(
    api: PayApi, sessionmaker: async_sessionmaker[AsyncSession], ticket_id: str
) -> None:
    """A new vendor's bank details: verified by the AP clerk (not the admin who entered
    them) on the number on file, then approved by two people."""
    path = f"/v1/pay/bank-changes/{ticket_id}"
    current = await api.ok(
        "POST",
        f"{path}/calls",
        CLERK,
        json={
            "result": "confirmed",
            "notes": "Called the number on the trade licence; the vendor confirmed.",
            "called_number_on_file": True,
        },
    )
    if not current.get("approval_id"):
        raise SeedError(f"bank-detail change {ticket_id} did not start its approval")
    await approve_all(api, sessionmaker, str(current["approval_id"]))
    done = await api.ok("GET", path, ADMIN)
    if done["status"] != "completed":
        raise SeedError(f"bank-detail change {ticket_id} is {done['status']}")


# ------------------------------------------------------------------ GL coding


async def seed_coding(
    api: PayApi, sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> list[str]:
    """The demo chart of accounts and cost centres (``pairly_pay.coding_demo``), imported
    from CSV through the API once."""
    lines: list[str] = []
    for kind, path, content in (
        ("gl_account", "gl-accounts", gl_csv()),
        ("cost_centre", "cost-centres", cc_csv()),
    ):
        found = await _rows(
            sessionmaker, org_id, "SELECT id FROM pay_coding_lists WHERE kind = :k", {"k": kind}
        )
        if found:
            lines.append(f"pay {path}: already on file")
            continue
        await api.ok(
            "POST", f"/v1/pay/coding/{path}/import", ADMIN, status=201, json={"csv": content}
        )
        lines.append(f"pay {path}: imported")
    return lines


# ------------------------------------------------------------------ purchase orders


def po_numbers(ds: Dataset) -> list[str]:
    """The PO numbers of ``PO_DOCS``, in order."""
    out: list[str] = []
    for doc_id in PO_DOCS:
        number = ds.doc(doc_id).record.truth.po_number
        if number is None:
            raise SeedError(f"{doc_id} cites no purchase order")
        out.append(number)
    return out


async def seed_purchase_orders(
    api: PayApi,
    sessionmaker: async_sessionmaker[AsyncSession],
    ds: Dataset,
    vendors: dict[str, uuid.UUID],
) -> list[str]:
    """The POs and goods receipts of ``PO_DOCS``, created by the AP clerk where missing."""
    numbers = po_numbers(ds)
    rows = await _rows(sessionmaker, api.org_id, "SELECT id, po_number FROM pay_purchase_orders")
    known = {str(r.po_number): str(r.id) for r in rows}
    grns = {
        str(r.grn_number)
        for r in await _rows(sessionmaker, api.org_id, "SELECT grn_number FROM pay_goods_receipts")
    }
    created = received = 0
    for number in numbers:
        (po,) = [p for p in ds.purchase_orders if p.po_number == number]
        if number not in known:
            body = await api.ok(
                "POST",
                "/v1/pay/purchase-orders",
                CLERK,
                status=201,
                json={
                    "po_number": po.po_number,
                    "vendor_id": str(vendors[po.vendor_id]),
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
            known[number] = str(body["id"])
            created += 1
        for grn in (r for r in ds.receipts if r.po_number == number):
            if grn.grn_number in grns:
                continue
            await api.ok(
                "POST",
                "/v1/pay/goods-receipts",
                CLERK,
                status=201,
                json={
                    "grn_number": grn.grn_number,
                    "po_id": known[number],
                    "received_date": grn.date,
                    "received_by": grn.received_by,
                    "lines": [
                        {"po_line_no": x.po_line_no, "quantity": x.quantity_received}
                        for x in grn.lines
                    ],
                },
            )
            grns.add(grn.grn_number)
            received += 1
    return [
        f"pay purchase orders: {created} created, {len(numbers)} on file for the demo "
        f"({received} goods receipts recorded)"
    ]


# ------------------------------------------------------------------ history


def history_specs(ds: Dataset) -> list[DocSpec]:
    """Per vendor the latest regular invoices before ``HISTORY_BEFORE`` (never one the
    demo uploads or a recorded one), plus every demo attack's original."""
    recorded = {s.record.id for s in subset_specs(ds)} | {RECEIVED}
    originals = {
        s.record.attack.original_id
        for s in ds.docs
        if s.record.demo and s.record.attack and s.record.attack.original_id
    }
    by_vendor: dict[str, list[DocSpec]] = {}
    for spec in ds.regular():
        record = spec.record
        if (
            record.id in recorded
            or record.format not in HISTORY_FORMATS
            or record.truth.invoice_date >= HISTORY_BEFORE
        ):
            continue
        by_vendor.setdefault(record.truth.vendor_id or "", []).append(spec)
    chosen: list[DocSpec] = []
    for specs in by_vendor.values():
        specs.sort(key=lambda s: (s.record.truth.invoice_date, s.record.id))
        chosen += specs[-HISTORY_PER_VENDOR:]
    ids = {s.record.id for s in chosen}
    chosen += [ds.doc(i) for i in sorted(originals) if i not in ids]
    return sorted(chosen, key=lambda s: (s.record.truth.invoice_date, s.record.id))


def _parse(spec: DocSpec, data: bytes, work: Path) -> NormalizedDocument:
    path = work / Path(spec.record.file).name
    path.write_bytes(data)
    fmt = spec.record.format
    if fmt == "pdf":
        return PdfParser().parse(path, PARSE)
    if fmt == "docx":
        return DocxParser().parse(path, PARSE)
    return UblInvoiceParser().parse(path, PARSE)


def _d(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


async def _history_run(
    session: AsyncSession, org_id: uuid.UUID, currency: str, people: HistoryPeople
) -> PayPaymentRun:
    """The paid run the history of one currency hangs off (``HIST-<currency>``): paid
    invoices always name the run that paid them and the approval they were paid on."""
    reference = f"HIST-{currency}"
    run = await session.scalar(select(PayPaymentRun).where(PayPaymentRun.reference == reference))
    if run is not None:
        return run
    done = dt.datetime.combine(dt.date.fromisoformat(HISTORY_BEFORE), dt.time(8), dt.UTC)
    run = PayPaymentRun(
        id=uuid.uuid4(),
        org_id=org_id,
        reference=reference,
        status=PaymentRunStatus.PAID.value,
        currency=currency,
        cash_limit=HISTORY_CASH_LIMIT,
        total=Decimal(0),
        item_count=0,
        execution_date=done.date(),
        due_on_or_before=None,
        excluded=[],
        proposed_by=people.clerk,
        approver_ids=[str(people.finance)],
        approved_at=done,
        exports=[],
        exported_at=done,
        paid_at=done,
        closed_at=done,
        close_reason="history_import",
        decision_ids=[],
        created_at=done,
        updated_at=done,
    )
    session.add(run)
    await session.flush()
    return run


@dataclasses.dataclass(frozen=True, slots=True)
class HistoryPeople:
    clerk: uuid.UUID
    finance: uuid.UUID


async def seed_history(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    ds: Dataset,
    vendors: dict[str, uuid.UUID],
    ingestion: IngestionSettings,
    people: HistoryPeople,
) -> list[str]:
    """Past invoices, paid: the documents through the engine's ingestion (the worker
    scans and parses them like any document), the values and comparison keys from the
    synthetic ground truth and the parsed file, each paid in its currency's history run
    to the vendor's account on file."""
    store = object_store_from_settings(ingestion)
    policy = pay_policy(ingestion)
    added = known = 0
    specs = history_specs(ds)
    with tempfile.TemporaryDirectory(prefix="pairly-demo-pay-") as tmp:
        work = Path(tmp)
        for n, spec in enumerate(specs, start=1):
            data = spec.render()
            async with sessionmaker() as session:
                set_tenant(session, org_id, None)
                async with session.begin():
                    spooled = await spool_bytes(data, work, max_bytes=policy.max_bytes)
                    outcome = await ingest_spooled(
                        session,
                        store,
                        spooled=spooled,
                        workflow="pay",
                        source=DocumentSource.UPLOAD,
                        filename=Path(spec.record.file).name,
                        policy=policy,
                        by=AuditBy.system(SEED_ACTOR),
                    )
                    if outcome.document is None:
                        raise SeedError(f"{spec.record.id}: {outcome.reason}")
                    invoice, created = await create_invoice(
                        session,
                        document=outcome.document,
                        source=InvoiceSource.UPLOAD,
                        by=By.system(SEED_ACTOR),
                    )
                    if not created:
                        known += 1
                        continue
                    facts = document_facts(_parse(spec, data, work), outcome.document.sha256)
                    await _paid_history(session, org_id, invoice, spec, vendors, facts, people, n)
                    await audit(
                        session,
                        By.system(SEED_ACTOR),
                        "pay.invoice.history_imported",
                        entity_type=INVOICE_ENTITY,
                        entity_id=invoice.id,
                        payload={"status": invoice.status, "synthetic": True},
                    )
                    added += 1
    return [f"pay history: {added} past invoices imported, {known} already there"]


async def _paid_history(
    session: AsyncSession,
    org_id: uuid.UUID,
    invoice: PayInvoice,
    spec: DocSpec,
    vendors: dict[str, uuid.UUID],
    facts: Any,
    people: HistoryPeople,
    n: int,
) -> None:
    truth = spec.record.truth
    lines = tuple(
        Line(
            index=i,
            description=x.description,
            quantity=_d(x.quantity),
            unit_price=_d(x.unit_price),
            vat_rate=_d(x.vat_rate),
            vat_amount=_d(x.vat_amount),
            line_total=_d(x.line_total),
        )
        for i, x in enumerate(truth.lines)
    )
    keys = keys_of(
        InvoiceFacts(
            id=str(invoice.id),
            received_at=dt.datetime.now(dt.UTC),
            vendor=None,
            invoice_number=truth.invoice_number,
            lines=lines,
            document=facts,
        )
    )
    date = dt.date.fromisoformat(truth.invoice_date)
    due = dt.date.fromisoformat(truth.due_date)
    received = dt.datetime.combine(date + dt.timedelta(days=1), dt.time(9), dt.UTC)
    approved = received + dt.timedelta(days=1)
    paid = max(dt.datetime.combine(due, dt.time(10), dt.UTC), approved + dt.timedelta(hours=1))
    vendor_id = vendors[truth.vendor_id or ""]
    account = await active_account(session, vendor_id)
    if account is None:
        raise SeedError(f"{spec.record.id}: the vendor has no account on file")
    total = _d(truth.total)
    if total is None or total <= 0:
        raise SeedError(f"{spec.record.id}: no total")
    run = await _history_run(session, org_id, truth.currency, people)
    checks_digest = hashlib.sha256(f"history:{invoice.id}".encode()).hexdigest()
    bank_digest = account_digest(account)

    invoice.status = InvoiceStatus.PAID.value
    invoice.vendor_id = vendor_id
    invoice.vendor_match = {"method": "history_import", "reasons": []}
    invoice.vendor_name = truth.vendor_name[:200]
    invoice.vendor_trn = truth.vendor_trn
    invoice.invoice_number = truth.invoice_number
    invoice.invoice_date = date
    invoice.due_date = due
    invoice.currency = truth.currency
    invoice.po_number = truth.po_number
    invoice.subtotal = _d(truth.subtotal)
    invoice.vat_amount = _d(truth.vat_amount)
    invoice.total = total
    invoice.number_key = keys.number_key
    invoice.lines_key = keys.lines_key
    invoice.fingerprint = dict(keys.fingerprint) if keys.fingerprint else None
    invoice.approval_submitted_by = people.clerk
    invoice.approval_submitted_at = received
    invoice.approved_at = approved
    invoice.approver_ids = [str(people.finance)]
    invoice.approved_amount = total
    invoice.approved_currency = truth.currency
    invoice.approved_account_id = account.id
    invoice.approved_bank_digest = bank_digest
    invoice.approved_checks_digest = checks_digest
    invoice.payment_run_id = run.id
    invoice.paid_at = paid
    invoice.paid_by = people.clerk
    invoice.payment_reference = f"HIST-{n:04d}"
    invoice.created_at = received
    invoice.updated_at = paid

    run.item_count += 1
    run.total += total
    session.add(
        PayPaymentRunItem(
            id=uuid.uuid4(),
            org_id=org_id,
            payment_run_id=run.id,
            invoice_id=invoice.id,
            vendor_id=vendor_id,
            position=run.item_count,
            status=RunItemStatus.PAID.value,
            amount=total,
            currency=truth.currency,
            due_date=due,
            discount=None,
            bank_account_id=account.id,
            bank_details_digest=bank_digest,
            checks_digest=checks_digest,
            end_to_end_id=f"{run.reference}-{run.item_count:05d}",
            paid_at=paid,
            paid_by=people.clerk,
            payment_reference=invoice.payment_reference,
            created_at=paid,
            updated_at=paid,
        )
    )


# ------------------------------------------------------------------ invoices


async def _approval(
    sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID, approval_id: str
) -> tuple[str, list[str]]:
    rows = await _rows(
        sessionmaker,
        org_id,
        "SELECT a.status, s.assignee_ids FROM approvals a LEFT JOIN approval_steps s "
        "ON s.approval_id = a.id AND s.position = a.current_step WHERE a.id = :a",
        {"a": approval_id},
    )
    if not rows:
        raise SeedError(f"no approval {approval_id}")
    return str(rows[0].status), [str(u) for u in rows[0].assignee_ids or []]


async def approve_all(
    api: PayApi,
    sessionmaker: async_sessionmaker[AsyncSession],
    approval_id: str,
    people: Sequence[str] = (APPROVER, FINANCE),
) -> None:
    """Each step decided by the first eligible demo person (never the same twice)."""
    used: list[str] = []
    for _ in range(5):
        status, assignees = await _approval(sessionmaker, api.org_id, approval_id)
        if status != "pending":
            return
        who = next(
            (p for p in people if p not in used and str(api.ids[p]) in assignees),
            None,
        )
        if who is None:
            raise SeedError(f"nobody left to decide approval {approval_id}")
        await api.ok(
            "POST",
            f"/v1/approvals/{approval_id}/decision",
            who,
            json={"decision": "approve", "reason": "Checked the invoice, findings and risk."},
        )
        used.append(who)


async def _invoice_of(
    sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID, sha256: str
) -> str | None:
    rows = await _rows(
        sessionmaker,
        org_id,
        "SELECT i.id FROM pay_invoices i JOIN documents d ON d.id = i.document_id "
        "WHERE d.sha256 = :s AND d.workflow = 'pay'",
        {"s": sha256},
    )
    return str(rows[0].id) if rows else None


async def _upload(api: PayApi, spec: DocSpec, data: bytes) -> str:
    response = await api.client.post(
        "/v1/pay/invoices",
        files={"file": (Path(spec.record.file).name, data, MEDIA[spec.record.format])},
        headers=api.headers(CLERK) | {"Idempotency-Key": f"demo-{uuid.uuid4().hex}"},
    )
    if response.status_code != 202:
        raise SeedError(f"upload {spec.record.id}: {response.status_code} {response.text}")
    return str(response.json()["id"])


async def drive(
    api: PayApi,
    sessionmaker: async_sessionmaker[AsyncSession],
    spec: DocSpec,
    target: str,
) -> str:
    """Upload the invoice once and move it towards ``target``. Returns its id."""
    data = spec.render()
    invoice_id = await _invoice_of(sessionmaker, api.org_id, hashlib.sha256(data).hexdigest())
    if invoice_id is None:
        invoice_id = await _upload(api, spec, data)
    path = f"/v1/pay/invoices/{invoice_id}"
    for _ in range(12):
        detail = await api.ok("GET", path, CLERK)
        status = detail["status"]
        if status == target or status in {"scheduled", "paid", "rejected"}:
            return invoice_id
        if status in {"received", "extracting"}:
            await _sleep(5)
        elif status == "needs_review":
            if target == "rejected":
                await _reject(api, path)
            else:
                await api.ok(
                    "POST", f"{path}/review", CLERK, json={"note": "Checked side by side."}
                )
        elif status == "ready":
            if target == "needs_review":
                await api.ok(
                    "POST",
                    f"{path}/corrections",
                    CLERK,
                    json={
                        "field_path": "po_number",
                        "new_value": "PO-2026-0950",
                        "reason": "The PO number from the purchasing team.",
                    },
                )
            elif target == "rejected":
                await _reject(api, path)
            else:
                await api.ok("POST", f"{path}/submit", CLERK, json={"note": "For approval."})
        elif status == "awaiting_approval":
            approval = (detail.get("approval") or {}).get("approval_id")
            if not approval:
                raise SeedError(f"{spec.record.id}: awaiting approval without an approval")
            await approve_all(api, sessionmaker, str(approval))
        elif status == "approved":
            return invoice_id
    raise SeedError(f"{spec.record.id}: stuck before {target}")


async def _reject(api: PayApi, path: str) -> None:
    await api.ok(
        "POST",
        f"{path}/reject",
        CLERK,
        json={"reason": "Already paid against the paper copy last month."},
    )


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def received_invoice(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    spec: DocSpec,
    ingestion: IngestionSettings,
    clerk: tuple[uuid.UUID, str],
) -> bool:
    """One invoice just received (stored, not read yet), as an upload leaves it before
    the run starts. The worker's intake job starts it within a minute."""
    data = spec.render()
    policy = pay_policy(ingestion)
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            with tempfile.TemporaryDirectory(prefix="pairly-demo-pay-") as tmp:
                spooled = await spool_bytes(data, Path(tmp), max_bytes=policy.max_bytes)
                outcome = await ingest_spooled(
                    session,
                    object_store_from_settings(ingestion),
                    spooled=spooled,
                    workflow="pay",
                    source=DocumentSource.UPLOAD,
                    filename=Path(spec.record.file).name,
                    policy=policy,
                    by=AuditBy.system(SEED_ACTOR),
                    submitted_by=clerk[0],
                )
            if outcome.document is None:
                raise SeedError(f"{spec.record.id}: {outcome.reason}")
            _invoice, created = await create_invoice(
                session,
                document=outcome.document,
                source=InvoiceSource.UPLOAD,
                by=By.system(SEED_ACTOR),
            )
    return created


# ------------------------------------------------------------------ payment runs


async def _run_of(
    sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID, invoice_id: str
) -> str | None:
    rows = await _rows(
        sessionmaker,
        org_id,
        "SELECT payment_run_id FROM pay_invoices WHERE id = :i",
        {"i": invoice_id},
    )
    return str(rows[0].payment_run_id) if rows and rows[0].payment_run_id else None


async def payment_run(
    api: PayApi,
    sessionmaker: async_sessionmaker[AsyncSession],
    invoice_ids: Sequence[str],
    *,
    pay: bool,
) -> str:
    """A run of exactly ``invoice_ids`` (AED), proposed by the AP clerk; with ``pay``
    approved by the finance controller, exported and marked paid."""
    run_id = await _run_of(sessionmaker, api.org_id, invoice_ids[0])
    if run_id is None:
        run = await api.ok(
            "POST",
            "/v1/pay/payment-runs",
            CLERK,
            status=201,
            json={
                "currency": "AED",
                "cash_limit": "10000000",
                "execution_date": (dt.date.today() + dt.timedelta(days=2)).isoformat(),
                "invoice_ids": list(invoice_ids),
            },
        )
        run_id = str(run["id"])
    path = f"/v1/pay/payment-runs/{run_id}"
    run = await api.ok("GET", path, CLERK)
    if not pay:
        return str(run["status"])
    if run["status"] == "awaiting_approval":
        await approve_all(api, sessionmaker, str(run["approval_id"]), people=(FINANCE,))
        run = await api.ok("GET", path, CLERK)
    if run["status"] == "approved":
        response = await api.call("POST", f"{path}/export", CLERK, json={"format": "csv"})
        if response.status_code != 200:
            raise SeedError(f"export: {response.status_code} {response.text}")
        run = await api.ok("GET", path, CLERK)
    if run["status"] == "exported":
        run = await api.ok("POST", f"{path}/paid", CLERK, json={"reference": "DEMO-BANK-0001"})
    return str(run["status"])


# ------------------------------------------------------------------ tickets


async def ticket(
    api: PayApi,
    sessionmaker: async_sessionmaker[AsyncSession],
    vendor: Any,
    vendor_id: uuid.UUID,
    *,
    cooling: bool,
) -> str:
    """A bank-detail change asked for by email: opened by the admin; with ``cooling``
    verified by the AP clerk on the number on file and approved by two people."""
    found = await api.ok("GET", f"/v1/pay/bank-changes?vendor_id={vendor_id}", ADMIN)
    items = found["items"]
    if not items:
        opened = await api.ok(
            "POST",
            f"/v1/pay/vendors/{vendor_id}/bank-changes",
            ADMIN,
            status=201,
            json={
                "bank": _new_bank(vendor, "ae_rimal" if cooling else "ae_marjan"),
                "source": "email",
                "note": "Email from accounts asking us to pay a new account from next month.",
                "request_contact": {
                    "name": vendor.contacts[-1].name,
                    "phone": REQUEST_PHONE,
                    "email": vendor.contacts[-1].email,
                },
            },
        )
        ticket_id = str(opened["id"])
    else:
        ticket_id = str(items[0]["id"])
    path = f"/v1/pay/bank-changes/{ticket_id}"
    current = await api.ok("GET", path, ADMIN)
    if not cooling:
        return str(current["status"])
    if current["status"] == "awaiting_verification":
        current = await api.ok(
            "POST",
            f"{path}/calls",
            CLERK,
            json={
                "result": "confirmed",
                "notes": "Called the finance manager on the number on file; confirmed.",
                "called_number_on_file": True,
            },
        )
    if current["status"] == "awaiting_approval" and current.get("approval_id"):
        await approve_all(api, sessionmaker, str(current["approval_id"]))
        current = await api.ok("GET", path, ADMIN)
    return str(current["status"])


# ------------------------------------------------------------------ inbound


async def demo_emails(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    ds: Dataset,
    domain: str,
    out: Path,
) -> tuple[str, list[Path]]:
    """Rotate the Pay inbound address and write the demo trio addressed to it."""
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            token = await rotate_inbound_token(
                session, workflow="pay", actor_type="system", actor_id=SEED_ACTOR
            )
    address = inbound_address("pay", ORG_SLUG, token, domain)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for kind in DEMO_KINDS:
        record = next(e for e in ds.emails if e.demo == kind)
        record = record.model_copy(update={"to": address})
        raw = render_email(record, ds, {i: ds.doc(i).render() for i in record.invoice_ids})
        path = out / DEMO_FILES[kind]
        path.write_bytes(raw)
        written.append(path)
    return address, written


# ------------------------------------------------------------------ vendor portal

PORTAL_DOC: Final = PAID[0]
PORTAL_HEADER: Final = "X-Pairly-Vendor-Portal"
PORTAL_QUESTION: Final = (
    "Could you confirm the date this invoice was paid? Our bank shows two transfers."
)
PORTAL_ANSWER: Final = (
    "It was paid in one transfer with the reference shown in the portal. The second "
    "transfer is not ours: please check it with your bank."
)


async def _portal_contact(
    sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID, vendor_id: uuid.UUID
) -> PayPortalContact | None:
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            row = await session.scalar(
                select(PayPortalContact).where(
                    PayPortalContact.vendor_id == vendor_id,
                    PayPortalContact.status == "active",
                )
            )
            if row is not None:
                session.expunge(row)
            return row


async def _portal_link(
    sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID, contact: PayPortalContact
) -> str:
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            token, _ = await issue_login_token(
                session, contact, purpose="request", ttl=PORTAL_INVITE_TTL, locale="en"
            )
    return token


async def seed_portal(
    api: PayApi,
    sessionmaker: async_sessionmaker[AsyncSession],
    ds: Dataset,
    vendors: dict[str, uuid.UUID],
    invoice_id: str,
    seed: PaySeed,
) -> list[str]:
    """Vendor portal access for one contact of the vendor of a paid invoice, a question
    the vendor asked in the portal about that invoice and accounts payable's answer, and
    a fresh sign-in link for the person running the demo."""
    synth = ds.doc(PORTAL_DOC).record.truth.vendor_id
    if synth is None:
        raise SeedError("the portal demo invoice has no vendor")
    vendor_id = vendors[synth]
    contact = await _portal_contact(sessionmaker, api.org_id, vendor_id)
    if contact is None:
        vendor = await api.ok("GET", f"/v1/pay/vendors/{vendor_id}", CLERK)
        email = next(str(c["email"]) for c in vendor["contacts"] if c.get("email"))
        await api.ok(
            "POST",
            f"/v1/pay/vendors/{vendor_id}/portal-contacts",
            CLERK,
            status=201,
            json={"email": email, "locale": "en"},
        )
        contact = await _portal_contact(sessionmaker, api.org_id, vendor_id)
    if contact is None:
        raise SeedError("the vendor portal contact was not created")
    existing = await _rows(
        sessionmaker,
        api.org_id,
        "SELECT count(*) AS n FROM pay_messages WHERE invoice_id = :i AND channel = 'portal'",
        {"i": invoice_id},
    )
    if int(existing[0].n) == 0:
        token = await _portal_link(sessionmaker, api.org_id, contact)
        opened = await api.client.post("/v1/pay/vendor-portal/session", json={"token": token})
        if opened.status_code != 200:
            raise SeedError(f"vendor portal sign-in: {opened.status_code} {opened.text}")
        portal = {PORTAL_HEADER: opened.json()["session_token"]}
        wrote = await api.client.post(
            f"/v1/pay/vendor-portal/invoices/{invoice_id}/messages",
            json={"text": PORTAL_QUESTION},
            headers=portal,
        )
        if wrote.status_code != 201:
            raise SeedError(f"vendor portal message: {wrote.status_code} {wrote.text}")
        await api.client.post("/v1/pay/vendor-portal/sign-out", headers=portal)
        await api.ok(
            "POST",
            f"/v1/pay/invoices/{invoice_id}/portal-replies",
            CLERK,
            status=201,
            json={"text": PORTAL_ANSWER},
        )
    seed.portal_token = await _portal_link(sessionmaker, api.org_id, contact)
    seed.portal_email = contact_email(contact)
    messages = await _rows(
        sessionmaker,
        api.org_id,
        "SELECT count(*) AS n FROM pay_messages WHERE invoice_id = :i AND channel = 'portal'",
        {"i": invoice_id},
    )
    return [f"pay vendor portal: one contact with access, {int(messages[0].n)} portal messages"]


# ------------------------------------------------------------------ counts


async def counts(
    sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> dict[str, Any]:
    statuses = await _rows(
        sessionmaker, org_id, "SELECT status, count(*) AS n FROM pay_invoices GROUP BY status"
    )
    tickets = await _rows(
        sessionmaker,
        org_id,
        "SELECT status, count(*) AS n FROM pay_bank_change_tickets GROUP BY status",
    )
    runs = await _rows(
        sessionmaker, org_id, "SELECT status, count(*) AS n FROM pay_payment_runs GROUP BY status"
    )
    vendors = await _rows(sessionmaker, org_id, "SELECT count(*) AS n FROM pay_vendors")
    pos = await _rows(sessionmaker, org_id, "SELECT count(*) AS n FROM pay_purchase_orders")
    return {
        "vendors": int(vendors[0].n),
        "purchase_orders": int(pos[0].n),
        "invoices": {str(r.status): int(r.n) for r in statuses},
        "tickets": {str(r.status): int(r.n) for r in tickets},
        "payment_runs": {str(r.status): int(r.n) for r in runs},
    }


# ------------------------------------------------------------------ the whole Pay seed


async def seed_pay(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    admin_email: str,
    local_dir: Path,
) -> PaySeed:
    seed = PaySeed()
    ds = build()
    ingestion = load_ingestion_settings()
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            report = await import_pay_recordings(session, org_id)
            ai_report = await import_ai_recordings(session, org_id)
    seed.lines.append(f"pay recordings: {report.imported} imported, {report.skipped} already there")
    seed.lines.append(
        f"pay coding recordings: {ai_report.imported} imported, {ai_report.skipped} already there"
    )
    people = await _people(sessionmaker, org_id, admin_email)
    async with pay_api(org_id, people) as api:
        payer = random.Random("demo-payer")  # noqa: S311 (synthetic data)
        await api.ok(
            "PUT",
            "/v1/pay/settings",
            ADMIN,
            json={
                "cooling_off_hours": DEFAULT_COOLING_OFF_HOURS,
                "payments": {
                    "payer_name": "Pairly Demo Trading LLC",
                    "payer_iban": make_iban(BANK_BY_KEY["ae_sahil"], payer),
                    "payer_bic": BANK_BY_KEY["ae_sahil"].bic,
                    # Approval by a rule: matched, low-risk AED invoices under the limit.
                    "auto_approve_low_risk": True,
                    "auto_approve_limit": RULE_LIMIT,
                    "auto_approve_currency": "AED",
                },
            },
        )
        seed.lines += await seed_coding(api, sessionmaker, org_id)
        vendors, lines = await seed_vendors(api, sessionmaker, ds)
        seed.lines += lines
        seed.lines += await seed_purchase_orders(api, sessionmaker, ds, vendors)
        history_people = HistoryPeople(clerk=people[CLERK][0], finance=people[FINANCE][0])
        seed.lines += await seed_history(
            sessionmaker, org_id, ds, vendors, ingestion, history_people
        )

        ids: dict[str, str] = {}
        for target in TARGETS:
            ids[target.doc_id] = await drive(
                api, sessionmaker, ds.doc(target.doc_id), target.status
            )
        paid = await payment_run(api, sessionmaker, [ids[d] for d in PAID], pay=True)
        pending = await payment_run(api, sessionmaker, [ids[d] for d in SCHEDULED], pay=False)
        seed.lines.append(f"pay payment runs: one {paid}, one {pending}")

        by_synth = {v.id: v for v in ds.vendors}
        waiting = await ticket(
            api, sessionmaker, by_synth[TICKET_WAITING], vendors[TICKET_WAITING], cooling=False
        )
        cooling = await ticket(
            api, sessionmaker, by_synth[TICKET_COOLING], vendors[TICKET_COOLING], cooling=True
        )
        seed.lines.append(f"pay bank-change tickets: one {waiting}, one {cooling}")
        seed.lines += await seed_portal(api, sessionmaker, ds, vendors, ids[PORTAL_DOC], seed)

    created = await received_invoice(
        sessionmaker, org_id, ds.doc(RECEIVED), ingestion, people[CLERK]
    )
    seed.lines.append(f"pay received invoice: {'created' if created else 'already there'}")
    seed.address, seed.emails = await demo_emails(
        sessionmaker, org_id, ds, ingestion.inbound_email_domain, local_dir / "inbound-demo"
    )
    seed.counts = await counts(sessionmaker, org_id)
    seed.lines.append(
        "pay invoices by status: "
        + json.dumps(dict(Counter(seed.counts["invoices"]).most_common()), sort_keys=True)
    )
    return seed


def next_steps(seed: PaySeed, web_url: str) -> list[str]:
    files = " ".join(str(p) for p in seed.emails)
    first = seed.emails[0] if seed.emails else Path("<file>")
    drop = first.parent.parent / "inbound"
    return [
        "Pay demo:",
        f"  - The Pay inbound address (shown once, rotated by this seed): {seed.address}",
        f"  - The three demo emails, addressed to it, are ready: {files}",
        f"    Deliver them one by one with make inbound-drop FILE={first} (and the other",
        f"    two), or copy them into {drop}/ for the worker to pick up within a",
        "    minute. Then sign in as the AP clerk (demo-pay-clerk@example.test) and open",
        f"    {web_url.rstrip('/')}/en/pay/invoices.",
        "  - Invoice 1 (clean) reads to ready, matched to purchase order PO-2026-0079 and",
        "    its goods receipt (3-way). Send it for approval as the AP clerk: the rule",
        f"    approves it (matched, low risk, under AED {RULE_LIMIT}). Then schedule it:",
        "    Payment runs, propose a run (AED) that includes it.",
        "  - Invoice 2 is flagged as a near duplicate with the original side by side;",
        "    invoice 3 is blocked: new bank details open a change ticket and hold the",
        "    vendor, with the lookalike sender domain and the hidden text found.",
        "  - Purchase orders: the demo POs and goods receipts are on the Purchase orders",
        "    page (import more from CSV there).",
        "  - Bank-detail changes: one waits for its call-back (verify it as the AP clerk",
        "    on the number on file, then approve as the approver and the finance",
        "    controller); one is cooling off after two approvals.",
        "  - Payment runs: one awaits the finance controller (demo-pay-finance@example.test);",
        "    one was approved, exported and marked paid.",
        "  - Approvals: the approver (demo-pay-approver@example.test) has an invoice waiting.",
        f"  - Vendor portal: {seed.portal_email} has access. Sign in as that vendor contact",
        f"    with {web_url.rstrip('/')}/en/vendor-portal/login#{seed.portal_token}",
        "    (works once, for 3 days): invoice statuses, a question and its answer, details,",
        "    contacts (read only) and a bank change request (held, never applied).",
    ]
