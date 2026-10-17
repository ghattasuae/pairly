# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""What a vendor's contact sees and does in the vendor portal (brief 4.7, 5.1 item 11).

Every function takes the signed-in ``PortalIdentity``; the database session is already
bound to the organisation and the vendor (``pairly_pay.portal.auth.bind``), so Row Level
Security limits every query to the vendor's own rows. Each function also filters by the
vendor id itself (a second layer), so a missing policy can never widen what is shown.

* Invoices and credit notes with a portal status (received, in review, approved,
  scheduled, paid; rejected; applied for a credit note), the dates of each step, the due
  date and the expected payment date (the payment run's date once scheduled).
* Messages with accounts payable, threaded per invoice (``pay_messages``): what AP sent
  to the vendor (sent emails and portal replies) and what the vendor's contacts wrote.
* Contact details (the people at the vendor, their emails and phone numbers) are read
  only here: accounts payable writes to those addresses, so whoever holds a contact's
  mailbox (the usual foothold of a business email compromise) must not be able to add
  or redirect one. A changed list is refused (``ContactsByApOnlyError``, nothing
  written; security review 2026-10, L-10). The phone number on file never changes here
  either: it is the number accounts payable calls back to confirm a bank-detail change.
  A portal request for a new number is refused (``PhoneOnFileByApOnlyError``); accounts
  payable changes it after confirming it with the vendor on the current number (H-01).
* Bank details never change here: a request opens the locked change ticket of
  ``pairly_pay.bank_changes`` (source ``vendor_portal``), which holds payments until AP
  calls the number on file and two people approve.
* The verified details: the vendor master as AP keeps it, with the bank account masked,
  and the open change requests.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.crypto.blind_index import blind_index
from pairly_core.crypto.types import seal
from pairly_core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from pairly_core.models import Organisation
from pairly_pay.alerts import alert_portal_bank_change
from pairly_pay.bank_changes import open_ticket
from pairly_pay.common import audit, clean_text, new_reference, opened
from pairly_pay.models import (
    OPEN_TICKET_STATUSES,
    VISIBLE_TO_VENDOR,
    DocumentType,
    InvoiceStatus,
    MessageChannel,
    MessageDirection,
    MessageRecipient,
    MessageStatus,
    PayBankChangeTicket,
    PayInvoice,
    PayMessage,
    PayPaymentRunItem,
    PayVendor,
    PayVendorBankAccount,
    RunItemStatus,
    TicketSource,
)
from pairly_pay.portal.auth import PortalIdentity
from pairly_pay.portal.models import PayPortalContact
from pairly_pay.vendors import (
    CheckedBank,
    account_name_of,
    active_account,
    check_phone,
    contacts_of,
    phone_of,
)

MESSAGE_ENTITY: Final = "pay.message"
INVOICE_ENTITY: Final = "pay.invoice"
MAX_LIST: Final = 200
MAX_THREAD: Final = 200
MAX_MESSAGE: Final = 4000
MAX_CONTACTS: Final = 20
# What the portal calls each internal status.
PORTAL_STATUS: Final[Mapping[str, str]] = {
    InvoiceStatus.RECEIVED.value: "received",
    InvoiceStatus.EXTRACTING.value: "received",
    InvoiceStatus.NEEDS_REVIEW.value: "in_review",
    InvoiceStatus.READY.value: "in_review",
    InvoiceStatus.AWAITING_APPROVAL.value: "in_review",
    InvoiceStatus.APPROVED.value: "approved",
    InvoiceStatus.SCHEDULED.value: "scheduled",
    InvoiceStatus.PAID.value: "paid",
    InvoiceStatus.REJECTED.value: "rejected",
    InvoiceStatus.APPLIED.value: "applied",
}
# Invoices a credit note may name: the vendor's own, still to be paid.
CREDITABLE: Final = frozenset(
    {
        InvoiceStatus.RECEIVED.value,
        InvoiceStatus.EXTRACTING.value,
        InvoiceStatus.NEEDS_REVIEW.value,
        InvoiceStatus.READY.value,
        InvoiceStatus.AWAITING_APPROVAL.value,
        InvoiceStatus.APPROVED.value,
    }
)
# A vendor writes about an invoice until a while after it was paid or closed.
THREAD_OPEN_DAYS: Final = 90


class PortalNotFoundError(NotFoundError):
    def __init__(self) -> None:
        super().__init__("not found")


class PhoneOnFileByApOnlyError(ForbiddenError):
    """The vendor portal asked for a new phone number on file. Only accounts payable
    changes it: it is the out-of-band channel of every bank-detail change."""

    code = "phone_on_file_by_ap_only"
    title = "Only accounts payable can change the phone number on file"

    def __init__(self) -> None:
        super().__init__(
            "ask accounts payable to change the phone number on file; they confirm it "
            "with you on the current number first"
        )


class ContactsByApOnlyError(ForbiddenError):
    """The vendor portal asked to add, remove or change a contact. Only accounts payable
    changes them: AP messages go to those addresses (business email compromise)."""

    code = "contacts_by_ap_only"
    title = "Only accounts payable can change your contacts"

    def __init__(self) -> None:
        super().__init__(
            "ask accounts payable to add or change a contact; they confirm it with you first"
        )


# ------------------------------------------------------------------ who


async def contact_row(session: AsyncSession, identity: PortalIdentity) -> PayPortalContact:
    row = await session.scalar(
        select(PayPortalContact).where(
            PayPortalContact.id == identity.contact_id,
            PayPortalContact.vendor_id == identity.vendor_id,
        )
    )
    if row is None:
        raise PortalNotFoundError()
    return row


async def vendor_row(
    session: AsyncSession, identity: PortalIdentity, *, lock: bool = False
) -> PayVendor:
    query = select(PayVendor).where(PayVendor.id == identity.vendor_id)
    if lock:
        query = query.with_for_update()
    vendor = await session.scalar(query.execution_options(populate_existing=True))
    if vendor is None:
        raise PortalNotFoundError()
    return vendor


async def organisation_name(session: AsyncSession, org_id: uuid.UUID) -> str:
    name = await session.scalar(select(Organisation.name).where(Organisation.id == org_id))
    return str(name or "")


# ------------------------------------------------------------------ invoices


@dataclass(frozen=True, slots=True)
class PortalStep:
    step: str
    at: datetime | None


@dataclass(frozen=True, slots=True)
class PortalInvoice:
    id: uuid.UUID
    reference: str
    document_type: str
    invoice_number: str | None
    status: str
    invoice_date: dt.date | None
    due_date: dt.date | None
    expected_payment_date: dt.date | None
    currency: str | None
    total: Decimal | None
    credit_total: Decimal
    amount_to_pay: Decimal | None
    received_at: datetime
    steps: tuple[PortalStep, ...]
    credit_for: str | None
    payment_reference: str | None
    can_message: bool
    messages: int


def _expected(invoice: PayInvoice, item: PayPaymentRunItem | None) -> dt.date | None:
    if invoice.status == InvoiceStatus.PAID.value and invoice.paid_at is not None:
        return invoice.paid_at.date()
    if invoice.status == InvoiceStatus.SCHEDULED.value and item is not None:
        return item.execution_date or item.due_date
    if invoice.status == InvoiceStatus.APPROVED.value:
        return invoice.due_date
    return None


def _steps(invoice: PayInvoice, item: PayPaymentRunItem | None) -> tuple[PortalStep, ...]:
    """The portal's steps with the date each was reached (``None``: not yet)."""
    status = invoice.status
    if invoice.document_type == DocumentType.CREDIT_NOTE.value:
        return (
            PortalStep("received", invoice.created_at),
            PortalStep("in_review", invoice.checked_at),
            PortalStep("applied", invoice.credit_applied_at),
        )
    reviewed = (
        invoice.checked_at
        if status not in {InvoiceStatus.RECEIVED.value, InvoiceStatus.EXTRACTING.value}
        else None
    )
    return (
        PortalStep("received", invoice.created_at),
        PortalStep("in_review", reviewed),
        PortalStep("approved", invoice.approved_at),
        PortalStep("scheduled", item.created_at if item is not None else None),
        PortalStep("paid", invoice.paid_at),
    )


def _thread_open(invoice: PayInvoice, now: datetime) -> bool:
    closed = {InvoiceStatus.PAID.value, InvoiceStatus.REJECTED.value, InvoiceStatus.APPLIED.value}
    if invoice.status not in closed:
        return True
    return now - invoice.updated_at <= dt.timedelta(days=THREAD_OPEN_DAYS)


async def _items(
    session: AsyncSession, invoice_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, PayPaymentRunItem]:
    if not invoice_ids:
        return {}
    rows = await session.scalars(
        select(PayPaymentRunItem).where(
            PayPaymentRunItem.invoice_id.in_(list(invoice_ids)),
            PayPaymentRunItem.status != RunItemStatus.RELEASED.value,
        )
    )
    return {r.invoice_id: r for r in rows}


async def _message_counts(
    session: AsyncSession, identity: PortalIdentity, invoice_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    counts: dict[uuid.UUID, int] = {}
    if not invoice_ids:
        return counts
    for message in await _visible_messages(session, identity, invoice_ids):
        counts[message.invoice_id] = counts.get(message.invoice_id, 0) + 1
    return counts


def _portal_invoice(
    invoice: PayInvoice,
    item: PayPaymentRunItem | None,
    references: Mapping[uuid.UUID, str],
    messages: int,
    now: datetime,
) -> PortalInvoice:
    is_credit = invoice.document_type == DocumentType.CREDIT_NOTE.value
    to_pay = None if is_credit or invoice.total is None else invoice.total - invoice.credit_total
    return PortalInvoice(
        id=invoice.id,
        reference=invoice.reference,
        document_type=invoice.document_type,
        invoice_number=invoice.invoice_number,
        status=PORTAL_STATUS.get(invoice.status, "in_review"),
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        expected_payment_date=None if is_credit else _expected(invoice, item),
        currency=invoice.currency,
        total=invoice.total,
        credit_total=invoice.credit_total,
        amount_to_pay=to_pay,
        received_at=invoice.created_at,
        steps=_steps(invoice, item),
        credit_for=references.get(invoice.credit_for_invoice_id)
        if invoice.credit_for_invoice_id
        else None,
        payment_reference=invoice.payment_reference
        if invoice.status == InvoiceStatus.PAID.value
        else None,
        can_message=_thread_open(invoice, now),
        messages=messages,
    )


async def _own_invoices(
    session: AsyncSession, identity: PortalIdentity, ids: Sequence[uuid.UUID] | None = None
) -> list[PayInvoice]:
    query = select(PayInvoice).where(PayInvoice.vendor_id == identity.vendor_id)
    if ids is not None:
        query = query.where(PayInvoice.id.in_(list(ids)))
    return list(
        await session.scalars(
            query.order_by(PayInvoice.created_at.desc(), PayInvoice.id).limit(MAX_LIST)
        )
    )


async def _describe(
    session: AsyncSession, identity: PortalIdentity, rows: list[PayInvoice]
) -> list[PortalInvoice]:
    ids = [r.id for r in rows]
    items = await _items(session, ids)
    counts = await _message_counts(session, identity, ids)
    linked = {r.credit_for_invoice_id for r in rows if r.credit_for_invoice_id}
    known = {r.id: r.reference for r in rows}
    missing = [i for i in linked if i not in known]
    if missing:
        for row in await _own_invoices(session, identity, missing):
            known[row.id] = row.reference
    now = datetime.now(UTC)
    return [_portal_invoice(r, items.get(r.id), known, counts.get(r.id, 0), now) for r in rows]


async def invoices(session: AsyncSession, identity: PortalIdentity) -> list[PortalInvoice]:
    return await _describe(session, identity, await _own_invoices(session, identity))


async def own_invoice(
    session: AsyncSession, identity: PortalIdentity, invoice_id: uuid.UUID, *, lock: bool = False
) -> PayInvoice:
    query = select(PayInvoice).where(
        PayInvoice.id == invoice_id, PayInvoice.vendor_id == identity.vendor_id
    )
    if lock:
        query = query.with_for_update()
    invoice = await session.scalar(query)
    if invoice is None:
        raise PortalNotFoundError()
    return invoice


async def invoice(
    session: AsyncSession, identity: PortalIdentity, invoice_id: uuid.UUID
) -> PortalInvoice:
    row = await own_invoice(session, identity, invoice_id)
    return (await _describe(session, identity, [row]))[0]


async def credit_target(
    session: AsyncSession, identity: PortalIdentity, invoice_id: uuid.UUID | None
) -> uuid.UUID | None:
    """The invoice a credit note names: the vendor's own, still to be paid."""
    if invoice_id is None:
        return None
    row = await session.scalar(
        select(PayInvoice).where(
            PayInvoice.id == invoice_id, PayInvoice.vendor_id == identity.vendor_id
        )
    )
    if (
        row is None
        or row.document_type != DocumentType.INVOICE.value
        or row.status not in CREDITABLE
    ):
        raise BadRequestError("choose one of your invoices that is not paid yet")
    return row.id


# ------------------------------------------------------------------ messages


@dataclass(frozen=True, slots=True)
class PortalMessage:
    id: uuid.UUID
    invoice_id: uuid.UUID
    from_vendor: bool
    author: str
    text: str
    at: datetime


async def _visible_messages(
    session: AsyncSession, identity: PortalIdentity, invoice_ids: Sequence[uuid.UUID]
) -> list[PayMessage]:
    """The vendor's thread: what its contacts wrote, and what AP sent to the vendor
    (sent or posted; never a draft, a declined message or one to a requester)."""
    rows = await session.scalars(
        select(PayMessage)
        .where(
            PayMessage.invoice_id.in_(list(invoice_ids)),
            PayMessage.vendor_id == identity.vendor_id,
        )
        .order_by(PayMessage.created_at, PayMessage.id)
        .limit(MAX_THREAD * max(1, len(invoice_ids)))
    )
    out: list[PayMessage] = []
    for m in rows:
        inbound = m.direction == MessageDirection.INBOUND.value
        sent_to_vendor = (
            m.direction == MessageDirection.OUTBOUND.value
            and m.recipient_kind == MessageRecipient.VENDOR.value
            and m.status in VISIBLE_TO_VENDOR
        )
        if inbound or sent_to_vendor:
            out.append(m)
    return out


async def thread(
    session: AsyncSession, identity: PortalIdentity, invoice_id: uuid.UUID
) -> list[PortalMessage]:
    await own_invoice(session, identity, invoice_id)
    rows = await _visible_messages(session, identity, [invoice_id])
    contact_ids = {m.portal_contact_id for m in rows if m.portal_contact_id}
    names: dict[uuid.UUID, str] = {}
    if contact_ids:
        for c in await session.scalars(
            select(PayPortalContact).where(PayPortalContact.id.in_(list(contact_ids)))
        ):
            names[c.id] = str(opened(c, "name") or "")
    out: list[PortalMessage] = []
    for m in rows[-MAX_THREAD:]:
        inbound = m.direction == MessageDirection.INBOUND.value
        author = names.get(m.portal_contact_id, "") if inbound and m.portal_contact_id else ""
        out.append(
            PortalMessage(
                id=m.id,
                invoice_id=m.invoice_id,
                from_vendor=inbound,
                author=author,
                text=str(opened(m, "message") or ""),
                at=m.sent_at or m.created_at,
            )
        )
    return out


async def post_message(
    session: AsyncSession, identity: PortalIdentity, invoice_id: uuid.UUID, text: str
) -> PayMessage:
    """A contact writes to accounts payable about one of the vendor's invoices."""
    invoice_row = await own_invoice(session, identity, invoice_id, lock=True)
    if not _thread_open(invoice_row, datetime.now(UTC)):
        raise ConflictError("this invoice is closed: write about a newer one")
    body = clean_text(text, MAX_MESSAGE)
    if not body:
        raise BadRequestError("write a message first")
    contact = await contact_row(session, identity)
    now = datetime.now(UTC)
    message = PayMessage(
        id=uuid.uuid4(),
        org_id=identity.org_id,
        reference=new_reference("MSG"),
        invoice_id=invoice_row.id,
        category="portal",
        template_id="portal",
        recipient_kind=MessageRecipient.AP.value,
        recipient_user_id=None,
        locale=contact.locale,
        ai_note=False,
        status=MessageStatus.RECEIVED.value,
        submitted_by=None,
        channel=MessageChannel.PORTAL.value,
        direction=MessageDirection.INBOUND.value,
        vendor_id=identity.vendor_id,
        portal_contact_id=contact.id,
        created_at=now,
        updated_at=now,
    )
    message.recipient_name = seal(  # type: ignore[assignment]
        PayMessage, "recipient_name", "Accounts payable", org_id=message.org_id, row_id=message.id
    )
    message.message = seal(  # type: ignore[assignment]
        PayMessage, "message", body, org_id=message.org_id, row_id=message.id
    )
    session.add(message)
    await session.flush()
    await audit(
        session,
        identity.by,
        "pay.message.received",
        entity_type=MESSAGE_ENTITY,
        entity_id=message.id,
        payload={
            "invoice_id": str(invoice_row.id),
            "vendor_id": str(identity.vendor_id),
            "channel": message.channel,
            "length": len(body),
        },
    )
    return message


# ------------------------------------------------------------------ details


@dataclass(frozen=True, slots=True)
class MaskedAccount:
    iban_masked: str
    account_name: str
    bank_name: str | None
    swift: str | None
    country: str
    since: datetime | None


@dataclass(frozen=True, slots=True)
class OpenRequest:
    reference: str
    status: str
    source: str
    iban_last4: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Details:
    legal_name: str
    legal_name_ar: str | None
    trade_name: str | None
    reference: str
    country: str
    trn: str | None
    trn_status: str
    address: dict[str, Any]
    contacts: list[dict[str, Any]]
    phone_masked: str | None
    phone_changed_at: datetime | None
    payment_hold: bool
    account: MaskedAccount | None
    open_requests: list[OpenRequest]


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = [c for c in phone if c.isdigit()]
    return "•••• " + "".join(digits[-3:]) if len(digits) >= 3 else "••••"


def _masked(account: PayVendorBankAccount) -> MaskedAccount:
    return MaskedAccount(
        iban_masked=f"{account.country}•• •••• •••• {account.iban_last4}",
        account_name=account_name_of(account),
        bank_name=account.bank_name,
        swift=account.swift,
        country=account.country,
        since=account.activated_at,
    )


async def open_requests(session: AsyncSession, identity: PortalIdentity) -> list[OpenRequest]:
    tickets = list(
        await session.scalars(
            select(PayBankChangeTicket)
            .where(
                PayBankChangeTicket.vendor_id == identity.vendor_id,
                PayBankChangeTicket.status.in_([s.value for s in OPEN_TICKET_STATUSES]),
            )
            .order_by(PayBankChangeTicket.created_at.desc())
        )
    )
    accounts: dict[uuid.UUID, str] = {}
    ids = [t.proposed_account_id for t in tickets]
    if ids:
        for a in await session.scalars(
            select(PayVendorBankAccount).where(PayVendorBankAccount.id.in_(ids))
        ):
            accounts[a.id] = a.iban_last4
    return [
        OpenRequest(
            reference=t.reference,
            status=t.status,
            source=t.source,
            iban_last4=accounts.get(t.proposed_account_id),
            created_at=t.created_at,
        )
        for t in tickets
    ]


async def details(session: AsyncSession, identity: PortalIdentity) -> Details:
    vendor = await vendor_row(session, identity)
    account = await active_account(session, vendor.id)
    return Details(
        legal_name=vendor.legal_name,
        legal_name_ar=vendor.legal_name_ar,
        trade_name=vendor.trade_name,
        reference=vendor.reference,
        country=vendor.country,
        trn=vendor.trn,
        trn_status=vendor.trn_status,
        address=dict(vendor.address or {}),
        contacts=contacts_of(vendor),
        phone_masked=mask_phone(phone_of(vendor)),
        phone_changed_at=vendor.phone_changed_at,
        payment_hold=vendor.payment_hold,
        account=_masked(account) if account is not None else None,
        open_requests=await open_requests(session, identity),
    )


@dataclass(frozen=True, slots=True)
class ContactsUpdate:
    contacts: list[dict[str, Any]] | None
    phone_on_file: str | None
    change_phone: bool


async def update_contacts(
    session: AsyncSession, identity: PortalIdentity, update: ContactsUpdate
) -> Details:
    """Nothing the portal sends changes the vendor's contacts or its number on file.
    A different contact list (a contact added, removed or with another email or phone)
    is refused (``ContactsByApOnlyError``, L-10), and so is a different number on file
    (``PhoneOnFileByApOnlyError``, H-01): accounts payable changes both. Sending what
    is on file again is a no-op, so an older client still reads its details back."""
    vendor = await vendor_row(session, identity)
    if update.change_phone:
        phone = check_phone(update.phone_on_file)
        if phone is None:
            raise BadRequestError("phone_format")
        if blind_index("phone", phone, vendor.org_id) != vendor.phone_bidx:
            raise PhoneOnFileByApOnlyError()
    if update.contacts is not None:
        if len(update.contacts) > MAX_CONTACTS:
            raise BadRequestError(f"at most {MAX_CONTACTS} contacts")
        if update.contacts != contacts_of(vendor):
            raise ContactsByApOnlyError()
    return await details(session, identity)


async def request_bank_change(
    session: AsyncSession, identity: PortalIdentity, bank: CheckedBank, note: str | None
) -> OpenRequest:
    """Ask for new bank details. Never applied here: the locked change ticket holds the
    vendor's payments until AP calls the number on file and two people approve."""
    opened = await open_ticket(
        session,
        identity.vendor_id,
        bank,
        source=TicketSource.VENDOR_PORTAL,
        by=identity.by,
        note=clean_text(note, 1000) or None,
    )
    ticket = opened.ticket
    vendor = await vendor_row(session, identity)
    await alert_portal_bank_change(session, ticket=ticket, vendor=vendor, iban_last4=bank.last4)
    return OpenRequest(
        reference=ticket.reference,
        status=ticket.status,
        source=ticket.source,
        iban_last4=bank.last4,
        created_at=ticket.created_at,
    )
