# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Month-end report routes (``pairly_pay.month_end``), ``pay.month_end.read``.

* ``GET /month-end?period=YYYY-MM``  (default: the previous month) open payables at the
  period end with aging buckets per currency, accrual suggestions for goods received
  not invoiced, and the exceptions open among invoices received by then. Every figure
  lists its records.
* ``GET /month-end/export?period=YYYY-MM``  the same as CSV, one row per record. The
  download is audited with the file's digest (``X-Content-SHA256``).

Amounts are strings at the currency's minor unit.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel

from pairly_api.problems import problem_response
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import authorize
from pairly_pay.checks.settings import amount_text
from pairly_pay.common import audit
from pairly_pay.exceptions import CATEGORIES
from pairly_pay.month_end import (
    BUCKETS,
    UNDATED,
    AgingBucket,
    InvalidPeriodError,
    MonthEnd,
    OpenInvoice,
    Period,
    exception_counts,
    month_end,
    parse_period,
    previous_period,
    to_csv,
)
from pairly_pay.permissions import JOURNAL_EXPORT, MONTH_END_READ

router = APIRouter(tags=["pay"])

MAX_LISTED: Final = 500
REPORT_ENTITY: Final = "pay.month_end"
PeriodQuery = Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


class PayMonthEndInvoiceOut(BaseModel):
    id: uuid.UUID
    reference: str
    invoice_number: str | None
    vendor_name: str | None
    status: str
    due_date: date | None
    invoice_date: date | None
    amount: str | None


class PayAgingBucketOut(BaseModel):
    bucket: str
    amount: str
    count: int
    invoices: list[PayMonthEndInvoiceOut]


class PayAgingCurrencyOut(BaseModel):
    currency: str
    total: str
    count: int
    buckets: list[PayAgingBucketOut]


class PayAccrualLineOut(BaseModel):
    po_id: uuid.UUID
    po_number: str
    vendor_name: str | None
    line_no: int
    description: str
    received: str
    invoiced: str
    quantity: str
    unit_price: str
    amount: str


class PayAccrualCurrencyOut(BaseModel):
    currency: str
    total: str
    count: int
    lines: list[PayAccrualLineOut]


class PayMonthEndExceptionOut(BaseModel):
    id: uuid.UUID
    reference: str
    vendor_name: str | None
    currency: str | None
    total: str | None
    received_at: datetime
    categories: list[str]


class PayExceptionGroupOut(BaseModel):
    category: str
    count: int
    invoice_ids: list[uuid.UUID]


class PayMonthEndOut(BaseModel):
    period: str
    start: date
    end: date
    as_of: datetime
    aging: list[PayAgingCurrencyOut]
    unpriced: list[PayMonthEndInvoiceOut]
    accruals: list[PayAccrualCurrencyOut]
    exceptions: list[PayMonthEndExceptionOut]
    exceptions_count: int
    exception_groups: list[PayExceptionGroupOut]
    can_export_journal: bool


def _number(value: Decimal) -> str:
    """A quantity or price without trailing zeros (``"3"``, ``"412.345"``)."""
    return format(value.normalize(), "f")


def _invoice(inv: OpenInvoice) -> PayMonthEndInvoiceOut:
    return PayMonthEndInvoiceOut(
        id=inv.id,
        reference=inv.reference,
        invoice_number=inv.invoice_number,
        vendor_name=inv.vendor_name,
        status=inv.status,
        due_date=inv.due_date,
        invoice_date=inv.invoice_date,
        amount=amount_text(inv.amount, inv.currency),
    )


def _bucket(key: str, b: AgingBucket, currency: str) -> PayAgingBucketOut:
    return PayAgingBucketOut(
        bucket=key,
        amount=amount_text(b.amount, currency) or "0",
        count=len(b.invoices),
        invoices=[_invoice(i) for i in b.invoices[:MAX_LISTED]],
    )


def _out(report: MonthEnd, can_export: bool) -> PayMonthEndOut:
    groups = exception_counts(report.exceptions)
    return PayMonthEndOut(
        period=report.period.label,
        start=report.period.start,
        end=report.period.end,
        as_of=report.as_of,
        aging=[
            PayAgingCurrencyOut(
                currency=c.currency,
                total=amount_text(c.total, c.currency) or "0",
                count=sum(len(b.invoices) for b in c.buckets.values()),
                buckets=[_bucket(k, c.buckets[k], c.currency) for k in (*BUCKETS, UNDATED)],
            )
            for c in report.aging.currencies
        ],
        unpriced=[_invoice(i) for i in report.aging.unpriced[:MAX_LISTED]],
        accruals=[
            PayAccrualCurrencyOut(
                currency=a.currency,
                total=amount_text(a.total, a.currency) or "0",
                count=len(a.lines),
                lines=[
                    PayAccrualLineOut(
                        po_id=line.po_id,
                        po_number=line.po_number,
                        vendor_name=line.vendor_name,
                        line_no=line.line_no,
                        description=line.description,
                        received=_number(line.received),
                        invoiced=_number(line.invoiced),
                        quantity=_number(line.quantity),
                        unit_price=_number(line.unit_price),
                        amount=amount_text(line.amount, a.currency) or "0",
                    )
                    for line in a.lines[:MAX_LISTED]
                ],
            )
            for a in report.accruals
        ],
        exceptions=[
            PayMonthEndExceptionOut(
                id=e.id,
                reference=e.reference,
                vendor_name=e.vendor_name,
                currency=e.currency,
                total=amount_text(e.total, e.currency),
                received_at=e.created_at,
                categories=list(e.categories),
            )
            for e in report.exceptions[:MAX_LISTED]
        ],
        exceptions_count=len(report.exceptions),
        exception_groups=[
            PayExceptionGroupOut(
                category=key,
                count=len(groups[key]),
                invoice_ids=[e.id for e in groups[key][:MAX_LISTED]],
            )
            for key in CATEGORIES
            if groups[key]
        ],
        can_export_journal=can_export,
    )


def _period(raw: str | None, now: datetime) -> Period:
    return parse_period(raw) if raw else previous_period(now.date())


@router.get("/month-end", response_model=PayMonthEndOut)
async def get_month_end(
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
    period: PeriodQuery = None,
) -> Any:
    authorize(actor, MONTH_END_READ)
    now = datetime.now(UTC)
    try:
        chosen = _period(period, now)
    except InvalidPeriodError as exc:
        return problem_response(request, 400, "invalid_period", detail=str(exc))
    return _out(await month_end(session, chosen, now), actor.has(JOURNAL_EXPORT))


@router.get("/month-end/export", responses={200: {"content": {"text/csv": {}}}})
async def export_month_end(
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
    period: PeriodQuery = None,
) -> Any:
    authorize(actor, MONTH_END_READ)
    now = datetime.now(UTC)
    try:
        chosen = _period(period, now)
    except InvalidPeriodError as exc:
        return problem_response(request, 400, "invalid_period", detail=str(exc))
    content = to_csv(await month_end(session, chosen, now))
    digest = hashlib.sha256(content).hexdigest()
    await audit(
        session,
        actor,
        "pay.month_end.exported",
        entity_type=REPORT_ENTITY,
        entity_id=uuid.uuid5(uuid.NAMESPACE_URL, f"pay.month_end:{chosen.label}"),
        payload={"period": chosen.label, "sha256": digest, "bytes": len(content)},
    )
    await session.commit()
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="month-end-{chosen.label}.csv"',
            "X-Content-SHA256": digest,
            "Cache-Control": "no-store",
        },
    )
