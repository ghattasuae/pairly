# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Invoices: creation at intake (file hash dedupe), values, and a person's actions.

Status lifecycle (``InvoiceStatus``)::

    received -> extracting -> needs_review | ready -> approved -> scheduled -> paid
                                   \\-> ready (a person reviewed it)
    received | extracting | needs_review | ready -> rejected (a person, with a reason)

This step owns ``received`` to ``ready`` and the rejection. ``approved``, ``scheduled``
and ``paid`` belong to the approval and payment work packages (WP4.7). Nothing here
lets an agent move an invoice forward: the intake run only ever stops at ``ready`` or
``needs_review``, and every other move is a person's, checked server side.

Values: the invoice row keeps the current header values (``HEADER_FIELDS``) for lists
and later checks. A value counts only when it was read with evidence and confidence
(no flags) or a person corrected it; a doubtful value is left empty (fail closed).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.crypto.types import seal
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.extraction.service import StoredExtraction, load_extraction
from pairly_core.ingestion.models import Document
from pairly_core.rbac import Actor
from pairly_pay.common import By, audit, clean_text, decision_ids_of, new_reference, record_step
from pairly_pay.models import (
    InvoiceSource,
    InvoiceStatus,
    PayIntakeEmail,
    PayInvoice,
    PayVendor,
    VendorStatus,
)

INVOICE_ENTITY: Final = "pay.invoice"
REFERENCE_PREFIX: Final = "AP"
MAX_REASONS: Final = 50
MAX_REASON_TEXT: Final = 1000
# Before a person may mark an invoice reviewed, these must have a value.
REQUIRED_TO_REVIEW: Final = ("invoice_number", "invoice_date", "currency", "total")
OPEN_STATUSES: Final = frozenset(
    {
        InvoiceStatus.RECEIVED.value,
        InvoiceStatus.EXTRACTING.value,
        InvoiceStatus.NEEDS_REVIEW.value,
        InvoiceStatus.READY.value,
    }
)


class InvoiceNotFoundError(NotFoundError):
    def __init__(self) -> None:
        super().__init__("invoice not found")


class InvoiceStateError(ConflictError):
    pass


async def get_invoice(
    session: AsyncSession, invoice_id: uuid.UUID, *, lock: bool = False
) -> PayInvoice:
    query = select(PayInvoice).where(PayInvoice.id == invoice_id)
    if lock:
        query = query.with_for_update()
    invoice = await session.scalar(query.execution_options(populate_existing=True))
    if invoice is None:
        raise InvoiceNotFoundError()
    return invoice


def add_reasons(invoice: PayInvoice, reasons: Iterable[str]) -> None:
    current = list(invoice.review_reasons)
    for reason in reasons:
        text = reason[:200]
        if text and text not in current:
            current.append(text)
    invoice.review_reasons = current[:MAX_REASONS]


async def refresh_decisions(session: AsyncSession, invoice: PayInvoice) -> None:
    invoice.decision_ids = await decision_ids_of(session, INVOICE_ENTITY, invoice.id)


# ------------------------------------------------------------------ intake


async def create_invoice(
    session: AsyncSession,
    *,
    document: Document,
    source: InvoiceSource,
    by: By,
    intake_email: PayIntakeEmail | None = None,
    sender_domains: Iterable[str] = (),
    review_reasons: Iterable[str] = (),
) -> tuple[PayInvoice, bool]:
    """The invoice of ``document``, created now (``True``) or found (file hash dedupe:
    the same bytes are the same document, so they are the same invoice)."""
    if document.workflow != "pay":
        raise BadRequestError("the document belongs to another workflow")
    existing = await session.scalar(select(PayInvoice).where(PayInvoice.document_id == document.id))
    if existing is not None:
        await audit(
            session,
            by,
            "pay.invoice.duplicate_received",
            entity_type=INVOICE_ENTITY,
            entity_id=existing.id,
            payload={"source": source.value, "document_id": str(document.id)},
        )
        return existing, False
    invoice = PayInvoice(
        id=uuid.uuid4(),
        org_id=document.org_id,
        reference=new_reference(REFERENCE_PREFIX),
        status=InvoiceStatus.RECEIVED.value,
        source=source.value,
        document_id=document.id,
        intake_email_id=intake_email.id if intake_email else None,
        intake_reference=intake_email.reference if intake_email else None,
        sender_domains=list(dict.fromkeys(d for d in sender_domains if d))[:5],
        submitted_by_type=by.actor_type.value,
        submitted_by_id=by.actor_id,
        submitted_by=by.user_id,
        review_reasons=[],
        removed_spans=[],
        decision_ids=[],
        vendor_match={},
    )
    add_reasons(invoice, review_reasons)
    session.add(invoice)
    await session.flush()
    await audit(
        session,
        by,
        "pay.invoice.received",
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        payload={
            "reference": invoice.reference,
            "source": source.value,
            "document_id": str(document.id),
            "intake_email_id": str(intake_email.id) if intake_email else None,
            "review_reasons": list(invoice.review_reasons),
        },
    )
    return invoice, True


