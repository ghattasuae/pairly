# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Vendor portal routes (brief 4.7, 5.1 item 11), under ``/v1/pay/vendor-portal``.

A vendor's contacts are not staff: these routes never read ``Authorization`` (a staff
JWT or API key does nothing here), and the portal session never works on a staff route
or on the candidate portal. The web app keeps the session in an HTTP-only cookie scoped
to the vendor portal and sends it in the ``X-Pairly-Vendor-Portal`` header
(``pairly_pay.portal.auth``).

Without a session (``Cache-Control: no-store``, rate limited per IP):

* ``POST /vendor-portal/login-link`` ``{"org", "email", "locale"}`` (``org``: the
  organisation id every portal link names, 32 hex characters): 202 whatever the address,
  so nobody learns who has access. When the organisation has an active portal contact
  with that address, a magic link goes to it (30 minutes, single use). 10 per hour per
  IP, 5 per hour per address.
* ``POST /vendor-portal/session`` ``{"token"}``: uses a magic link once; returns the
  portal session token (8 hours). 30 per hour per IP. Any link that is not valid: 404.

With a session (``X-Pairly-Vendor-Portal``; 401 without a valid one; 120 requests a
minute per contact; writes have their own limits):

* ``GET  /vendor-portal/me``                       organisation, vendor and contact
* ``POST /vendor-portal/sign-out``                 ends the session
* ``GET  /vendor-portal/invoices``                 invoices and credit notes with status,
  dates and expected payment dates
* ``GET  /vendor-portal/invoices/{id}``            one, with its messages and the
  decisions about it the vendor may ask a person to review (``decisions``)
* ``POST /vendor-portal/invoices``                 multipart ``file`` plus
  ``document_type`` (``invoice`` or ``credit_note``) and, for a credit note,
  ``credit_for`` (one of the vendor's unpaid invoices): through Pay intake (source
  ``vendor_portal``), the same checks as any invoice. 20 per hour.
* ``POST /vendor-portal/invoices/{id}/messages``   ``{"text"}``: to accounts payable.
  30 per hour.
* ``POST /vendor-portal/invoices/{id}/appeals``    ``{"decision_id", "reason"}``: a
  human review of one of those decisions (a duplicate flag, a rejection, a bank-detail
  change step), as an appeal with the vendor as the requester; the Pay admins are told
  with its deadline (``pairly_pay.portal.appeals``). Once per decision; 10 per day.
* ``GET  /vendor-portal/details``                  the verified details (bank account
  masked) and the open change requests
* ``PUT  /vendor-portal/details/contacts``         never changes anything: the people
  at the vendor and the number on file are accounts payable's to change (AP writes to
  those addresses and calls that number). A different contact list is refused (403
  ``contacts_by_ap_only``, security review 2026-10, L-10), a different
  ``phone_on_file`` too (403 ``phone_on_file_by_ap_only``, H-01); what is on file
  already is a no-op that returns the details. 10 per hour.
* ``POST /vendor-portal/bank-change``              new bank details: always a locked
  change request (payments held, call back on the number on file, two approvals), never
  applied here; the approvers and finance controllers get the same urgent alert as for
  a ticket an invoice check opens (``pairly_pay.alerts``). 5 per day.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pairly_api.problems import problem_response
from pairly_api.routes.approvals import get_runtime
from pairly_api.uploads import (
    MULTIPART_OVERHEAD,
    UnsupportedBodyError,
    enqueue_scan,
    get_services,
    receive_upload_with_fields,
    rejected_response,
)
from pairly_core.crypto.blind_index import blind_index_candidates, normalize_email
from pairly_core.db import set_tenant
from pairly_core.errors import BadRequestError, ConflictError
from pairly_core.identity import RequestSession
from pairly_core.ingestion.models import DocumentSource
from pairly_core.ingestion.service import AuditBy, RejectReason, TooLargeError, ingest_spooled
from pairly_core.models import Organisation
from pairly_core.notifications.service import Recipient, notify
from pairly_core.ratelimit import window_limiters
from pairly_core.rbac import authorize
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_pay.api.onboarding import _Window, client_ip
from pairly_pay.api.schemas import PayBankIn, PayContactIn
from pairly_pay.api.vendors import contacts_json
from pairly_pay.common import WORKFLOW
from pairly_pay.intake import pay_policy
from pairly_pay.invoice_start import start_after_intake
from pairly_pay.invoices import create_invoice
from pairly_pay.models import DocumentType, InvoiceSource, PayVendor
from pairly_pay.portal import appeals, auth, service
from pairly_pay.portal.contacts import contact_email, contact_name
from pairly_pay.portal.models import ContactStatus, PayPortalContact
from pairly_pay.portal.templates import LOGIN_LINK
from pairly_pay.vendors import BankInput, check_bank

