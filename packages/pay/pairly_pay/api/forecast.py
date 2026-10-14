# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Cash forecast route (brief 4.6, 4.9). Finance controllers and admins,
``pay.analytics.read``.

* ``GET /forecast?weeks=12``  per currency, the next ``weeks`` weeks (1 to 26, Monday to
  Sunday, the first one holding today and anything overdue): approved and scheduled
  invoices by expected payment date (the run's date, else the due date, else the
  invoice date), minus the credit notes applied to them, and a separate line for
  invoices still in review. What falls after the last week, what has no date and what
  has no amount are listed apart, never guessed (``pairly_pay.payables``).

Amounts are strings at the currency's minor unit; every bucket lists its invoices, so
every number on the page links to the invoices it adds up.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Query
from pydantic import BaseModel

from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import authorize
from pairly_pay.checks.settings import amount_text
from pairly_pay.payables import (
    WEEKS,
    Bucket,
    CurrencyForecast,
    PayableFact,
    amount_due,
    expected_date,
    forecast,
    load_payables,
)
from pairly_pay.permissions import ANALYTICS_READ

router = APIRouter(tags=["pay"])

MAX_WEEKS: Final = 26
MAX_LISTED: Final = 500


class PayForecastInvoiceOut(BaseModel):
    id: uuid.UUID
    reference: str
    invoice_number: str | None
    vendor_name: str | None
    status: str
    expected_date: date | None
    amount: str | None
    credits: str


class PayForecastBucketOut(BaseModel):
    amount: str
    gross: str
    credits: str
    count: int
    invoices: list[PayForecastInvoiceOut]


class PayForecastWeekOut(BaseModel):
    index: int
    start: date
    end: date
    committed: PayForecastBucketOut
    in_review: PayForecastBucketOut


class PayForecastCurrencyOut(BaseModel):
    currency: str
    committed_total: str
    in_review_total: str
    weeks: list[PayForecastWeekOut]
    later_committed: PayForecastBucketOut
    later_in_review: PayForecastBucketOut
    undated_committed: PayForecastBucketOut
    undated_in_review: PayForecastBucketOut


class PayForecastOut(BaseModel):
    generated_at: datetime
    today: date
    weeks: int
    currencies: list[PayForecastCurrencyOut]
    unpriced: list[PayForecastInvoiceOut]


def _invoice(f: PayableFact, today: date) -> PayForecastInvoiceOut:
    return PayForecastInvoiceOut(
        id=f.id,
        reference=f.reference,
        invoice_number=f.invoice_number,
        vendor_name=f.vendor_name,
        status=f.status,
        expected_date=expected_date(f, today),
        amount=amount_text(amount_due(f), f.currency),
        credits=amount_text(f.credit_total, f.currency) or "0",
    )


def _bucket(b: Bucket, currency: str, today: date) -> PayForecastBucketOut:
    return PayForecastBucketOut(
        amount=amount_text(b.amount, currency) or "0",
        gross=amount_text(b.gross, currency) or "0",
        credits=amount_text(b.credits, currency) or "0",
        count=len(b.invoices),
        invoices=[_invoice(f, today) for f in b.invoices[:MAX_LISTED]],
    )


def _currency(c: CurrencyForecast, today: date) -> PayForecastCurrencyOut:
    cur = c.currency
    return PayForecastCurrencyOut(
        currency=cur,
        committed_total=amount_text(c.committed_total, cur) or "0",
        in_review_total=amount_text(c.in_review_total, cur) or "0",
        weeks=[
            PayForecastWeekOut(
                index=w.index,
                start=w.start,
                end=w.end,
                committed=_bucket(w.committed, cur, today),
                in_review=_bucket(w.in_review, cur, today),
            )
            for w in c.weeks
        ],
        later_committed=_bucket(c.later_committed, cur, today),
        later_in_review=_bucket(c.later_in_review, cur, today),
        undated_committed=_bucket(c.undated_committed, cur, today),
        undated_in_review=_bucket(c.undated_in_review, cur, today),
    )


@router.get("/forecast", response_model=PayForecastOut)
async def cash_forecast(
    actor: CurrentActor,
    session: OrgSession,
    weeks: Annotated[int, Query(ge=1, le=MAX_WEEKS)] = WEEKS,
) -> PayForecastOut:
    authorize(actor, ANALYTICS_READ)
    now = datetime.now(UTC)
    today = now.date()
    result = forecast(await load_payables(session), today=today, weeks=weeks)
    return PayForecastOut(
        generated_at=now,
        today=today,
        weeks=result.weeks,
        currencies=[_currency(c, today) for c in result.currencies],
        unpriced=[_invoice(f, today) for f in result.unpriced[:MAX_LISTED]],
    )