# ------------------------------------------------------------------ values


def usable_values(stored: StoredExtraction | None) -> dict[str, Any]:
    """Field values read with evidence and confidence, or corrected by a person."""
    if stored is None:
        return {}
    usable = {f.path: f.value for f in stored.fields if not f.flags and f.value is not None}
    for correction in stored.corrections:  # oldest first; a person's word wins
        if correction.new_value is None:
            usable.pop(correction.field_path, None)
        else:
            usable[correction.field_path] = correction.new_value
    return usable


def current_values(stored: StoredExtraction | None) -> dict[str, Any]:
    """Every field's current value, doubtful ones included (what a person sees)."""
    return stored.current_values() if stored is not None else {}


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def apply_values(invoice: PayInvoice, values: Mapping[str, Any]) -> None:
    """Copy the header values onto the row (empty when not usable)."""
    invoice.vendor_name = _text(values.get("vendor_name"), 200)
    trn = values.get("vendor_trn")
    invoice.vendor_trn = trn if isinstance(trn, str) and len(trn) == 15 and trn.isdigit() else None
    invoice.invoice_number = _text(values.get("invoice_number"), 64)
    invoice.invoice_date = _date(values.get("invoice_date"))
    invoice.due_date = _date(values.get("due_date"))
    currency = values.get("currency")
    invoice.currency = currency if isinstance(currency, str) and len(currency) == 3 else None
    invoice.po_number = _text(values.get("po_number"), 64)
    invoice.subtotal = _decimal(values.get("subtotal"))
    invoice.vat_amount = _decimal(values.get("vat_amount"))
    invoice.total = _decimal(values.get("total"))


async def refresh_values(session: AsyncSession, invoice: PayInvoice) -> StoredExtraction | None:
    stored = (
        await load_extraction(session, invoice.extraction_id) if invoice.extraction_id else None
    )
    apply_values(invoice, usable_values(stored))
    return stored


# ------------------------------------------------------------------ a person's actions


def _open(invoice: PayInvoice, *allowed: InvoiceStatus) -> None:
    if invoice.status not in {s.value for s in allowed}:
        raise InvoiceStateError(f"the invoice is {invoice.status}")


def _reason(text: str | None, *, required: bool) -> str | None:
    cleaned = clean_text(text, MAX_REASON_TEXT + 1)
    if len(cleaned) > MAX_REASON_TEXT:
        raise BadRequestError(f"the reason is longer than {MAX_REASON_TEXT} characters")
    if required and not cleaned:
        raise BadRequestError("a reason is required")
    return cleaned or None


