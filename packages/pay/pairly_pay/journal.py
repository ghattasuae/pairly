# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The generic journal export: approved invoices and payments of a period as journal
lines, one balanced entry each (Xero, QuickBooks and Zoho formats come later).

**Invoice entries** (journal ``AP_INVOICE``, dated the approval day) for invoices
approved in the period (a rejected invoice was never approved):

* one debit per invoice line at its net amount, on the line's GL account and cost
  centre (``pay_invoice_line_codings``), with the line's tax code (``VAT5`` for 5%,
  ``VAT0``, ``VAT<rate>``, or ``NOVAT``) and VAT amount;
* one debit of the invoice's VAT on ``VAT_INPUT``;
* one credit of the approved amount on ``AP_CONTROL``.

When the coded lines do not add up to the invoice (a line without a code or an amount,
or line amounts that do not match the subtotal), the entry falls back to one expense
debit of the approved amount less VAT, on the invoice's GL account when every line is
coded with that one account, else on ``SUSPENSE`` for a person to reclass. Never dropped,
never guessed into a coded account.

**Credit note entries** (journal ``AP_CREDIT``, dated the day it was applied) for credit
notes applied to an invoice in the period: the invoice entry of the credit note, reversed.
A debit of its total on ``AP_CONTROL`` (the payables it takes off the invoice it reduces),
a credit of its VAT on ``VAT_INPUT`` and credits of its net lines on their coded accounts,
with the same fallback to one line (the single coded account or ``SUSPENSE``). Its VAT
amounts are negative, so a VAT report sums them as written. A credit note that was
received but not applied is not an accounting event yet and is left out.

**Payment entries** (journal ``AP_PAYMENT``, dated the day it was marked paid) for run
items marked paid in the period: a debit on ``AP_CONTROL`` of what the payment settled
(the amount paid plus any early-payment discount), a credit on ``BANK`` of the amount
paid and a credit on ``DISCOUNT_RECEIVED`` of the discount.

``AP_CONTROL``, ``VAT_INPUT``, ``BANK``, ``DISCOUNT_RECEIVED`` and ``SUSPENSE`` are
placeholder codes the accounting system maps on import. Amounts are ``Decimal`` at the
currency's minor unit, quantised before the entry is built, so every entry's debits
equal its credits exactly (checked; an entry that would not balance raises).
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.extraction.service import load_extraction
from pairly_pay.checks.service import lines_of
from pairly_pay.checks.settings import to_minor_unit
from pairly_pay.models import (
    DocumentType,
    InvoiceStatus,
    PayInvoice,
    PayInvoiceLineCoding,
    PayPaymentRunItem,
    PayVendor,
    RunItemStatus,
)
from pairly_pay.month_end import Period

AP_CONTROL: Final = "AP_CONTROL"
VAT_INPUT: Final = "VAT_INPUT"
BANK: Final = "BANK"
DISCOUNT_RECEIVED: Final = "DISCOUNT_RECEIVED"
SUSPENSE: Final = "SUSPENSE"
INVOICE_JOURNAL: Final = "AP_INVOICE"
CREDIT_JOURNAL: Final = "AP_CREDIT"
PAYMENT_JOURNAL: Final = "AP_PAYMENT"
MAX_ENTRIES: Final = 20_000
APPROVED_STATES: Final = (
    InvoiceStatus.APPROVED.value,
    InvoiceStatus.SCHEDULED.value,
    InvoiceStatus.PAID.value,
)
CSV_HEADER: Final = (
    "entry",
    "date",
    "journal",
    "account_code",
    "cost_centre",
    "debit",
    "credit",
    "currency",
    "tax_code",
    "vat_amount",
    "vendor",
    "reference",
    "document_id",
)
_FORMULA: Final = ("=", "+", "-", "@", "\t", "\r")
ZERO: Final = Decimal(0)


