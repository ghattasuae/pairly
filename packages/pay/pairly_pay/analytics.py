# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pay analytics (brief 4.9), computed at request time from the organisation's own tables.

``load_facts`` reads the invoices received in the period (``created_at``), the
bank-detail change tickets opened in it, the steps of the Pay approvals of those
invoices and the model calls made for them (Row Level Security; every query is org
scoped by the session). ``compute`` turns them into ``Metric`` values, pure.

Every metric has a value, its unit, the sample size ``n`` it was computed from, plain
details, and the records it counts (``RecordLink``: an invoice, a bank-detail change or
an approval), so every number on the page links to the rows behind it. A metric with
no data has ``value`` None, never a made-up zero. Methods, stated plainly:

* touchless rate: among approved invoices (``approved_at`` set), the share approved by
  the rule with no review reason and nobody marking it reviewed.
* cycle time, in hours: receipt to approval (``created_at`` to ``approved_at``),
  approval to payment (``approved_at`` to ``paid_at``), receipt to payment. Median and
  mean; ``value`` is the median.
* cost per invoice: the model cost of the calls made for the period's invoices (model
  calls with ``entity_type`` ``pay.invoice``) over the invoices that had calls. In
  recorded mode the calls replay recordings and cost 0; the metric says so
  (``recorded_mode``).
* exception rate: invoices with at least one review reason over all invoices, and per
  category of reason (reading, vendor, document safety, a check by its name, check
  errors, risk, approval, intake, other).
* duplicates prevented: invoices the duplicate check flagged for a person (same number,
  same file, near duplicate) that were never approved or paid; count, and value per
  currency.