async def link_vendor(
    session: AsyncSession, actor: Actor, invoice_id: uuid.UUID, vendor_id: uuid.UUID
) -> PayInvoice:
    """A person links the invoice to a vendor (no match, or a wrong one)."""
    invoice = await get_invoice(session, invoice_id, lock=True)
    _open(invoice, InvoiceStatus.NEEDS_REVIEW, InvoiceStatus.READY)
    vendor = await session.scalar(select(PayVendor).where(PayVendor.id == vendor_id))
    if vendor is None:
        raise NotFoundError("vendor not found")
    if vendor.status in {VendorStatus.ARCHIVED.value, VendorStatus.REJECTED.value}:
        raise InvoiceStateError("the vendor is closed")
    previous = invoice.vendor_id
    invoice.vendor_id = vendor.id
    invoice.vendor_match = {
        **invoice.vendor_match,
        "vendor_id": str(vendor.id),
        "method": "person",
        "status": vendor.status,
        "conflict": False,
        "linked_by": str(actor.id),
    }
    if invoice.status == InvoiceStatus.READY.value:
        # A different vendor changes what was checked: a person looks again.
        invoice.status = InvoiceStatus.NEEDS_REVIEW.value
    add_reasons(invoice, ["vendor_linked_by_person"])
    invoice.updated_at = datetime.now(UTC)
    by = By.of(actor)
    await record_step(
        session,
        by,
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        action="pay.invoice.link_vendor",
        inputs={"previous_vendor_id": str(previous) if previous else None},
        output={"vendor_id": str(vendor.id), "status": invoice.status},
    )
    await audit(
        session,
        actor,
        "pay.invoice.vendor_linked",
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        payload={
            "vendor_id": str(vendor.id),
            "previous_vendor_id": str(previous) if previous else None,
        },
    )
    await refresh_decisions(session, invoice)
    return invoice


async def mark_reviewed(
    session: AsyncSession, actor: Actor, invoice_id: uuid.UUID, note: str | None
) -> PayInvoice:
    """A person checked the invoice side by side: ``needs_review`` to ``ready``.

    Refused unless the invoice has an active vendor and the fields every later step
    needs have a value (corrected where they were doubtful)."""
    invoice = await get_invoice(session, invoice_id, lock=True)
    _open(invoice, InvoiceStatus.NEEDS_REVIEW)
    text = _reason(note, required=False)
    stored = await refresh_values(session, invoice)
    if stored is None:
        raise InvoiceStateError("nothing was read from this invoice yet")
    missing = [f for f in REQUIRED_TO_REVIEW if getattr(invoice, f) is None]
    if missing:
        raise InvoiceStateError("correct these fields first: " + ", ".join(missing))
    vendor = (
        await session.scalar(select(PayVendor).where(PayVendor.id == invoice.vendor_id))
        if invoice.vendor_id
        else None
    )
    if vendor is None or vendor.status != VendorStatus.ACTIVE.value:
        raise InvoiceStateError("link the invoice to an active vendor first")
    accepted = [f.path for f in stored.fields if f.flags]
    now = datetime.now(UTC)
    invoice.status = InvoiceStatus.READY.value
    invoice.reviewed_by = actor.id
    invoice.reviewed_at = invoice.updated_at = now
    by = By.of(actor)
    await record_step(
        session,
        by,
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        action="pay.invoice.reviewed",
        inputs={
            "extraction_id": str(invoice.extraction_id),
            "review_reasons": list(invoice.review_reasons),
            "flagged_fields_accepted": accepted[:100],
        },
        output={"status": invoice.status},
        reason=text,
    )
    await audit(
        session,
        actor,
        "pay.invoice.reviewed",
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        payload={"flagged_fields_accepted": len(accepted)},
    )
    await refresh_decisions(session, invoice)
    return invoice


async def reject_invoice(
    session: AsyncSession, actor: Actor, invoice_id: uuid.UUID, reason: str
) -> PayInvoice:
    """A person closes the invoice (not an invoice, a duplicate, a fake), with a reason."""
    invoice = await get_invoice(session, invoice_id, lock=True)
    _open(
        invoice,
        InvoiceStatus.RECEIVED,
        InvoiceStatus.EXTRACTING,
        InvoiceStatus.NEEDS_REVIEW,
        InvoiceStatus.READY,
    )
    text = _reason(reason, required=True)
    now = datetime.now(UTC)
    invoice.status = InvoiceStatus.REJECTED.value
    invoice.rejected_by = actor.id
    invoice.rejected_at = invoice.updated_at = now
    invoice.reject_reason = seal(  # type: ignore[assignment]
        PayInvoice, "reject_reason", text, org_id=invoice.org_id, row_id=invoice.id
    )
    await record_step(
        session,
        By.of(actor),
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        action="pay.invoice.rejected",
        inputs={"previous_status": "open"},
        output={"status": invoice.status},
        reason=text,
    )
    await audit(
        session,
        actor,
        "pay.invoice.rejected",
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        payload={"reference": invoice.reference},
    )
    await refresh_decisions(session, invoice)
    return invoice
