# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The month-end report: what was owed, what was received but not invoiced, and what
was still waiting for a person at the end of a period (a calendar month).

**Open payables** at the period end (``as_of``, the end of its last day, or now for the
running month): invoices received by then (never credit notes) that were not paid by
then (``paid_at``) nor rejected by then (``rejected_at``). The amount is what is due:
the approved amount (the total before an approval) less the credit notes applied to it,
at the currency's minor unit. **Aging** puts each one in a bucket by days past its due
date (the invoice date when it has none) at the period end: ``current`` (not due yet),
``1_30``, ``31_60``, ``61_90``, ``90_plus``; one without either date is ``undated``,
one without an amount or currency is listed apart, never guessed.

**Accrual suggestions** (goods received not invoiced): per purchase order line, the
quantity received by the period end less the quantity invoices received by then took
(``po_match``, rejected invoices left out), valued at the PO's unit price. A suggestion
for a person to book, never a journal entry by itself. Cancelled POs are left out.

**Exceptions**: invoices received by the period end that wait for a person
(``needs_review``), by category. The queue keeps no history: this is the state now of
what arrived by the period end, and the page says so.

Amounts are ``Decimal``, never floats; every figure lists the records it adds up.
"""

from __future__ import annotations

import calendar
import csv
import io
import re
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Final

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_pay.checks.settings import to_minor_unit
from pairly_pay.exceptions import CATEGORIES, categorise
from pairly_pay.models import (
    DocumentType,
    InvoiceStatus,
    PayGoodsReceipt,
    PayGoodsReceiptLine,
    PayInvoice,
    PayPurchaseOrder,
    PayPurchaseOrderLine,
    PayVendor,
    PoStatus,
)
from pairly_pay.purchase_orders import allocation_of

BUCKETS: Final = ("current", "1_30", "31_60", "61_90", "90_plus")
UNDATED: Final = "undated"
MAX_ROWS: Final = 20_000
_PERIOD: Final = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_FORMULA: Final = ("=", "+", "-", "@", "\t", "\r")


class InvalidPeriodError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Period:
    start: date
    end: date

    @property
    def label(self) -> str:
        return f"{self.start.year:04d}-{self.start.month:02d}"

    def as_of(self, now: datetime) -> datetime:
        """The end of the period's last day, or ``now`` while the period runs."""
        end = datetime.combine(self.end, time.max, tzinfo=UTC)
        return min(end, now)


def parse_period(text: str) -> Period:
    match = _PERIOD.fullmatch(text.strip())
    if not match:
        raise InvalidPeriodError("period must be YYYY-MM")
    year, month = int(match.group(1)), int(match.group(2))
    if year < 2000 or year > 2100:
        raise InvalidPeriodError("period out of range")
    last = calendar.monthrange(year, month)[1]
    return Period(date(year, month, 1), date(year, month, last))


def previous_period(today: date) -> Period:
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return parse_period(f"{year:04d}-{month:02d}")


# ------------------------------------------------------------------ aging (pure)


@dataclass(frozen=True, slots=True)
class OpenInvoice:
    id: uuid.UUID
    reference: str
    invoice_number: str | None
    vendor_name: str | None
    status: str
    currency: str | None
    amount: Decimal | None  # what is due, at the minor unit; None without an amount
    invoice_date: date | None
    due_date: date | None


def bucket_of(invoice: OpenInvoice, period_end: date) -> str:
    due = invoice.due_date or invoice.invoice_date
    if due is None:
        return UNDATED
    days = (period_end - due).days
    if days <= 0:
        return "current"
    if days <= 30:
        return "1_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "90_plus"


@dataclass(slots=True)
class AgingBucket:
    amount: Decimal = Decimal(0)
    invoices: list[OpenInvoice] = field(default_factory=list)


@dataclass(slots=True)
class CurrencyAging:
    currency: str
    total: Decimal = Decimal(0)
    buckets: dict[str, AgingBucket] = field(
        default_factory=lambda: {k: AgingBucket() for k in (*BUCKETS, UNDATED)}
    )


@dataclass(slots=True)
class Aging:
    currencies: list[CurrencyAging]
    unpriced: list[OpenInvoice]


def age(invoices: Iterable[OpenInvoice], period_end: date) -> Aging:
    by_currency: dict[str, CurrencyAging] = {}
    unpriced: list[OpenInvoice] = []
    for inv in invoices:
        if inv.amount is None or not inv.currency:
            unpriced.append(inv)
            continue
        row = by_currency.setdefault(inv.currency, CurrencyAging(inv.currency))
        bucket = row.buckets[bucket_of(inv, period_end)]
        bucket.amount += inv.amount
        bucket.invoices.append(inv)
        row.total += inv.amount
    return Aging([by_currency[c] for c in sorted(by_currency)], unpriced)