log = structlog.get_logger("pairly_pay.api.vendor_portal")

router = APIRouter(prefix="/vendor-portal", tags=["pay-vendor-portal"])

PORTAL_HEADER: Final = "X-Pairly-Vendor-Portal"
NO_STORE: Final = {"Cache-Control": "no-store"}
DEFAULT_WEB_URL: Final = "http://localhost:3000"
# (requests, seconds): per IP before sign-in, per contact after.
LIMITS: Final = {
    "link_ip": (10, 3600.0),
    "link_email": (5, 3600.0),
    "session_ip": (30, 3600.0),
    "read": (120, 60.0),
    "submit": (20, 3600.0),
    "message": (30, 3600.0),
    "update": (10, 3600.0),
    "bank": (5, 86400.0),
    "appeal": (10, 86400.0),
}
UPLOAD_FIELDS: Final = frozenset({"document_type", "credit_for"})

Runtime = Annotated[WorkflowRuntime, Depends(get_runtime)]


async def _hit(request: Request, kind: str, key: str) -> None:
    """Shared limits (Valkey, with this process's window as the fallback; security
    review 2026-10, L-03)."""
    store = window_limiters(
        request, "pay_vendor_portal_limiters", LIMITS, "pay.vendor_portal", local=_Window
    )
    await store[kind].hit(key)


def web_url(request: Request) -> str:
    settings = getattr(request.app.state, "notifications", None)
    url = getattr(settings, "public_web_url", None)
    if isinstance(url, str) and url:
        return url.rstrip("/")
    return os.environ.get("PAIRLY_PUBLIC_WEB_URL", DEFAULT_WEB_URL).rstrip("/")


async def portal_identity(request: Request, session: RequestSession) -> auth.PortalIdentity:
    """The contact behind the ``X-Pairly-Vendor-Portal`` header (401 otherwise); the
    database session is bound to their organisation and vendor (both RLS layers)."""
    identity = await auth.authenticate(session, request.headers.get(PORTAL_HEADER))
    await _hit(request, "read", str(identity.contact_id))
    return identity


Portal = Annotated[auth.PortalIdentity, Depends(portal_identity)]


def _no_store(body: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=NO_STORE)


# ------------------------------------------------------------------ schemas


class PayPortalLinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    email: Annotated[str, StringConstraints(max_length=320)]
    locale: Literal["en", "ar"] = "en"


class PayPortalLinkOut(BaseModel):
    sent: bool


class PayPortalTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, StringConstraints(min_length=20, max_length=240)]


class PayPortalSessionOut(BaseModel):
    session_token: str
    expires_at: datetime
    locale: str


class PayPortalMeOut(BaseModel):
    organisation: str
    org_id: str
    vendor_name: str
    vendor_reference: str
    contact_name: str
    email: str
    locale: str


class PayPortalStepOut(BaseModel):
    step: str
    at: datetime | None


class PayPortalInvoiceOut(BaseModel):
    id: uuid.UUID
    reference: str
    document_type: str
    invoice_number: str | None
    status: str
    invoice_date: date | None
    due_date: date | None
    expected_payment_date: date | None
    currency: str | None
    total: Decimal | None
    credit_total: Decimal
    amount_to_pay: Decimal | None
    received_at: datetime
    steps: list[PayPortalStepOut]
    credit_for: str | None
    payment_reference: str | None
    can_message: bool
    messages: int


class PayPortalMessageOut(BaseModel):
    id: uuid.UUID
    from_vendor: bool
    author: str
    text: str
    at: datetime


class PayPortalAppealOut(BaseModel):
    id: uuid.UUID
    reference: str
    status: str
    outcome: str | None
    sla_due_at: datetime
    created_at: datetime


class PayPortalDecisionOut(BaseModel):
    decision_id: str
    kind: Literal["duplicate_flag", "rejection", "bank_change"]
    decided_at: datetime
    can_appeal: bool
    appeal: PayPortalAppealOut | None


class PayPortalInvoiceDetailOut(PayPortalInvoiceOut):
    thread: list[PayPortalMessageOut]
    decisions: list[PayPortalDecisionOut]


class PayPortalAppealIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class PayPortalSubmittedOut(BaseModel):
    invoice: PayPortalInvoiceOut
    duplicate: bool


class PayPortalMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class PayPortalAccountOut(BaseModel):
    iban_masked: str
    account_name: str
    bank_name: str | None
    swift: str | None
    country: str
    since: datetime | None


class PayPortalRequestOut(BaseModel):
    reference: str
    status: str
    source: str
    iban_last4: str | None
    created_at: datetime


class PayPortalContactOut(BaseModel):
    name: str
    role: str | None
    email: str | None
    phone: str | None


class PayPortalDetailsOut(BaseModel):
    legal_name: str
    legal_name_ar: str | None
    trade_name: str | None
    reference: str
    country: str
    trn: str | None
    trn_status: str
    address: dict[str, Any]
    contacts: list[PayPortalContactOut]
    phone_masked: str | None
    phone_changed_at: datetime | None
    payment_hold: bool
    account: PayPortalAccountOut | None
    open_requests: list[PayPortalRequestOut]


class PayPortalContactsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contacts: list[PayContactIn] | None = None
    phone_on_file: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None
    ) = None


class PayPortalBankChangeIn(PayBankIn):
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None


class PayPortalBankChangeOut(BaseModel):
    request: PayPortalRequestOut


# ------------------------------------------------------------------ helpers


def _invoice_out(item: service.PortalInvoice) -> dict[str, Any]:
    return {
        "id": item.id,
        "reference": item.reference,
        "document_type": item.document_type,
        "invoice_number": item.invoice_number,
        "status": item.status,
        "invoice_date": item.invoice_date,
        "due_date": item.due_date,
        "expected_payment_date": item.expected_payment_date,
        "currency": item.currency,
        "total": item.total,
        "credit_total": item.credit_total,
        "amount_to_pay": item.amount_to_pay,
        "received_at": item.received_at,
        "steps": [PayPortalStepOut(step=s.step, at=s.at) for s in item.steps],
        "credit_for": item.credit_for,
        "payment_reference": item.payment_reference,
        "can_message": item.can_message,
        "messages": item.messages,
    }


def _message_out(m: service.PortalMessage) -> PayPortalMessageOut:
    return PayPortalMessageOut(
        id=m.id, from_vendor=m.from_vendor, author=m.author, text=m.text, at=m.at
    )


def _request_out(r: service.OpenRequest) -> PayPortalRequestOut:
    return PayPortalRequestOut(
        reference=r.reference,
        status=r.status,
        source=r.source,
        iban_last4=r.iban_last4,
        created_at=r.created_at,
    )


def _details_out(d: service.Details) -> PayPortalDetailsOut:
    return PayPortalDetailsOut(
        legal_name=d.legal_name,
        legal_name_ar=d.legal_name_ar,
        trade_name=d.trade_name,
        reference=d.reference,
        country=d.country,
        trn=d.trn,
        trn_status=d.trn_status,
        address=d.address,
        contacts=[
            PayPortalContactOut(
                name=str(c.get("name") or ""),
                role=c.get("role"),
                email=c.get("email"),
                phone=c.get("phone"),
            )
            for c in d.contacts
        ],
        phone_masked=d.phone_masked,
        phone_changed_at=d.phone_changed_at,
        payment_hold=d.payment_hold,
        account=PayPortalAccountOut(
            iban_masked=d.account.iban_masked,
            account_name=d.account.account_name,
            bank_name=d.account.bank_name,
            swift=d.account.swift,
            country=d.account.country,
            since=d.account.since,
        )
        if d.account is not None
        else None,
        open_requests=[_request_out(r) for r in d.open_requests],
    )


async def _detail(
    session: RequestSession, identity: auth.PortalIdentity, invoice_id: uuid.UUID
) -> PayPortalInvoiceDetailOut:
    item = await service.invoice(session, identity, invoice_id)
    messages = await service.thread(session, identity, invoice_id)
    row = await service.own_invoice(session, identity, invoice_id)
    decisions = await appeals.decisions(session, identity, row)
    return PayPortalInvoiceDetailOut(
        **_invoice_out(item),
        thread=[_message_out(m) for m in messages],
        decisions=[_decision_out(d) for d in decisions],
    )


