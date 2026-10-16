# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""What the organisation owes: open payables and the cash forecast (brief 4.6, 4.9).

``load_payables`` reads the organisation's invoices still to pay (Row Level Security;
never a credit note, which pays nothing) with the date of the payment run each one is
in. The rest is pure:

* ``amount_due``: what will be paid on an invoice, at the currency's minor unit: the
  approved amount (the total before an approval) less the credit notes a person applied
  to it (``credit_total``, ``pay_0012``), never below zero.
* ``expected_date``: the scheduled run's execution date, else the due date, else the
  invoice date; a date already past is today (it is paid as soon as possible).
* ``forecast``: per currency, the next ``weeks`` weeks (Monday to Sunday, the first one
  holding today and anything overdue): approved and scheduled invoices by expected
  payment date, minus applied credits, and a separate line for invoices still in review
  (not yet approved, so not yet a commitment). An invoice without a date or an amount is
  counted apart, never dropped silently or given a made-up date.

Amounts are ``Decimal``, never floats; every bucket lists the invoices it adds up.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_pay.checks.settings import to_minor_unit
from pairly_pay.models import DocumentType, PayInvoice, PayPaymentRunItem, PayVendor

COMMITTED: Final = ("approved", "scheduled")
IN_REVIEW: Final = ("received", "extracting", "needs_review", "ready", "awaiting_approval")
MAX_PAYABLES: Final = 20_000
WEEKS: Final = 12
RELEASED_ITEM: Final = "released"


@dataclass(frozen=True, slots=True)
class PayableFact:
    id: uuid.UUID
    reference: str
    status: str
    currency: str | None
    total: Decimal | None
    approved_amount: Decimal | None = None
    credit_total: Decimal = Decimal(0)
    invoice_date: date | None = None
    due_date: date | None = None
    # The execution date of the active payment run item (a scheduled invoice).
    run_date: date | None = None
    vendor_name: str | None = None
    invoice_number: str | None = None

    @property
    def committed(self) -> bool:
        return self.status in COMMITTED


def gross_amount(f: PayableFact) -> Decimal | None:
    """The amount before credits: the approved amount, else the invoice total."""
    return f.approved_amount if f.approved_amount is not None else f.total


def amount_due(f: PayableFact) -> Decimal | None:
    """What will be paid: the gross amount less applied credits, at the currency's
    minor unit, never below zero. ``None`` without an amount or a currency."""
    gross = gross_amount(f)
    if gross is None or not f.currency:
        return None
    net = gross - (f.credit_total or Decimal(0))
    return to_minor_unit(max(net, Decimal(0)), f.currency)


def expected_date(f: PayableFact, today: date) -> date | None:
    """When the invoice is expected to be paid; a past date is today."""
    when = (f.run_date if f.status == "scheduled" else None) or f.due_date or f.invoice_date
    if when is None:
        return None
    return max(when, today)


async def load_payables(
    session: AsyncSession, statuses: Iterable[str] = (*COMMITTED, *IN_REVIEW)
) -> tuple[PayableFact, ...]:
    """The organisation's invoices in ``statuses`` (never credit notes), oldest due
    first, with the execution date of the payment run each scheduled one is in."""
    rows = await session.execute(
        select(PayInvoice, PayPaymentRunItem.execution_date, PayVendor.legal_name)
        .outerjoin(
            PayPaymentRunItem,
            and_(
                PayPaymentRunItem.invoice_id == PayInvoice.id,
                PayPaymentRunItem.status != RELEASED_ITEM,
            ),
        )
        .outerjoin(PayVendor, PayVendor.id == PayInvoice.vendor_id)
        .where(
            PayInvoice.document_type == DocumentType.INVOICE.value,
            PayInvoice.status.in_(list(statuses)),
        )
        .order_by(PayInvoice.due_date.asc().nulls_last(), PayInvoice.created_at, PayInvoice.id)
        .limit(MAX_PAYABLES)
    )
    return tuple(
        PayableFact(
            id=inv.id,
            reference=inv.reference,
            status=inv.status,
            currency=inv.approved_currency or inv.currency,
            total=inv.total,
            approved_amount=inv.approved_amount,
            credit_total=inv.credit_total or Decimal(0),
            invoice_date=inv.invoice_date,
            due_date=inv.due_date,
            run_date=run_date,
            vendor_name=vendor_name or inv.vendor_name,
            invoice_number=inv.invoice_number,
        )
        for inv, run_date, vendor_name in rows.all()
    )