# ------------------------------------------------------------------ accruals (pure)


@dataclass(frozen=True, slots=True)
class AccrualLine:
    po_id: uuid.UUID
    po_number: str
    vendor_name: str | None
    currency: str
    line_no: int
    description: str
    received: Decimal
    invoiced: Decimal
    unit_price: Decimal

    @property
    def quantity(self) -> Decimal:
        return self.received - self.invoiced

    @property
    def amount(self) -> Decimal:
        return to_minor_unit(self.quantity * self.unit_price, self.currency)


@dataclass(slots=True)
class CurrencyAccruals:
    currency: str
    total: Decimal = Decimal(0)
    lines: list[AccrualLine] = field(default_factory=list)


def accruals(lines: Iterable[AccrualLine]) -> list[CurrencyAccruals]:
    """Lines with more received than invoiced, per currency, largest first."""
    out: dict[str, CurrencyAccruals] = {}
    for line in lines:
        if line.quantity <= 0 or line.amount <= 0:
            continue
        row = out.setdefault(line.currency, CurrencyAccruals(line.currency))
        row.lines.append(line)
        row.total += line.amount
    for row in out.values():
        row.lines.sort(key=lambda x: (-x.amount, x.po_number, x.line_no))
    return [out[c] for c in sorted(out)]


# ------------------------------------------------------------------ loading


def _due(inv: PayInvoice) -> Decimal | None:
    gross = inv.approved_amount if inv.approved_amount is not None else inv.total
    currency = inv.approved_currency or inv.currency
    if gross is None or not currency:
        return None
    net = gross - (inv.credit_total or Decimal(0))
    return to_minor_unit(max(net, Decimal(0)), currency)


async def open_invoices(session: AsyncSession, as_of: datetime) -> list[OpenInvoice]:
    rows = await session.execute(
        select(PayInvoice, PayVendor.legal_name)
        .outerjoin(PayVendor, PayVendor.id == PayInvoice.vendor_id)
        .where(
            PayInvoice.document_type == DocumentType.INVOICE.value,
            PayInvoice.created_at <= as_of,
            or_(PayInvoice.paid_at.is_(None), PayInvoice.paid_at > as_of),
            or_(PayInvoice.rejected_at.is_(None), PayInvoice.rejected_at > as_of),
            PayInvoice.status != InvoiceStatus.APPLIED.value,
        )
        .order_by(PayInvoice.due_date.asc().nulls_last(), PayInvoice.created_at, PayInvoice.id)
        .limit(MAX_ROWS)
    )
    return [
        OpenInvoice(
            id=inv.id,
            reference=inv.reference,
            invoice_number=inv.invoice_number,
            vendor_name=vendor or inv.vendor_name,
            status=inv.status,
            currency=inv.approved_currency or inv.currency,
            amount=_due(inv),
            invoice_date=inv.invoice_date,
            due_date=inv.due_date,
        )
        for inv, vendor in rows.all()
    ]


