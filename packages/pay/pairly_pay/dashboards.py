# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pay role dashboards on Home (brief 5.1 items 8 to 10; the vendor portal home, item
11, is the portal's own). Each is built from the services the Pay pages already use;
nothing is stored and no number is invented: a figure without data is ``None``.

* **AP clerk**: the intake queue (being read, to review, ready to send for approval),
  invoices with low-confidence fields, exceptions by category, duplicate suspects,
  invoices waiting on a vendor (a message was sent and the invoice still waits), and
  the clerk's own throughput (reviewed and sent for approval, 7 and 30 days).
* **Approver**: Pay approvals waiting for this person, with the invoice's risk level
  and the step's SLA state; the ones that reach them through a delegation, apart; their
  decisions of the last 30 days; and the spend they approved this month, per currency.
* **Finance controller**: open payables and their aging, payment runs to come, the cash
  forecast of the next four weeks, early-payment discounts still open, the exceptions
  trend (four weeks), the analytics of the last 90 days (touchless rate, cycle time,
  cost per invoice, days payable outstanding, fraud flags and bank-change attempts
  blocked) and the status of the previous month's close.

Every item carries the app path of its record; every section counts what it lists.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.models import Approval, ApprovalDecision
from pairly_core.approvals.service import list_inbox
from pairly_core.rbac import Actor
from pairly_pay.analytics import Metric, RecordLink, compute, load_facts
from pairly_pay.checks.settings import amount_text
from pairly_pay.common import WORKFLOW
from pairly_pay.discounts import invoice_offer
from pairly_pay.exceptions import CATEGORIES, categorise
from pairly_pay.models import (
    DocumentType,
    InvoiceStatus,
    MessageDirection,
    MessageRecipient,
    MessageStatus,
    PayBankStatementLine,
    PayInvoice,
    PayJournalExport,
    PaymentRunStatus,
    PayMessage,
    PayPaymentRun,
    PayVendor,
    StatementLineStatus,
)
from pairly_pay.month_end import BUCKETS, UNDATED, OpenInvoice, age, previous_period
from pairly_pay.payables import COMMITTED, amount_due, forecast, load_payables

MAX_ITEMS: Final = 20
ANALYTICS_DAYS: Final = 90
TREND_WEEKS: Final = 4
FORECAST_WEEKS: Final = 4

Detail = dict[str, str | int | float | bool | None]


@dataclass(slots=True)
class Item:
    kind: str
    id: str
    label: str
    href: str | None
    at: datetime | None = None
    detail: Detail = field(default_factory=dict)


@dataclass(slots=True)
class Section:
    key: str
    count: int
    items: list[Item]
    href: str | None = None


@dataclass(frozen=True, slots=True)
class Amount:
    key: str
    currency: str
    amount: str
    count: int
    href: str


@dataclass(slots=True)
class Board:
    role: str
    generated_at: datetime
    action: list[Section] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    amounts: list[Amount] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


def _invoice_item(inv: PayInvoice, **detail: str | int | float | bool | None) -> Item:
    return Item(
        kind="invoice",
        id=str(inv.id),
        label=inv.reference,
        href=f"/pay/invoices/{inv.id}",
        at=inv.created_at,
        detail={
            "vendor": inv.vendor_name,
            "currency": inv.currency,
            "total": amount_text(inv.total, inv.currency),
            **detail,
        },
    )


def _section(key: str, items: Sequence[Item], href: str | None = None) -> Section:
    return Section(key=key, count=len(items), items=list(items[:MAX_ITEMS]), href=href)


def _count_metric(key: str, invoices: Sequence[PayInvoice], **detail: Any) -> Metric:
    return Metric(
        key=key,
        value=float(len(invoices)),
        unit="count",
        n=len(invoices),
        records=[RecordLink("invoice", str(i.id), i.reference) for i in invoices[:MAX_ITEMS]],
        records_total=len(invoices),
        detail=detail,
    )


# ------------------------------------------------------------------ AP clerk


async def _invoices(session: AsyncSession, *statuses: str) -> list[PayInvoice]:
    return list(
        await session.scalars(
            select(PayInvoice)
            .where(
                PayInvoice.status.in_(statuses),
                PayInvoice.document_type == DocumentType.INVOICE.value,
            )
            .order_by(PayInvoice.created_at, PayInvoice.id)
            .limit(2000)
        )
    )