# ------------------------------------------------------------------ forecast


@dataclass(slots=True)
class Bucket:
    """The invoices of one line in one week (or apart), and what they add up to."""

    amount: Decimal = Decimal(0)
    gross: Decimal = Decimal(0)
    credits: Decimal = Decimal(0)
    invoices: list[PayableFact] = field(default_factory=list)

    def add(self, f: PayableFact, due: Decimal) -> None:
        self.amount += due
        self.gross += gross_amount(f) or Decimal(0)
        self.credits += min(f.credit_total or Decimal(0), gross_amount(f) or Decimal(0))
        self.invoices.append(f)


@dataclass(frozen=True, slots=True)
class Week:
    index: int
    start: date
    end: date
    committed: Bucket
    in_review: Bucket


@dataclass(frozen=True, slots=True)
class CurrencyForecast:
    currency: str
    weeks: tuple[Week, ...]
    # Expected after the last week shown.
    later_committed: Bucket
    later_in_review: Bucket
    # No date to place them by (no due date, no invoice date, no run).
    undated_committed: Bucket
    undated_in_review: Bucket

    @property
    def committed_total(self) -> Decimal:
        return sum((w.committed.amount for w in self.weeks), Decimal(0))

    @property
    def in_review_total(self) -> Decimal:
        return sum((w.in_review.amount for w in self.weeks), Decimal(0))


@dataclass(frozen=True, slots=True)
class Forecast:
    today: date
    weeks: int
    currencies: tuple[CurrencyForecast, ...]
    # Invoices without an amount or a currency: listed, never guessed.
    unpriced: tuple[PayableFact, ...]


def week_start(day: date) -> date:
    """The Monday of ``day``'s week."""
    return day - timedelta(days=day.weekday())


def forecast(facts: Sequence[PayableFact], *, today: date, weeks: int = WEEKS) -> Forecast:
    if weeks < 1:
        raise ValueError("weeks must be at least 1")
    first = week_start(today)
    unpriced: list[PayableFact] = []
    per: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    currencies: set[str] = set()
    for f in facts:
        if f.status not in COMMITTED and f.status not in IN_REVIEW:
            continue
        due = amount_due(f)
        if due is None or not f.currency:
            unpriced.append(f)
            continue
        currencies.add(f.currency)
        line = "committed" if f.committed else "in_review"
        when = expected_date(f, today)
        if when is None:
            slot = "undated"
        else:
            index = (week_start(when) - first).days // 7
            slot = str(index) if index < weeks else "later"
        per[f.currency][f"{slot}:{line}"].add(f, due)
    out: list[CurrencyForecast] = []
    for currency in sorted(currencies):
        buckets = per[currency]
        out.append(
            CurrencyForecast(
                currency=currency,
                weeks=tuple(
                    Week(
                        index=i,
                        start=first + timedelta(weeks=i),
                        end=first + timedelta(weeks=i, days=6),
                        committed=buckets[f"{i}:committed"],
                        in_review=buckets[f"{i}:in_review"],
                    )
                    for i in range(weeks)
                ),
                later_committed=buckets["later:committed"],
                later_in_review=buckets["later:in_review"],
                undated_committed=buckets["undated:committed"],
                undated_in_review=buckets["undated:in_review"],
            )
        )
    return Forecast(today=today, weeks=weeks, currencies=tuple(out), unpriced=tuple(unpriced))
