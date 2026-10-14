# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The exceptions queue and drafted messages (brief 4.5.4, ``pairly_pay.exceptions``).

* ``GET  /exceptions?category=&q=&limit=``   invoices waiting for a person
  (``needs_review``), oldest first, each with its categories, the explanation sentences
  of the checks, a suggested resolution, the message templates that fit, and how long it
  has waited (overdue after ``exception_sla_hours``); counts per category
  (``pay.invoice.read``).
* ``POST /invoices/{id}/messages/draft``     a drafted message from a template, with an
  optional personal paragraph from the model (``pay.message.create``). Nothing is saved.
* ``POST /invoices/{id}/messages``           save a message for approval and start the
  ``pay_message`` run (``pay.message.create``, people only). Sent only after another
  person approved the exact text in the inbox.
* ``GET  /invoices/{id}/messages``           the messages of an invoice and their status.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Final, Literal

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from pairly_core.errors import BadRequestError, ForbiddenError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.llm.errors import ModelUnavailable
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_pay.api.invoices import Runtime, money
from pairly_pay.checks.service import load_settings
from pairly_pay.common import WORKFLOW, opened
from pairly_pay.exceptions import (
    CATEGORIES,
    MAX_MESSAGE,
    age_of,
    ai_note,
    categorise,
    compose,
    fill,
    template_for,
)
from pairly_pay.invoices import get_invoice
from pairly_pay.messages import (
    ENTITY,
    GRAPH,
    MessageInput,
    create_message,
    fail_message,
    messages_of,
    vendor_contacts,
)
from pairly_pay.models import (
    InvoiceStatus,
    MessageRecipient,
    PayInvoice,
    PayMessage,
    PayPurchaseOrder,
    PayVendor,
)
from pairly_pay.permissions import INVOICE_READ, MESSAGE_CREATE

log = structlog.get_logger("pairly_pay.api.exceptions")

router = APIRouter(tags=["pay"])
MAX_SCAN: Final = 500
Category = Literal[
    "no_po",
    "price_variance",
    "quantity_variance",
    "missing_receipt",
    "duplicate_suspect",
    "bank_mismatch",
    "policy_breach",
    "low_confidence",
    "other",
]
Locale = Literal["en", "ar"]


def _recipient(kind: MessageRecipient) -> Literal["vendor", "requester"]:
    """Exception message templates only ever write to a vendor or a requester."""
    return "vendor" if kind is MessageRecipient.VENDOR else "requester"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PayTextOut(BaseModel):
    en: str
    ar: str


class PayTemplateOut(BaseModel):
    id: str
    recipient: Literal["vendor", "requester"]


class PayExceptionCategoryOut(BaseModel):
    category: Category
    explanations: list[PayTextOut]
    resolution: PayTextOut
    templates: list[PayTemplateOut]


class PayExceptionItemOut(BaseModel):
    id: uuid.UUID
    reference: str
    status: str
    vendor_name: str | None
    invoice_number: str | None
    po_number: str | None
    currency: str | None
    total: Decimal | None
    created_at: datetime
    age_hours: int
    overdue: bool
    categories: list[PayExceptionCategoryOut]


class PayExceptionsOut(BaseModel):
    items: list[PayExceptionItemOut]
    counts: dict[str, int]
    total: int
    overdue: int
    sla_hours: int
    can_message: bool


class PayDraftIn(_In):
    category: Category
    template_id: Annotated[str, Field(pattern=r"^[a-z_]{1,32}$")]
    locale: Locale = "en"
    personalise: bool = False


class PayMessageContactOut(BaseModel):
    name: str
    email: str


class PayDraftOut(BaseModel):
    category: Category
    template_id: str
    recipient: Literal["vendor", "requester"]
    locale: Locale
    message: str
    template_text: str
    ai_note: str | None
    # Why there is no personal paragraph: "model_unavailable" or a check that refused it.
    ai_problems: list[str]
    from_recording: bool
    model_call_id: uuid.UUID | None
    # Vendor: the contacts on file with an email address (the only vendor recipients).
    contacts: list[PayMessageContactOut]
    # Requester: the name on the purchase order, when there is one (a hint only).
    requester_hint: str | None