async def ap_clerk(session: AsyncSession, actor: Actor, now: datetime) -> Board:
    board = Board(role="ap_clerk", generated_at=now, links=["exceptions", "invoices"])
    reading = await _invoices(session, InvoiceStatus.RECEIVED.value, InvoiceStatus.EXTRACTING.value)
    review = await _invoices(session, InvoiceStatus.NEEDS_REVIEW.value)
    ready = await _invoices(session, InvoiceStatus.READY.value)
    categories = {inv.id: [c.key for c in categorise(inv)] for inv in review}
    waiting_ids = set(
        await session.scalars(
            select(PayMessage.invoice_id).where(
                PayMessage.recipient_kind == MessageRecipient.VENDOR.value,
                PayMessage.direction == MessageDirection.OUTBOUND.value,
                PayMessage.status.in_([MessageStatus.QUEUED.value, MessageStatus.POSTED.value]),
                PayMessage.invoice_id.in_([i.id for i in review]),
            )
        )
    )
    board.action = [
        _section("to_review", [_invoice_item(i) for i in review], "/pay/exceptions"),
        _section("ready", [_invoice_item(i) for i in ready], "/pay/invoices?status=ready"),
        _section(
            "low_confidence",
            [_invoice_item(i) for i in review if "low_confidence" in categories[i.id]],
            "/pay/exceptions?category=low_confidence",
        ),
        _section(
            "duplicates",
            [_invoice_item(i) for i in review if "duplicate_suspect" in categories[i.id]],
            "/pay/exceptions?category=duplicate_suspect",
        ),
        _section(
            "waiting_on_vendor",
            [_invoice_item(i) for i in review if i.id in waiting_ids],
            "/pay/exceptions",
        ),
    ]
    by_category: dict[str, int] = dict.fromkeys(CATEGORIES, 0)
    for keys in categories.values():
        for key in keys:
            by_category[key] += 1
    board.sections = [
        _section("intake", [_invoice_item(i) for i in reading], "/pay/invoices"),
        _section(
            "exceptions_by_category",
            [
                Item(
                    kind="category",
                    id=key,
                    label=key,
                    href=f"/pay/exceptions?category={key}",
                    detail={"count": count},
                )
                for key, count in by_category.items()
                if count
            ],
            "/pay/exceptions",
        ),
    ]
    for days in (7, 30):
        since = now - timedelta(days=days)
        reviewed = list(
            await session.scalars(
                select(PayInvoice)
                .where(PayInvoice.reviewed_by == actor.id, PayInvoice.reviewed_at >= since)
                .order_by(PayInvoice.reviewed_at.desc())
            )
        )
        sent = list(
            await session.scalars(
                select(PayInvoice)
                .where(
                    PayInvoice.approval_submitted_by == actor.id,
                    PayInvoice.approval_submitted_at >= since,
                )
                .order_by(PayInvoice.approval_submitted_at.desc())
            )
        )
        board.metrics.append(_count_metric(f"reviewed_{days}d", reviewed, days=days))
        board.metrics.append(_count_metric(f"submitted_{days}d", sent, days=days))
    return board


# ------------------------------------------------------------------ approver


async def _invoices_by_id(session: AsyncSession, ids: Iterable[str]) -> dict[str, PayInvoice]:
    wanted = []
    for raw in ids:
        try:
            wanted.append(uuid.UUID(raw))
        except ValueError:
            continue
    if not wanted:
        return {}
    rows = await session.scalars(select(PayInvoice).where(PayInvoice.id.in_(wanted)))
    return {str(i.id): i for i in rows}