class UnbalancedEntryError(ValueError):
    """An entry whose debits and credits differ (a programming error, never exported)."""


@dataclass(frozen=True, slots=True)
class CodedLine:
    gl_code: str | None
    cost_centre: str | None
    net: Decimal | None
    vat_rate: Decimal | None
    vat_amount: Decimal | None


@dataclass(frozen=True, slots=True)
class InvoiceFact:
    id: uuid.UUID
    reference: str
    document_id: uuid.UUID
    vendor_name: str | None
    currency: str
    day: date
    total: Decimal  # the approved amount
    vat: Decimal
    subtotal: Decimal | None
    lines: tuple[CodedLine, ...]


@dataclass(frozen=True, slots=True)
class PaymentFact:
    id: uuid.UUID
    reference: str
    document_id: uuid.UUID
    vendor_name: str | None
    currency: str
    day: date
    amount: Decimal
    discount: Decimal


@dataclass(frozen=True, slots=True)
class JournalLine:
    entry: int
    day: date
    journal: str
    account: str
    cost_centre: str | None
    debit: Decimal
    credit: Decimal
    currency: str
    tax_code: str | None
    vat_amount: Decimal | None
    vendor: str | None
    reference: str
    document_id: uuid.UUID


def tax_code(rate: Decimal | None) -> str | None:
    if rate is None:
        return None
    if rate == 0:
        return "VAT0"
    return "VAT" + format(rate.normalize(), "f")


def _check(lines: Sequence[JournalLine]) -> None:
    debit = sum((x.debit for x in lines), ZERO)
    credit = sum((x.credit for x in lines), ZERO)
    if debit != credit:
        raise UnbalancedEntryError(f"entry {lines[0].entry} debits {debit} credits {credit}")


def invoice_entry(
    entry: int, f: InvoiceFact, *, journal: str = INVOICE_JOURNAL, reverse: bool = False
) -> list[JournalLine]:
    """The balanced entry of an invoice; ``reverse`` swaps every debit and credit and
    negates the VAT amounts (a credit note)."""

    def q(value: Decimal) -> Decimal:
        return to_minor_unit(value, f.currency)

    total, vat = q(f.total), q(f.vat)
    expense = total - vat

    def line(account: str, debit: Decimal, credit: Decimal, **kw: object) -> JournalLine:
        vat_amount: Decimal | None = kw.get("vat_amount")  # type: ignore[assignment]
        if reverse:
            debit, credit = credit, debit
            vat_amount = -vat_amount if vat_amount else vat_amount
        return JournalLine(
            entry=entry,
            day=f.day,
            journal=journal,
            account=account,
            cost_centre=kw.get("cost_centre"),  # type: ignore[arg-type]
            debit=debit,
            credit=credit,
            currency=f.currency,
            tax_code=kw.get("tax_code"),  # type: ignore[arg-type]
            vat_amount=vat_amount,
            vendor=f.vendor_name,
            reference=f.reference,
            document_id=f.document_id,
        )

    out: list[JournalLine] = []
    complete = bool(f.lines) and all(c.gl_code and c.net is not None for c in f.lines)
    nets = [q(c.net) for c in f.lines if c.net is not None]
    if complete and sum(nets, ZERO) == expense:
        for c in f.lines:
            if c.net is None or not c.gl_code or q(c.net) == 0:
                continue  # never: the lines are complete
            net = q(c.net)
            out.append(
                line(
                    c.gl_code,
                    net if net > 0 else ZERO,
                    -net if net < 0 else ZERO,
                    cost_centre=c.cost_centre,
                    tax_code=tax_code(c.vat_rate) or ("NOVAT" if vat == 0 else None),
                    vat_amount=q(c.vat_amount) if c.vat_amount is not None else None,
                )
            )
    else:
        codes = {c.gl_code for c in f.lines if c.gl_code}
        centres = {c.cost_centre for c in f.lines if c.gl_code}
        every_coded = bool(f.lines) and all(c.gl_code for c in f.lines)
        account = codes.pop() if every_coded and len(codes) == 1 else SUSPENSE
        centre = centres.pop() if account != SUSPENSE and len(centres) == 1 else None
        out.append(
            line(
                account,
                expense if expense > 0 else ZERO,
                -expense if expense < 0 else ZERO,
                cost_centre=centre,
                tax_code=None if vat else "NOVAT",
            )
        )
    if vat:
        out.append(line(VAT_INPUT, vat, ZERO, tax_code="VAT", vat_amount=vat))
    out.append(line(AP_CONTROL, ZERO, total))
    _check(out)
    return out