class PayMessageIn(_In):
    category: Category
    template_id: Annotated[str, Field(pattern=r"^[a-z_]{1,32}$")]
    locale: Locale = "en"
    message: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE)]
    recipient_email: Annotated[str, Field(min_length=3, max_length=254)]
    ai_note: bool = False
    model_call_id: uuid.UUID | None = None


class PayMessageOut(BaseModel):
    id: uuid.UUID
    reference: str
    invoice_id: uuid.UUID
    category: str
    template_id: str
    recipient_kind: str
    # The recipient and the text: only for people who may write messages.
    recipient_name: str | None
    recipient_email: str | None
    message: str | None
    locale: str
    ai_note: bool
    status: str
    status_reason: str | None
    approval_id: uuid.UUID | None
    created_at: datetime
    sent_at: datetime | None


def _person(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can do this")


def _message(row: PayMessage, full: bool) -> PayMessageOut:
    return PayMessageOut(
        id=row.id,
        reference=row.reference,
        invoice_id=row.invoice_id,
        category=row.category,
        template_id=row.template_id,
        recipient_kind=row.recipient_kind,
        recipient_name=str(opened(row, "recipient_name")) if full else None,
        recipient_email=(str(opened(row, "recipient_email")) if row.recipient_email else None)
        if full
        else None,
        message=str(opened(row, "message")) if full else None,
        locale=row.locale,
        ai_note=row.ai_note,
        status=row.status,
        status_reason=row.status_reason,
        approval_id=row.approval_id,
        created_at=row.created_at,
        sent_at=row.sent_at,
    )


# ------------------------------------------------------------------ the queue


@router.get("/exceptions", response_model=PayExceptionsOut)
async def list_exceptions(
    actor: CurrentActor,
    session: OrgSession,
    category: Annotated[Category | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> PayExceptionsOut:
    authorize(actor, INVOICE_READ)
    settings = await load_settings(session)
    query = select(PayInvoice).where(PayInvoice.status == InvoiceStatus.NEEDS_REVIEW.value)
    term = (q or "").strip()
    if term:
        like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(
            or_(
                PayInvoice.reference.ilike(like, escape="\\"),
                PayInvoice.invoice_number.ilike(like, escape="\\"),
                PayInvoice.vendor_name.ilike(like, escape="\\"),
            )
        )
    rows = list(
        await session.scalars(query.order_by(PayInvoice.created_at, PayInvoice.id).limit(MAX_SCAN))
    )
    now = datetime.now(UTC)
    counts = dict.fromkeys(CATEGORIES, 0)
    items: list[PayExceptionItemOut] = []
    overdue = 0
    for invoice in rows:
        found = categorise(invoice)
        for c in found:
            counts[c.key] += 1
        age = age_of(invoice, settings.exception_sla_hours, now)
        overdue += int(age.overdue)
        if category is not None and category not in {c.key for c in found}:
            continue
        if len(items) >= limit:
            continue
        items.append(
            PayExceptionItemOut(
                id=invoice.id,
                reference=invoice.reference,
                status=invoice.status,
                vendor_name=invoice.vendor_name,
                invoice_number=invoice.invoice_number,
                po_number=invoice.po_number,
                currency=invoice.currency,
                total=money(invoice.total),
                created_at=invoice.created_at,
                age_hours=age.hours,
                overdue=age.overdue,
                categories=[
                    PayExceptionCategoryOut(
                        category=c.key,  # type: ignore[arg-type]
                        explanations=[PayTextOut(en=e.en, ar=e.ar) for e in c.explanations],
                        resolution=PayTextOut(en=c.resolution[0], ar=c.resolution[1]),
                        templates=[
                            PayTemplateOut(id=t.id, recipient=_recipient(t.recipient))
                            for t in c.templates
                        ],
                    )
                    for c in found
                ],
            )
        )
    return PayExceptionsOut(
        items=items,
        counts=counts,
        total=len(rows),
        overdue=overdue,
        sla_hours=settings.exception_sla_hours,
        can_message=actor.has(MESSAGE_CREATE) and actor.kind is ActorKind.USER,
    )


# ------------------------------------------------------------------ messages


@router.post("/invoices/{invoice_id}/messages/draft", response_model=PayDraftOut)
async def draft_message(
    invoice_id: uuid.UUID,
    body: PayDraftIn,
    actor: CurrentActor,
    session: OrgSession,
    runtime: Runtime,
) -> PayDraftOut:
    """A drafted message (nothing is saved). The personal paragraph is optional and
    checked; without the model the template alone is the draft."""
    authorize(actor, MESSAGE_CREATE)
    _person(actor)
    invoice = await get_invoice(session, invoice_id)
    if body.category not in {c.key for c in categorise(invoice)}:
        raise BadRequestError("the invoice has no exception of this category")
    template = template_for(body.category, body.template_id)
    if template is None:
        raise BadRequestError("no message template of this category")
    base = fill(template, body.locale, invoice, invoice.po_number)
    note: str | None = None
    problems: list[str] = []
    recorded = False
    call_id: uuid.UUID | None = None
    router_ = runtime.services.router
    if body.personalise:
        if router_ is None:
            problems = ["model_unavailable"]
        else:
            try:
                found = await ai_note(
                    router_, actor.org_id, template, body.locale,
                    invoice_id=invoice.id, actor_id=actor.id,
                )  # fmt: skip
                note, problems = found.note, list(found.problems)
                recorded, call_id = found.from_recording, found.call_id
            except ModelUnavailable as exc:
                log.info("pay.message.model_unavailable", error=type(exc).__name__)
                problems = ["model_unavailable"]
    contacts: list[PayMessageContactOut] = []
    hint: str | None = None
    if template.recipient is MessageRecipient.VENDOR and invoice.vendor_id is not None:
        vendor = await session.scalar(select(PayVendor).where(PayVendor.id == invoice.vendor_id))
        if vendor is not None:
            contacts = [
                PayMessageContactOut(name=c.name, email=c.email) for c in vendor_contacts(vendor)
            ]
    if template.recipient is MessageRecipient.REQUESTER and invoice.po_id is not None:
        hint = await session.scalar(
            select(PayPurchaseOrder.requester).where(PayPurchaseOrder.id == invoice.po_id)
        )
    return PayDraftOut(
        category=body.category,
        template_id=template.id,
        recipient=_recipient(template.recipient),
        locale=body.locale,
        message=compose(base, note),
        template_text=base,
        ai_note=note,
        ai_problems=problems,
        from_recording=recorded,
        model_call_id=call_id if note else None,
        contacts=contacts,
        requester_hint=hint,
    )


@router.post("/invoices/{invoice_id}/messages", status_code=201, response_model=PayMessageOut)
async def submit_message(
    invoice_id: uuid.UUID,
    body: PayMessageIn,
    actor: CurrentActor,
    session: OrgSession,
    runtime: Runtime,
) -> PayMessageOut:
    """Save the message and start its approval run; it is sent only once another
    person approved this exact text."""
    authorize(actor, MESSAGE_CREATE)
    _person(actor)
    message = await create_message(
        session,
        actor,
        invoice_id,
        MessageInput(
            category=body.category,
            template_id=body.template_id,
            locale=body.locale,
            text=body.message,
            recipient_email=body.recipient_email,
            ai_note=body.ai_note,
            model_call_id=body.model_call_id if body.ai_note else None,
        ),
    )
    message_id = message.id
    await session.commit()
    try:
        await runtime.start_run(
            actor,
            workflow=WORKFLOW,
            graph=GRAPH,
            input={"message_id": str(message_id)},
            entity_type=ENTITY,
            entity_id=str(message_id),
        )
    except Exception as exc:  # the run did not start: never sent, and it says why
        log.warning("pay_message_not_started", error=type(exc).__name__)
        await fail_message(session, message_id, f"run_not_started:{type(exc).__name__}")
        await session.commit()
    row = await session.scalar(
        select(PayMessage)
        .where(PayMessage.id == message_id)
        .execution_options(populate_existing=True)
    )
    if row is None:  # pragma: no cover - just saved
        raise BadRequestError("the message is missing")
    return _message(row, True)


@router.get("/invoices/{invoice_id}/messages", response_model=list[PayMessageOut])
async def list_messages(
    invoice_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> list[PayMessageOut]:
    authorize(actor, INVOICE_READ)
    invoice = await get_invoice(session, invoice_id)
    full = actor.has(MESSAGE_CREATE)
    return [_message(row, full) for row in await messages_of(session, invoice.id)]