def _decision_out(d: appeals.PortalDecision) -> PayPortalDecisionOut:
    return PayPortalDecisionOut(
        decision_id=d.decision_id,
        kind=d.kind.value,
        decided_at=d.decided_at,
        can_appeal=d.can_appeal,
        appeal=PayPortalAppealOut(
            id=d.appeal.id,
            reference=d.appeal.reference,
            status=d.appeal.status,
            outcome=d.appeal.outcome,
            sla_due_at=d.appeal.sla_due_at,
            created_at=d.appeal.created_at,
        )
        if d.appeal is not None
        else None,
    )


# ------------------------------------------------------------------ sign-in


@dataclass(frozen=True, slots=True)
class LinkJob:
    """What the login-link sender needs, decided before the answer (the same for every
    address: the address is only hashed)."""

    org_id: uuid.UUID
    email: str
    locale: str
    web_url: str


async def send_login_link(sessionmaker: Any, job: LinkJob) -> None:
    """After the answer: find an active portal contact by the address's blind index and,
    only if one exists, issue a single-use link and queue the email. For an unknown
    address this is a no-op. Runs in its own transaction; never raises into the request."""
    try:
        session = sessionmaker()
        set_tenant(session, job.org_id, None)
        async with session, session.begin():
            org = await session.scalar(select(Organisation).where(Organisation.id == job.org_id))
            if org is None or org.status != "active":
                return
            contact = await session.scalar(
                select(PayPortalContact).where(
                    PayPortalContact.email_bidx.in_(
                        blind_index_candidates("email", job.email, job.org_id)
                    ),
                    PayPortalContact.status == ContactStatus.ACTIVE.value,
                )
            )
            if contact is None:
                return
            vendor = await session.scalar(
                select(PayVendor).where(PayVendor.id == contact.vendor_id)
            )
            if vendor is None or vendor.status in auth.CLOSED_VENDOR:
                return
            token, _ = await auth.issue_login_token(
                session, contact, purpose="request", ttl=auth.REQUEST_TTL, locale=job.locale
            )
            await notify(
                session,
                recipient=Recipient.external(contact_email(contact), locale=job.locale),
                template_key=LOGIN_LINK,
                variables={
                    "org_name": org.name,
                    "vendor_name": vendor.legal_name,
                    "portal_url": f"{job.web_url}/{job.locale}/vendor-portal/login#{token}",
                    "expires_minutes": int(auth.REQUEST_TTL.total_seconds() // 60),
                },
                dedupe_key=f"pay.portal.link:{uuid.uuid4()}",
                channels=["email"],
                workflow=WORKFLOW,
            )
    except Exception:
        log.exception("vendor_portal_login_link_failed")


# Swappable for tests.
LINK_SENDER: list[Any] = [send_login_link]


@router.post("/login-link", status_code=202, response_model=PayPortalLinkOut)
async def login_link(
    body: PayPortalLinkIn, request: Request, background: BackgroundTasks
) -> JSONResponse:
    """The same answer, after the same work, whether or not the address has access: the
    request only checks the shape and schedules the sender, which looks the contact up
    after the answer is sent (``send_login_link``)."""
    await _hit(request, "link_ip", client_ip(request))
    email = normalize_email(body.email)
    if email is None:
        raise BadRequestError("please give a valid email address")
    await _hit(request, "link_email", f"{body.org}|{email}")
    org_id = auth.parse_hex_uuid(body.org)
    if org_id is not None:
        job = LinkJob(org_id=org_id, email=email, locale=body.locale, web_url=web_url(request))
        background.add_task(LINK_SENDER[0], request.app.state.pairly.sessionmaker, job)
    return _no_store({"sent": True}, 202)


@router.post("/session", response_model=PayPortalSessionOut)
async def open_session(
    body: PayPortalTokenIn, request: Request, session: RequestSession
) -> JSONResponse:
    await _hit(request, "session_ip", client_ip(request))
    opened = await auth.open_session(session, body.token)
    out = PayPortalSessionOut(
        session_token=opened.token, expires_at=opened.expires_at, locale=opened.locale
    )
    return _no_store(out.model_dump(mode="json"))


@router.get("/me", response_model=PayPortalMeOut)
async def me(identity: Portal, session: RequestSession) -> JSONResponse:
    authorize(identity.actor(), auth.PORTAL_READ)
    contact = await service.contact_row(session, identity)
    vendor = await service.vendor_row(session, identity)
    out = PayPortalMeOut(
        organisation=await service.organisation_name(session, identity.org_id),
        org_id=identity.org_id.hex,
        vendor_name=vendor.legal_name,
        vendor_reference=vendor.reference,
        contact_name=contact_name(contact),
        email=contact_email(contact),
        locale=contact.locale,
    )
    return _no_store(out.model_dump(mode="json"))


@router.post("/sign-out", status_code=204)
async def sign_out(identity: Portal, session: RequestSession) -> JSONResponse:
    await auth.sign_out(session, identity)
    return JSONResponse(None, status_code=204, headers=NO_STORE)


# ------------------------------------------------------------------ invoices


@router.get("/invoices", response_model=list[PayPortalInvoiceOut])
async def list_invoices(identity: Portal, session: RequestSession) -> JSONResponse:
    authorize(identity.actor(), auth.PORTAL_READ)
    items = await service.invoices(session, identity)
    out = [PayPortalInvoiceOut(**_invoice_out(i)) for i in items]
    return _no_store([o.model_dump(mode="json") for o in out])


@router.get("/invoices/{invoice_id}", response_model=PayPortalInvoiceDetailOut)
async def get_invoice(invoice_id: uuid.UUID, identity: Portal, session: RequestSession) -> Any:
    authorize(identity.actor(), auth.PORTAL_READ)
    detail = await _detail(session, identity, invoice_id)
    return _no_store(detail.model_dump(mode="json"))


@router.post(
    "/invoices",
    status_code=202,
    response_model=PayPortalSubmittedOut,
    responses={
        400: {"description": "Malformed body, an empty file or a wrong credit note link"},
        409: {"description": "This file was already received"},
        413: {"description": "File larger than Pay allows"},
        415: {"description": "File type not allowed, or the body is not multipart"},
    },
)
async def submit(
    request: Request,
    identity: Portal,
    session: RequestSession,
    runtime: Runtime,
    background: BackgroundTasks,
) -> Any:
    authorize(identity.actor(), auth.PORTAL_SUBMIT)
    await _hit(request, "submit", str(identity.contact_id))
    services = get_services(request)
    policy = pay_policy(services.settings)
    by = AuditBy(identity.by.actor_type, identity.by.actor_id)
    declared = request.headers.get("content-length")
    if (
        declared is not None
        and declared.isdigit()
        and int(declared) > policy.max_bytes + MULTIPART_OVERHEAD
    ):
        return rejected_response(request, RejectReason.TOO_LARGE, WORKFLOW)
    with tempfile.TemporaryDirectory(prefix="pairly-pay-portal-", dir=services.tmp_dir) as tmp:
        try:
            received = await receive_upload_with_fields(
                request,
                Path(tmp),
                policy.max_bytes,
                services.settings.upload_timeout_seconds,
                UPLOAD_FIELDS,
            )
        except UnsupportedBodyError:
            return problem_response(
                request,
                415,
                "unsupported_media_type",
                detail="send multipart/form-data with a 'file' part",
            )
        except TooLargeError:
            return rejected_response(request, RejectReason.TOO_LARGE, WORKFLOW)
        kind_raw = received.fields.get("document_type", DocumentType.INVOICE.value)
        if kind_raw not in {DocumentType.INVOICE.value, DocumentType.CREDIT_NOTE.value}:
            raise BadRequestError("document_type must be invoice or credit_note")
        kind = DocumentType(kind_raw)
        credit_for: uuid.UUID | None = None
        if kind is DocumentType.CREDIT_NOTE:
            try:
                credit_for = uuid.UUID(received.fields.get("credit_for", ""))
            except ValueError:
                raise BadRequestError("choose the invoice this credit note is for") from None
            credit_for = await service.credit_target(session, identity, credit_for)
        elif received.fields.get("credit_for"):
            raise BadRequestError("only a credit note names an invoice")
        outcome = await ingest_spooled(
            session,
            services.store,
            spooled=received.file,
            workflow=WORKFLOW,
            source=DocumentSource.UPLOAD,
            filename=received.filename,
            policy=policy,
            by=by,
        )
    if outcome.reason is not None or outcome.document is None:
        return rejected_response(request, outcome.reason or RejectReason.EMPTY, WORKFLOW)
    document = outcome.document
    try:
        async with session.begin_nested():
            invoice, created = await create_invoice(
                session,
                document=document,
                source=InvoiceSource.VENDOR_PORTAL,
                by=identity.by,
                vendor_id=identity.vendor_id,
                document_type=kind,
                credit_for_invoice_id=credit_for,
                portal_contact_id=identity.contact_id,
            )
    except IntegrityError:
        # The same file is already an invoice this vendor cannot see (another source).
        raise ConflictError("this file was already received") from None
    invoice_id, document_id = invoice.id, document.id
    await session.commit()
    if created:
        if outcome.created and runtime.services.scanner is None:
            background.add_task(enqueue_scan, services.queue, identity.org_id, document_id)
        background.add_task(start_after_intake, runtime, identity.org_id, invoice_id, document_id)
    item = await service.invoice(session, identity, invoice_id)
    body = PayPortalSubmittedOut(
        invoice=PayPortalInvoiceOut(**_invoice_out(item)), duplicate=not created
    )
    return _no_store(body.model_dump(mode="json"), 202)


@router.post(
    "/invoices/{invoice_id}/messages", status_code=201, response_model=PayPortalInvoiceDetailOut
)
async def write_message(
    invoice_id: uuid.UUID,
    body: PayPortalMessageIn,
    request: Request,
    identity: Portal,
    session: RequestSession,
) -> JSONResponse:
    authorize(identity.actor(), auth.PORTAL_MESSAGE)
    await _hit(request, "message", str(identity.contact_id))
    await service.post_message(session, identity, invoice_id, body.text)
    detail = await _detail(session, identity, invoice_id)
    return _no_store(detail.model_dump(mode="json"), 201)


@router.post(
    "/invoices/{invoice_id}/appeals",
    status_code=201,
    response_model=PayPortalInvoiceDetailOut,
    responses={
        404: {"description": "Not a decision about this invoice the vendor may appeal"},
        409: {"description": "The vendor already asked for a review of this decision"},
    },
)
async def ask_for_review(
    invoice_id: uuid.UUID,
    body: PayPortalAppealIn,
    request: Request,
    identity: Portal,
    session: RequestSession,
) -> JSONResponse:
    """A human review of a decision about the vendor's own invoice: an appeal with the
    vendor as the requester, audited, with the Pay admins told of its deadline."""
    authorize(identity.actor(), auth.APPEAL_CREATE)
    await _hit(request, "appeal", str(identity.vendor_id))
    row = await service.own_invoice(session, identity, invoice_id)
    await appeals.appeal(session, identity, row, body.decision_id, body.reason)
    detail = await _detail(session, identity, invoice_id)
    return _no_store(detail.model_dump(mode="json"), 201)


# ------------------------------------------------------------------ details


@router.get("/details", response_model=PayPortalDetailsOut)
async def get_details(identity: Portal, session: RequestSession) -> JSONResponse:
    authorize(identity.actor(), auth.PORTAL_READ)
    out = _details_out(await service.details(session, identity))
    return _no_store(out.model_dump(mode="json"))


@router.put("/details/contacts", response_model=PayPortalDetailsOut)
async def update_contacts(
    body: PayPortalContactsIn, request: Request, identity: Portal, session: RequestSession
) -> JSONResponse:
    authorize(identity.actor(), auth.PORTAL_UPDATE)
    await _hit(request, "update", str(identity.contact_id))
    fields = body.model_fields_set
    update = service.ContactsUpdate(
        contacts=contacts_json(body.contacts) if body.contacts is not None else None,
        phone_on_file=body.phone_on_file,
        change_phone="phone_on_file" in fields and body.phone_on_file is not None,
    )
    out = _details_out(await service.update_contacts(session, identity, update))
    return _no_store(out.model_dump(mode="json"))


@router.post("/bank-change", status_code=201, response_model=PayPortalBankChangeOut)
async def bank_change(
    body: PayPortalBankChangeIn, request: Request, identity: Portal, session: RequestSession
) -> JSONResponse:
    """New bank details are a request, never a change: a locked change ticket opens and
    the vendor's payments are held until AP verifies it by phone and two people approve."""
    authorize(identity.actor(), auth.PORTAL_UPDATE)
    await _hit(request, "bank", str(identity.vendor_id))
    bank = check_bank(
        BankInput(
            iban=body.iban,
            account_name=body.account_name,
            bank_name=body.bank_name,
            swift=body.swift,
        )
    )
    opened = await service.request_bank_change(session, identity, bank, body.note)
    out = PayPortalBankChangeOut(request=_request_out(opened))
    return _no_store(out.model_dump(mode="json"), 201)