* bank-change attempts blocked: bank-detail change tickets opened in the period that
  ended without the new details being used (rejected, failed or cancelled). Open
  tickets are counted apart (still holding the vendor's payments).
* fraud flags with outcomes: invoices with a high or critical finding of the bank,
  sender, document or hidden instruction checks, or a split under the approval limit,
  by what happened to them (rejected, paid, approved, still open).
* approval SLA breaches: steps of the period's Pay approvals decided after their due
  time, or still open past it now.
* on-time payment rate: paid invoices with a due date, paid on or before it.
* days payable outstanding: days from the invoice date to payment, over paid invoices
  with an invoice date. Median and mean; ``value`` is the mean.
* open payables, per currency, now (not limited to the period): what is still to pay on
  approved and scheduled invoices, the approved amount less the credit notes applied to
  each (``pairly_pay.payables.amount_due``); the gross amount and the credits applied
  are in the detail. Credit notes are never counted as payables.
"""

from __future__ import annotations

import statistics
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, Final, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.models import Approval, ApprovalStep
from pairly_core.models.llm import ModelCall
from pairly_pay.checks.settings import amount_text
from pairly_pay.common import WORKFLOW
from pairly_pay.invoices import INVOICE_ENTITY
from pairly_pay.models import PayBankChangeTicket, PayInvoice
from pairly_pay.payables import PayableFact, amount_due, gross_amount

RecordKind = Literal["invoice", "bank_change", "approval"]

MAX_RECORDS: Final = 500
MAX_INVOICES: Final = 20_000
HOUR: Final = 3600.0
DAY: Final = 86400.0

DUPLICATE_CODES: Final = frozenset(
    {"duplicate_invoice_number", "duplicate_file_hash", "near_duplicate"}
)
FRAUD_CHECKS: Final = frozenset({"bank", "impersonation", "forgery", "injection"})
FRAUD_CODES: Final = frozenset({"split_under_threshold"})
SERIOUS: Final = frozenset({"high", "critical"})
BLOCKED_TICKETS: Final = frozenset({"rejected", "failed", "cancelled"})
DONE_TICKETS: Final = BLOCKED_TICKETS | {"completed"}
APPROVED_STATUSES: Final = frozenset({"approved", "scheduled", "paid"})
CATEGORIES: Final = {
    "extraction": "reading",
    "not_read": "reading",
    "document_not_parsed": "reading",
    "run_ended": "reading",
    "corrected_after_ready": "reading",
    "guardrails_flagged": "document_safety",
    "guardrails_error": "document_safety",
    "intake_unsure": "intake",
    "check_error": "check_errors",
    "check_failed": "check_errors",
    "risk": "risk",
    "coded_after_ready": "approval",
    "changes_requested_by_approver": "approval",
}


# ------------------------------------------------------------------ facts


@dataclass(frozen=True, slots=True)
class InvoiceFact:
    id: uuid.UUID
    reference: str
    status: str
    created_at: datetime
    invoice_date: date | None = None
    due_date: date | None = None
    currency: str | None = None
    total: Decimal | None = None
    review_reasons: tuple[str, ...] = ()
    checks: tuple[dict[str, Any], ...] = ()
    reviewed: bool = False
    approved_at: datetime | None = None
    approved_by_rule: bool = False
    paid_at: datetime | None = None
    cost_micros: int = 0
    model_calls: int = 0
    recorded_calls: int = 0


@dataclass(frozen=True, slots=True)
class TicketFact:
    id: uuid.UUID
    reference: str
    status: str
    source: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StepFact:
    approval_id: uuid.UUID
    title: str
    entity_id: str | None
    status: str
    due_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class Facts:
    invoices: tuple[InvoiceFact, ...] = ()
    tickets: tuple[TicketFact, ...] = ()
    steps: tuple[StepFact, ...] = ()
    # Approved and scheduled invoices now (``pairly_pay.payables.load_payables``).
    payables: tuple[PayableFact, ...] = ()


def _bounds(since: date | None, until: date | None) -> tuple[datetime | None, datetime | None]:
    start = datetime.combine(since, time.min, tzinfo=UTC) if since else None
    # ``until`` is inclusive: everything before the next day.
    end = datetime.combine(until, time.max, tzinfo=UTC) if until else None
    return start, end


async def load_facts(
    session: AsyncSession, *, since: date | None = None, until: date | None = None
) -> Facts:
    start, end = _bounds(since, until)
    # The newest invoices when a period holds more than MAX_INVOICES.
    query = (
        select(PayInvoice)
        .order_by(PayInvoice.created_at.desc(), PayInvoice.id.desc())
        .limit(MAX_INVOICES)
    )
    tickets_q = select(PayBankChangeTicket).order_by(PayBankChangeTicket.created_at)
    if start is not None:
        query = query.where(PayInvoice.created_at >= start)
        tickets_q = tickets_q.where(PayBankChangeTicket.created_at >= start)
    if end is not None:
        query = query.where(PayInvoice.created_at <= end)
        tickets_q = tickets_q.where(PayBankChangeTicket.created_at <= end)
    rows = list(await session.scalars(query))
    ids = [str(r.id) for r in rows]
    costs: dict[str, tuple[int, int, int]] = {}
    for chunk in (ids[i : i + 1000] for i in range(0, len(ids), 1000)):
        result = await session.execute(
            select(
                ModelCall.entity_id,
                func.coalesce(func.sum(ModelCall.cost_micros), 0),
                func.count(),
                func.count().filter(
                    (ModelCall.from_recording.is_(True)) | (ModelCall.mode == "recorded")
                ),
            )
            .where(ModelCall.entity_type == INVOICE_ENTITY, ModelCall.entity_id.in_(chunk))
            .group_by(ModelCall.entity_id)
        )
        for entity_id, cost, calls, recorded in result.all():
            costs[str(entity_id)] = (int(cost), int(calls), int(recorded))
    invoices = tuple(
        InvoiceFact(
            id=r.id,
            reference=r.reference,
            status=r.status,
            created_at=r.created_at,
            invoice_date=r.invoice_date,
            due_date=r.due_date,
            currency=r.currency,
            total=r.total,
            review_reasons=tuple(str(x) for x in r.review_reasons or ()),
            checks=tuple(c for c in r.checks or () if isinstance(c, dict)),
            reviewed=r.reviewed_by is not None,
            approved_at=r.approved_at,
            approved_by_rule=bool(r.approved_by_rule),
            paid_at=r.paid_at,
            cost_micros=costs.get(str(r.id), (0, 0, 0))[0],
            model_calls=costs.get(str(r.id), (0, 0, 0))[1],
            recorded_calls=costs.get(str(r.id), (0, 0, 0))[2],
        )
        for r in rows
    )
    tickets = tuple(
        TicketFact(t.id, t.reference, t.status, t.source, t.created_at)
        for t in await session.scalars(tickets_q)
    )
    steps: list[StepFact] = []
    for chunk in (ids[i : i + 1000] for i in range(0, len(ids), 1000)):
        step_rows = await session.execute(
            select(
                Approval.id,
                Approval.title,
                Approval.entity_id,
                ApprovalStep.status,
                ApprovalStep.due_at,
                ApprovalStep.completed_at,
            )
            .join(ApprovalStep, ApprovalStep.approval_id == Approval.id)
            .where(
                Approval.workflow == WORKFLOW,
                Approval.entity_type == INVOICE_ENTITY,
                Approval.entity_id.in_(chunk),
            )
        )
        steps.extend(
            StepFact(
                approval_id=row[0],
                title=row[1],
                entity_id=row[2],
                status=row[3],
                due_at=row[4],
                completed_at=row[5],
            )
            for row in step_rows.all()
        )
    return Facts(invoices=invoices, tickets=tickets, steps=tuple(steps))


# ------------------------------------------------------------------ metrics


@dataclass(frozen=True, slots=True)
class RecordLink:
    kind: RecordKind
    id: str
    label: str


@dataclass(slots=True)
class Metric:
    key: str
    value: float | None
    unit: str
    n: int
    records: list[RecordLink] = field(default_factory=list)
    records_total: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def invoice_link(i: InvoiceFact) -> RecordLink:
    return RecordLink("invoice", str(i.id), i.reference)


def _metric(
    key: str, value: float | None, unit: str, n: int, records: Sequence[RecordLink], **detail: Any
) -> Metric:
    return Metric(
        key=key,
        value=value,
        unit=unit,
        n=n,
        records=list(records[:MAX_RECORDS]),
        records_total=len(records),
        detail=detail,
    )


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _summary(values: Sequence[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return round(statistics.median(values), 2), round(statistics.fmean(values), 2)


def _findings(invoice: InvoiceFact) -> Iterable[tuple[str, dict[str, Any]]]:
    for check in invoice.checks:
        name = str(check.get("check") or "")
        for f in check.get("findings") or ():
            if isinstance(f, dict):
                yield name, f


def touchless_rate(invoices: Sequence[InvoiceFact]) -> Metric:
    approved = [i for i in invoices if i.approved_at is not None]
    touchless = [
        i for i in approved if i.approved_by_rule and not i.review_reasons and not i.reviewed
    ]
    return _metric(
        "touchless_rate",
        _rate(len(touchless), len(approved)),
        "rate",
        len(approved),
        [invoice_link(i) for i in touchless],
        touchless=len(touchless),
    )


def cycle_times(invoices: Sequence[InvoiceFact]) -> list[Metric]:
    spans: tuple[tuple[str, str, str], ...] = (
        ("cycle.receipt_to_approval", "created_at", "approved_at"),
        ("cycle.approval_to_payment", "approved_at", "paid_at"),
        ("cycle.receipt_to_payment", "created_at", "paid_at"),
    )
    out: list[Metric] = []
    for key, first, last in spans:
        hours: list[float] = []
        records: list[RecordLink] = []
        for i in invoices:
            start: datetime | None = getattr(i, first)
            end: datetime | None = getattr(i, last)
            if start is None or end is None or end < start:
                continue
            hours.append((end - start).total_seconds() / HOUR)
            records.append(invoice_link(i))
        median, mean = _summary(hours)
        out.append(_metric(key, median, "hours", len(hours), records, median=median, mean=mean))
    return out


def cost_per_invoice(invoices: Sequence[InvoiceFact]) -> Metric:
    called = [i for i in invoices if i.model_calls]
    total = sum(i.cost_micros for i in called)
    calls = sum(i.model_calls for i in called)
    recorded = sum(i.recorded_calls for i in called)
    value = round(total / len(called) / 1_000_000, 6) if called else None
    return _metric(
        "cost_per_invoice",
        value,
        "usd",
        len(called),
        [invoice_link(i) for i in called],
        total_usd=round(total / 1_000_000, 6),
        model_calls=calls,
        recorded_calls=recorded,
        recorded_mode=bool(calls) and recorded == calls,
    )


def reason_category(reason: str, invoice: InvoiceFact) -> str:
    kind, _, rest = reason.partition(":")
    if kind == "check":
        code = rest.split(":", 1)[0]
        for name, f in _findings(invoice):
            if f.get("code") == code and name:
                return f"check.{name}"
        return "check.other"
    if kind.startswith("vendor_"):
        return "vendor"
    if kind.startswith("approval_"):
        return "approval"
    return CATEGORIES.get(kind, "other")


def exception_rates(invoices: Sequence[InvoiceFact]) -> list[Metric]:
    flagged = [i for i in invoices if i.review_reasons]
    out = [
        _metric(
            "exceptions.all",
            _rate(len(flagged), len(invoices)),
            "rate",
            len(invoices),
            [invoice_link(i) for i in flagged],
            exceptions=len(flagged),
        )
    ]
    by_category: dict[str, list[InvoiceFact]] = defaultdict(list)
    for i in flagged:
        for category in dict.fromkeys(reason_category(r, i) for r in i.review_reasons):
            by_category[category].append(i)
    for category in sorted(by_category):
        rows = by_category[category]
        out.append(
            _metric(
                f"exceptions.{category}",
                _rate(len(rows), len(invoices)),
                "rate",
                len(invoices),
                [invoice_link(i) for i in rows],
                category=category,
                exceptions=len(rows),
            )
        )
    return out


def _amount(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):f}"


def duplicates_prevented(invoices: Sequence[InvoiceFact]) -> list[Metric]:
    flagged = [
        i
        for i in invoices
        if any(
            f.get("code") in DUPLICATE_CODES and f.get("needs_review") is True
            for _, f in _findings(i)
        )
    ]
    prevented = [i for i in flagged if i.approved_at is None and i.status not in APPROVED_STATUSES]
    out = [
        _metric(
            "duplicates_prevented",
            float(len(prevented)) if invoices else None,
            "count",
            len(invoices),
            [invoice_link(i) for i in prevented],
            flagged=len(flagged),
            rejected=sum(1 for i in prevented if i.status == "rejected"),
            still_held=sum(1 for i in prevented if i.status != "rejected"),
        )
    ]
    by_currency: dict[str, list[InvoiceFact]] = defaultdict(list)
    for i in prevented:
        if i.total is not None and i.currency:
            by_currency[i.currency].append(i)
    for currency in sorted(by_currency):
        rows = by_currency[currency]
        total = sum((i.total for i in rows if i.total is not None), Decimal(0))
        out.append(
            _metric(
                f"duplicates_prevented_value.{currency}",
                float(total),
                "amount",
                len(rows),
                [invoice_link(i) for i in rows],
                currency=currency,
                amount=_amount(total),
            )
        )
    return out


def bank_changes_blocked(tickets: Sequence[TicketFact]) -> Metric:
    blocked = [t for t in tickets if t.status in BLOCKED_TICKETS]
    open_ = [t for t in tickets if t.status not in DONE_TICKETS]
    by_source: dict[str, int] = defaultdict(int)
    for t in blocked:
        by_source[t.source] += 1
    return _metric(
        "bank_changes_blocked",
        float(len(blocked)) if tickets else None,
        "count",
        len(tickets),
        [RecordLink("bank_change", str(t.id), t.reference) for t in blocked],
        open=len(open_),
        completed=sum(1 for t in tickets if t.status == "completed"),
        by_source=dict(sorted(by_source.items())),
    )


def outcome_of(invoice: InvoiceFact) -> str:
    if invoice.status == "rejected":
        return "rejected"
    if invoice.status == "paid" or invoice.paid_at is not None:
        return "paid"
    if invoice.status in APPROVED_STATUSES or invoice.approved_at is not None:
        return "approved"
    return "open"


def fraud_flags(invoices: Sequence[InvoiceFact]) -> list[Metric]:
    flagged = [
        i
        for i in invoices
        if any(
            (name in FRAUD_CHECKS or f.get("code") in FRAUD_CODES) and f.get("severity") in SERIOUS
            for name, f in _findings(i)
        )
    ]
    out = [
        _metric(
            "fraud_flags",
            float(len(flagged)) if invoices else None,
            "count",
            len(invoices),
            [invoice_link(i) for i in flagged],
        )
    ]
    for outcome in ("rejected", "open", "approved", "paid"):
        rows = [i for i in flagged if outcome_of(i) == outcome]
        out.append(
            _metric(
                f"fraud_flags.{outcome}",
                float(len(rows)) if flagged else None,
                "count",
                len(flagged),
                [invoice_link(i) for i in rows],
                outcome=outcome,
            )
        )
    return out


def step_breached(step: StepFact, now: datetime) -> bool:
    if step.due_at is None:
        return False
    if step.completed_at is not None:
        return step.completed_at > step.due_at
    return step.status == "active" and now > step.due_at


def sla_breaches(steps: Sequence[StepFact], now: datetime) -> Metric:
    timed = [s for s in steps if s.due_at is not None]
    late = [s for s in timed if step_breached(s, now)]
    approvals = {s.approval_id: s.title for s in late}
    return _metric(
        "approval_sla_breaches",
        float(len(late)) if timed else None,
        "count",
        len(timed),
        [
            RecordLink("approval", str(a), t)
            for a, t in sorted(approvals.items(), key=lambda x: str(x[0]))
        ],
        approvals=len(approvals),
    )


def on_time_rate(invoices: Sequence[InvoiceFact]) -> Metric:
    paid = [i for i in invoices if i.paid_at is not None and i.due_date is not None]
    on_time = [
        i
        for i in paid
        if i.paid_at is not None and i.due_date is not None and i.paid_at.date() <= i.due_date
    ]
    return _metric(
        "on_time_payment_rate",
        _rate(len(on_time), len(paid)),
        "rate",
        len(paid),
        [invoice_link(i) for i in on_time],
        late=len(paid) - len(on_time),
    )


def days_payable_outstanding(invoices: Sequence[InvoiceFact]) -> Metric:
    days: list[float] = []
    records: list[RecordLink] = []
    for i in invoices:
        if i.paid_at is None or i.invoice_date is None:
            continue
        span = (i.paid_at.date() - i.invoice_date).days
        if span < 0:
            continue
        days.append(float(span))
        records.append(invoice_link(i))
    median, mean = _summary(days)
    return _metric(
        "days_payable_outstanding", mean, "days", len(days), records, median=median, mean=mean
    )


def open_payables(payables: Sequence[PayableFact]) -> list[Metric]:
    """Per currency: what is still to pay on approved and scheduled invoices, after the
    credit notes applied to them."""
    by_currency: dict[str, list[PayableFact]] = defaultdict(list)
    for p in payables:
        if p.committed and p.currency and amount_due(p) is not None:
            by_currency[p.currency].append(p)
    out: list[Metric] = []
    for currency in sorted(by_currency):
        rows = by_currency[currency]
        net = sum((amount_due(p) or Decimal(0) for p in rows), Decimal(0))
        gross = sum((gross_amount(p) or Decimal(0) for p in rows), Decimal(0))
        credits = sum(
            (min(p.credit_total, gross_amount(p) or Decimal(0)) for p in rows), Decimal(0)
        )
        out.append(
            _metric(
                f"open_payables.{currency}",
                float(net),
                "amount",
                len(rows),
                [RecordLink("invoice", str(p.id), p.reference) for p in rows],
                currency=currency,
                amount=amount_text(net, currency),
                gross=amount_text(gross, currency),
                credits_applied=amount_text(credits, currency),
                scheduled=sum(1 for p in rows if p.status == "scheduled"),
                approved=sum(1 for p in rows if p.status == "approved"),
            )
        )
    return out


@dataclass(slots=True)
class PayAnalytics:
    invoices: int
    touchless_rate: Metric
    cycle_time: list[Metric]
    cost_per_invoice: Metric
    exception_rate: list[Metric]
    duplicates_prevented: list[Metric]
    bank_changes_blocked: Metric
    fraud_flags: list[Metric]
    approval_sla_breaches: Metric
    on_time_payment_rate: Metric
    days_payable_outstanding: Metric
    open_payables: list[Metric] = field(default_factory=list)


def compute(facts: Facts, *, now: datetime) -> PayAnalytics:
    invoices = list(facts.invoices)
    return PayAnalytics(
        invoices=len(invoices),
        touchless_rate=touchless_rate(invoices),
        cycle_time=cycle_times(invoices),
        cost_per_invoice=cost_per_invoice(invoices),
        exception_rate=exception_rates(invoices),
        duplicates_prevented=duplicates_prevented(invoices),
        bank_changes_blocked=bank_changes_blocked(facts.tickets),
        fraud_flags=fraud_flags(invoices),
        approval_sla_breaches=sla_breaches(facts.steps, now),
        on_time_payment_rate=on_time_rate(invoices),
        days_payable_outstanding=days_payable_outstanding(invoices),
        open_payables=open_payables(facts.payables),
    )