def credit_entry(entry: int, f: InvoiceFact) -> list[JournalLine]:
    """The entry of an applied credit note: its invoice entry reversed."""
    return invoice_entry(entry, f, journal=CREDIT_JOURNAL, reverse=True)


def payment_entry(entry: int, f: PaymentFact) -> list[JournalLine]:
    amount = to_minor_unit(f.amount, f.currency)
    discount = to_minor_unit(f.discount, f.currency)

    def line(account: str, debit: Decimal, credit: Decimal) -> JournalLine:
        return JournalLine(
            entry=entry,
            day=f.day,
            journal=PAYMENT_JOURNAL,
            account=account,
            cost_centre=None,
            debit=debit,
            credit=credit,
            currency=f.currency,
            tax_code=None,
            vat_amount=None,
            vendor=f.vendor_name,
            reference=f.reference,
            document_id=f.document_id,
        )

    out = [line(AP_CONTROL, amount + discount, ZERO), line(BANK, ZERO, amount)]
    if discount:
        out.append(line(DISCOUNT_RECEIVED, ZERO, discount))
    _check(out)
    return out


def build_journal(
    invoices: Iterable[InvoiceFact],
    payments: Iterable[PaymentFact],
    credits: Iterable[InvoiceFact] = (),
) -> list[JournalLine]:
    """Invoice, then credit note, then payment entries, each by date then reference."""
    out: list[JournalLine] = []
    entry = 0
    for f in sorted(invoices, key=lambda x: (x.day, x.reference)):
        entry += 1
        out += invoice_entry(entry, f)
    for c in sorted(credits, key=lambda x: (x.day, x.reference)):
        entry += 1
        out += credit_entry(entry, c)
    for p in sorted(payments, key=lambda x: (x.day, x.reference)):
        entry += 1
        out += payment_entry(entry, p)
    return out


def _cell(value: str | None) -> str:
    text = value or ""
    return "'" + text if text.startswith(_FORMULA) else text


def _amount(value: Decimal | None, currency: str) -> str:
    return "" if value is None else format(to_minor_unit(value, currency), "f")