async def approver(session: AsyncSession, actor: Actor, now: datetime) -> Board:
    board = Board(role="approver", generated_at=now, links=["inbox"])
    page = await list_inbox(session, actor, workflow=WORKFLOW, assigned_to_me=True, limit=200)
    invoices = await _invoices_by_id(
        session,
        (
            i.approval.entity_id
            for i in page.items
            if i.approval.entity_type == "pay.invoice" and i.approval.entity_id
        ),
    )
    mine: list[Item] = []
    delegated: list[Item] = []
    for entry in page.items:
        a, step = entry.approval, entry.step
        direct = step is not None and (
            actor.id in (step.assignee_ids or []) or actor.id in (step.escalation_ids or [])
        )
        inv = invoices.get(a.entity_id or "")
        risk = inv.risk.get("level") if inv is not None and isinstance(inv.risk, dict) else None
        item = Item(
            kind="approval",
            id=str(a.id),
            label=inv.reference if inv is not None else a.title,
            href=f"/inbox/{a.id}",
            at=a.created_at,
            detail={
                "type": a.approval_type,
                "risk": str(risk) if risk else None,
                "sla": entry.sla,
                "due_at": step.due_at.isoformat() if step is not None and step.due_at else None,
                "vendor": inv.vendor_name if inv is not None else None,
                "currency": inv.currency if inv is not None else None,
                "total": amount_text(inv.total, inv.currency) if inv is not None else None,
            },
        )
        (mine if direct else delegated).append(item)
    board.action = [
        _section("awaiting_me", mine, "/inbox"),
        _section("delegated", delegated, "/inbox"),
    ]

    since = now - timedelta(days=30)
    history = await session.execute(
        select(ApprovalDecision, Approval)
        .join(Approval, Approval.id == ApprovalDecision.approval_id)
        .where(
            ApprovalDecision.actor_user_id == actor.id,
            Approval.workflow == WORKFLOW,
            ApprovalDecision.created_at >= since,
        )
        .order_by(ApprovalDecision.created_at.desc())
        .limit(MAX_ITEMS)
    )
    board.sections = [
        _section(
            "history",
            [
                Item(
                    kind="approval",
                    id=str(a.id),
                    label=a.title,
                    href=f"/inbox/{a.id}",
                    at=d.created_at,
                    detail={"decision": d.decision, "type": a.approval_type},
                )
                for d, a in history.all()
            ],
            "/inbox",
        )
    ]

    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    approved = list(
        await session.scalars(
            select(PayInvoice).where(
                PayInvoice.approved_at >= start,
                PayInvoice.approver_ids.contains([str(actor.id)]),
            )
        )
    )
    totals: dict[str, list[PayInvoice]] = defaultdict(list)
    for inv in approved:
        currency = inv.approved_currency or inv.currency
        if currency and (inv.approved_amount or inv.total) is not None:
            totals[currency].append(inv)
    for currency in sorted(totals):
        rows = totals[currency]
        spend = sum(
            (
                (i.approved_amount if i.approved_amount is not None else i.total) or Decimal(0)
                for i in rows
            ),
            Decimal(0),
        )
        board.amounts.append(
            Amount(
                "approved_this_month",
                currency,
                amount_text(spend, currency) or "0",
                len(rows),
                "/inbox",
            )
        )
    board.metrics.append(_count_metric("approved_this_month", approved))
    return board


# ------------------------------------------------------------------ finance controller