async def accrual_lines(
    session: AsyncSession, period_end: date, as_of: datetime
) -> list[AccrualLine]:
    received: dict[tuple[uuid.UUID, int], Decimal] = defaultdict(Decimal)
    rows = await session.execute(
        select(
            PayGoodsReceiptLine.po_id, PayGoodsReceiptLine.po_line_no, PayGoodsReceiptLine.quantity
        )
        .join(PayGoodsReceipt, PayGoodsReceipt.id == PayGoodsReceiptLine.receipt_id)
        .where(PayGoodsReceipt.received_date <= period_end)
        .limit(MAX_ROWS)
    )
    for po_id, line_no, quantity in rows.all():
        received[(po_id, int(line_no))] += Decimal(quantity)
    if not received:
        return []
    po_ids = sorted({po for po, _ in received})
    invoiced: dict[tuple[uuid.UUID, int], Decimal] = defaultdict(Decimal)
    invoices = await session.scalars(
        select(PayInvoice).where(
            PayInvoice.po_id.in_(po_ids),
            PayInvoice.created_at <= as_of,
            or_(PayInvoice.rejected_at.is_(None), PayInvoice.rejected_at > as_of),
        )
    )
    for inv in invoices:
        if inv.po_id is None:
            continue
        for line_no, quantity in allocation_of(inv).items():
            invoiced[(inv.po_id, line_no)] += quantity
    lines = await session.execute(
        select(PayPurchaseOrderLine, PayPurchaseOrder, PayVendor.legal_name)
        .join(PayPurchaseOrder, PayPurchaseOrder.id == PayPurchaseOrderLine.po_id)
        .outerjoin(PayVendor, PayVendor.id == PayPurchaseOrder.vendor_id)
        .where(
            PayPurchaseOrderLine.po_id.in_(po_ids),
            PayPurchaseOrder.status != PoStatus.CANCELLED.value,
        )
    )
    out: list[AccrualLine] = []
    for line, po, vendor in lines.all():
        key = (po.id, line.line_no)
        if key not in received:
            continue
        out.append(
            AccrualLine(
                po_id=po.id,
                po_number=po.po_number,
                vendor_name=vendor,
                currency=po.currency,
                line_no=line.line_no,
                description=line.description,
                received=received[key],
                invoiced=invoiced.get(key, Decimal(0)),
                unit_price=line.unit_price,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class ExceptionInvoice:
    id: uuid.UUID
    reference: str
    vendor_name: str | None
    currency: str | None
    total: Decimal | None
    created_at: datetime
    categories: tuple[str, ...]


async def open_exceptions(session: AsyncSession, as_of: datetime) -> list[ExceptionInvoice]:
    rows = await session.scalars(
        select(PayInvoice)
        .where(
            PayInvoice.status == InvoiceStatus.NEEDS_REVIEW.value,
            PayInvoice.created_at <= as_of,
        )
        .order_by(PayInvoice.created_at, PayInvoice.id)
        .limit(MAX_ROWS)
    )
    return [
        ExceptionInvoice(
            id=inv.id,
            reference=inv.reference,
            vendor_name=inv.vendor_name,
            currency=inv.currency,
            total=inv.total,
            created_at=inv.created_at,
            categories=tuple(c.key for c in categorise(inv)),
        )
        for inv in rows
    ]


def exception_counts(items: Sequence[ExceptionInvoice]) -> dict[str, list[ExceptionInvoice]]:
    out: dict[str, list[ExceptionInvoice]] = {k: [] for k in CATEGORIES}
    for item in items:
        for key in item.categories:
            out[key].append(item)
    return out


@dataclass(frozen=True, slots=True)
class MonthEnd:
    period: Period
    as_of: datetime
    aging: Aging
    accruals: list[CurrencyAccruals]
    exceptions: list[ExceptionInvoice]


async def month_end(session: AsyncSession, period: Period, now: datetime) -> MonthEnd:
    as_of = period.as_of(now)
    return MonthEnd(
        period=period,
        as_of=as_of,
        aging=age(await open_invoices(session, as_of), period.end),
        accruals=accruals(await accrual_lines(session, period.end, as_of)),
        exceptions=await open_exceptions(session, as_of),
    )


# ------------------------------------------------------------------ CSV


CSV_HEADER: Final = (
    "section",
    "currency",
    "bucket",
    "record",
    "record_id",
    "number",
    "vendor",
    "date",
    "detail",
    "amount",
)


def _cell(value: str) -> str:
    return "'" + value if value.startswith(_FORMULA) else value


def _day(value: date | None) -> str:
    return value.isoformat() if value else ""


def _amount(value: Decimal | None, currency: str | None) -> str:
    return "" if value is None else format(to_minor_unit(value, currency), "f")


def to_csv(report: MonthEnd) -> bytes:
    """Every figure of the report with its records, one row per record (UTF-8)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)

    def row(*values: str) -> None:
        text, amount = values[:-1], values[-1]
        writer.writerow([*(_cell(v) for v in text), amount])

    for cur in report.aging.currencies:
        for key in (*BUCKETS, UNDATED):
            for inv in cur.buckets[key].invoices:
                row(
                    "open_payable",
                    cur.currency,
                    key,
                    inv.reference,
                    str(inv.id),
                    inv.invoice_number or "",
                    inv.vendor_name or "",
                    _day(inv.due_date or inv.invoice_date),
                    inv.status,
                    _amount(inv.amount, cur.currency),
                )
    for inv in report.aging.unpriced:
        row(
            "open_payable",
            "",
            "unpriced",
            inv.reference,
            str(inv.id),
            inv.invoice_number or "",
            inv.vendor_name or "",
            "",
            inv.status,
            "",
        )
    for acc in report.accruals:
        for line in acc.lines:
            row(
                "accrual",
                acc.currency,
                "",
                line.po_number,
                str(line.po_id),
                str(line.line_no),
                line.vendor_name or "",
                report.period.end.isoformat(),
                f"{line.description} ({format(line.quantity.normalize(), 'f')} x "
                f"{format(line.unit_price.normalize(), 'f')})",
                _amount(line.amount, acc.currency),
            )
    for exc in report.exceptions:
        row(
            "exception",
            exc.currency or "",
            " ".join(exc.categories),
            exc.reference,
            str(exc.id),
            "",
            exc.vendor_name or "",
            exc.created_at.date().isoformat(),
            "needs_review",
            _amount(exc.total, exc.currency),
        )
    return out.getvalue().encode("utf-8")