def to_csv(lines: Sequence[JournalLine]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for x in lines:
        writer.writerow(
            [
                str(x.entry),
                x.day.isoformat(),
                x.journal,
                _cell(x.account),
                _cell(x.cost_centre),
                _amount(x.debit, x.currency),
                _amount(x.credit, x.currency),
                x.currency,
                x.tax_code or "",
                _amount(x.vat_amount, x.currency),
                _cell(x.vendor),
                _cell(x.reference),
                str(x.document_id),
            ]
        )
    return out.getvalue().encode("utf-8")


# ------------------------------------------------------------------ loading


def _bounds(period: Period) -> tuple[datetime, datetime]:
    start = datetime.combine(period.start, time.min, tzinfo=UTC)
    return start, datetime.combine(period.end + timedelta(days=1), time.min, tzinfo=UTC)


async def _facts(session: AsyncSession, period: Period, *, credit_notes: bool) -> list[InvoiceFact]:
    start, end = _bounds(period)
    if credit_notes:
        day_column = PayInvoice.credit_applied_at
        where = (
            PayInvoice.document_type == DocumentType.CREDIT_NOTE.value,
            PayInvoice.status == InvoiceStatus.APPLIED.value,
        )
    else:
        day_column = PayInvoice.approved_at
        where = (
            PayInvoice.document_type == DocumentType.INVOICE.value,
            PayInvoice.status.in_(APPROVED_STATES),
        )
    rows = await session.execute(
        select(PayInvoice, PayVendor.legal_name)
        .outerjoin(PayVendor, PayVendor.id == PayInvoice.vendor_id)
        .where(*where, day_column >= start, day_column < end)
        .order_by(day_column, PayInvoice.id)
        .limit(MAX_ENTRIES)
    )
    out: list[InvoiceFact] = []
    for inv, vendor in rows.all():
        if credit_notes:
            currency, total, when = inv.currency, inv.total, inv.credit_applied_at
        else:
            currency = inv.approved_currency or inv.currency
            total = inv.approved_amount if inv.approved_amount is not None else inv.total
            when = inv.approved_at
        if not currency or total is None or when is None:
            continue  # an approval (or an application) always carries both
        codings = {
            c.line_index: c
            for c in await session.scalars(
                select(PayInvoiceLineCoding).where(PayInvoiceLineCoding.invoice_id == inv.id)
            )
        }
        stored = await load_extraction(session, inv.extraction_id) if inv.extraction_id else None
        lines = lines_of(stored.current_values()) if stored is not None else ()
        out.append(
            InvoiceFact(
                id=inv.id,
                reference=inv.reference,
                document_id=inv.document_id,
                vendor_name=vendor or inv.vendor_name,
                currency=currency,
                day=when.astimezone(UTC).date(),
                total=total,
                vat=inv.vat_amount or ZERO,
                subtotal=inv.subtotal,
                lines=tuple(
                    CodedLine(
                        gl_code=codings[x.index].gl_code if x.index in codings else None,
                        cost_centre=codings[x.index].cost_centre if x.index in codings else None,
                        net=x.line_total,
                        vat_rate=x.vat_rate,
                        vat_amount=x.vat_amount,
                    )
                    for x in lines
                ),
            )
        )
    return out


async def invoice_facts(session: AsyncSession, period: Period) -> list[InvoiceFact]:
    """Invoices approved in the period."""
    return await _facts(session, period, credit_notes=False)


async def credit_facts(session: AsyncSession, period: Period) -> list[InvoiceFact]:
    """Credit notes applied to an invoice in the period."""
    return await _facts(session, period, credit_notes=True)


async def payment_facts(session: AsyncSession, period: Period) -> list[PaymentFact]:
    start, end = _bounds(period)
    rows = await session.execute(
        select(PayPaymentRunItem, PayInvoice, PayVendor.legal_name)
        .join(PayInvoice, PayInvoice.id == PayPaymentRunItem.invoice_id)
        .outerjoin(PayVendor, PayVendor.id == PayPaymentRunItem.vendor_id)
        .where(
            PayPaymentRunItem.status == RunItemStatus.PAID.value,
            PayPaymentRunItem.paid_at >= start,
            PayPaymentRunItem.paid_at < end,
        )
        .order_by(PayPaymentRunItem.paid_at, PayPaymentRunItem.id)
        .limit(MAX_ENTRIES)
    )
    return [
        PaymentFact(
            id=item.id,
            reference=inv.reference,
            document_id=inv.document_id,
            vendor_name=vendor or inv.vendor_name,
            currency=item.currency,
            day=item.paid_at.astimezone(UTC).date() if item.paid_at else period.end,
            amount=item.amount,
            discount=item.discount or ZERO,
        )
        for item, inv, vendor in rows.all()
    ]


async def journal_lines(session: AsyncSession, period: Period) -> list[JournalLine]:
    return build_journal(
        await invoice_facts(session, period),
        await payment_facts(session, period),
        await credit_facts(session, period),
    )