async def finance_controller(session: AsyncSession, actor: Actor, now: datetime) -> Board:
    today = now.date()
    board = Board(
        role="finance_controller",
        generated_at=now,
        links=["analytics", "forecast", "month_end", "reconciliation", "payment_runs"],
    )
    payables = await load_payables(session, COMMITTED)
    open_rows = [
        OpenInvoice(
            id=f.id,
            reference=f.reference,
            invoice_number=f.invoice_number,
            vendor_name=f.vendor_name,
            status=f.status,
            currency=f.currency,
            amount=amount_due(f),
            invoice_date=f.invoice_date,
            due_date=f.due_date,
        )
        for f in payables
    ]
    aging = age(open_rows, today)
    for cur in aging.currencies:
        board.amounts.append(
            Amount(
                "payables",
                cur.currency,
                amount_text(cur.total, cur.currency) or "0",
                sum(len(b.invoices) for b in cur.buckets.values()),
                "/pay/forecast",
            )
        )
        for key in (*BUCKETS, UNDATED):
            bucket = cur.buckets[key]
            if bucket.invoices:
                board.amounts.append(
                    Amount(
                        f"aging.{key}",
                        cur.currency,
                        amount_text(bucket.amount, cur.currency) or "0",
                        len(bucket.invoices),
                        "/pay/month-end",
                    )
                )
    plan = forecast(payables, today=today, weeks=FORECAST_WEEKS)
    for cur_forecast in plan.currencies:
        weeks_total = sum((w.committed.amount for w in cur_forecast.weeks), Decimal(0))
        board.amounts.append(
            Amount(
                "forecast_4_weeks",
                cur_forecast.currency,
                amount_text(weeks_total, cur_forecast.currency) or "0",
                sum(len(w.committed.invoices) for w in cur_forecast.weeks),
                f"/pay/forecast?weeks={FORECAST_WEEKS}",
            )
        )

    runs = await session.scalars(
        select(PayPaymentRun)
        .where(
            PayPaymentRun.status.in_(
                [
                    PaymentRunStatus.AWAITING_APPROVAL.value,
                    PaymentRunStatus.APPROVED.value,
                    PaymentRunStatus.EXPORTED.value,
                ]
            )
        )
        .order_by(PayPaymentRun.execution_date, PayPaymentRun.id)
    )
    board.action.append(
        _section(
            "upcoming_runs",
            [
                Item(
                    kind="payment_run",
                    id=str(r.id),
                    label=r.reference,
                    href=f"/pay/payment-runs/{r.id}",
                    at=r.created_at,
                    detail={
                        "status": r.status,
                        "execution_date": r.execution_date.isoformat(),
                        "currency": r.currency,
                        "total": amount_text(r.total, r.currency),
                        "items": r.item_count,
                    },
                )
                for r in runs
            ],
            "/pay/payment-runs",
        )
    )

    approved = await _invoices(session, InvoiceStatus.APPROVED.value, InvoiceStatus.SCHEDULED.value)
    terms = {
        v.id: v.payment_terms
        for v in await session.scalars(
            select(PayVendor).where(
                PayVendor.id.in_([i.vendor_id for i in approved if i.vendor_id])
            )
        )
    }
    discounts: list[Item] = []
    for inv in approved:
        offer = invoice_offer(inv, terms.get(inv.vendor_id) if inv.vendor_id else None)
        if offer is None or not offer.available(today):
            continue
        discounts.append(
            _invoice_item(
                inv,
                saving=amount_text(offer.saving, offer.currency),
                pay_by=offer.pay_by.isoformat(),
                days_left=offer.days_left(today),
            )
        )
    discounts.sort(key=lambda i: str(i.detail.get("pay_by")))
    board.action.append(_section("discounts", discounts, "/pay/payment-runs"))

    trend: list[Item] = []
    monday = today - timedelta(days=today.weekday())
    for back in range(TREND_WEEKS - 1, -1, -1):
        start = monday - timedelta(weeks=back)
        lo = datetime(start.year, start.month, start.day, tzinfo=UTC)
        hi = lo + timedelta(days=7)
        end_day = start + timedelta(days=6)
        received = int(
            await session.scalar(
                select(func.count())
                .select_from(PayInvoice)
                .where(PayInvoice.created_at >= lo, PayInvoice.created_at < hi)
            )
            or 0
        )
        flagged = int(
            await session.scalar(
                select(func.count())
                .select_from(PayInvoice)
                .where(
                    PayInvoice.created_at >= lo,
                    PayInvoice.created_at < hi,
                    func.jsonb_array_length(PayInvoice.review_reasons) > 0,
                )
            )
            or 0
        )
        trend.append(
            Item(
                kind="week",
                id=start.isoformat(),
                label=start.isoformat(),
                href=f"/pay/analytics?since={start.isoformat()}&until={end_day.isoformat()}",
                detail={"received": received, "with_exceptions": flagged},
            )
        )
    board.sections.append(Section("exceptions_trend", len(trend), trend, "/pay/analytics"))

    period = previous_period(today)
    exports = int(
        await session.scalar(
            select(func.count())
            .select_from(PayJournalExport)
            .where(PayJournalExport.period == period.label)
        )
        or 0
    )
    unmatched = int(
        await session.scalar(
            select(func.count())
            .select_from(PayBankStatementLine)
            .where(PayBankStatementLine.status == StatementLineStatus.UNMATCHED.value)
        )
        or 0
    )
    open_exceptions = len(await _invoices(session, InvoiceStatus.NEEDS_REVIEW.value))
    board.sections.append(
        _section(
            "month_end",
            [
                Item(
                    "check",
                    "journal_exported",
                    "journal_exported",
                    f"/pay/month-end?period={period.label}",
                    detail={"done": exports > 0, "count": exports, "period": period.label},
                ),
                Item(
                    "check",
                    "statements_matched",
                    "statements_matched",
                    "/pay/reconciliation",
                    detail={"done": unmatched == 0, "count": unmatched},
                ),
                Item(
                    "check",
                    "exceptions_cleared",
                    "exceptions_cleared",
                    "/pay/exceptions",
                    detail={"done": open_exceptions == 0, "count": open_exceptions},
                ),
            ],
            f"/pay/month-end?period={period.label}",
        )
    )

    since: date = today - timedelta(days=ANALYTICS_DAYS)
    facts = await load_facts(session, since=since, until=today)
    result = compute(replace(facts, payables=()), now=now)
    board.metrics = [
        result.touchless_rate,
        *[m for m in result.cycle_time if m.key.endswith("receipt_to_payment")],
        result.cost_per_invoice,
        result.days_payable_outstanding,
        result.bank_changes_blocked,
        *[m for m in result.fraud_flags if m.key == "fraud_flags"],
    ]
    return board
