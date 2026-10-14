# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pay analytics route (brief 4.9). Finance controllers and admins,
``pay.analytics.read``.

* ``GET /analytics?since=&until=``  every metric of ``pairly_pay.analytics`` with the
  records behind it (invoices, bank-detail changes, approvals by reference). Open
  payables are as of now, whatever the period.

Computed from the organisation's own tables at request time (Row Level Security); no
number is stored or invented, and a metric without data is ``null``, never 0.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from pairly_core.errors import BadRequestError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import authorize
from pairly_pay.analytics import Metric, compute, load_facts
from pairly_pay.payables import COMMITTED, load_payables
from pairly_pay.permissions import ANALYTICS_READ

router = APIRouter(tags=["pay"])


class PayRecordLinkOut(BaseModel):
    kind: str
    id: str
    label: str


class PayMetricOut(BaseModel):
    key: str
    value: float | None
    unit: str
    n: int
    records: list[PayRecordLinkOut]
    records_total: int
    detail: dict[str, Any]


class PayAnalyticsOut(BaseModel):
    generated_at: datetime
    since: date | None
    until: date | None
    invoices: int
    touchless_rate: PayMetricOut
    cycle_time: list[PayMetricOut]
    cost_per_invoice: PayMetricOut
    exception_rate: list[PayMetricOut]
    duplicates_prevented: list[PayMetricOut]
    bank_changes_blocked: PayMetricOut
    fraud_flags: list[PayMetricOut]
    approval_sla_breaches: PayMetricOut
    on_time_payment_rate: PayMetricOut
    days_payable_outstanding: PayMetricOut
    open_payables: list[PayMetricOut]


def metric_out(m: Metric) -> PayMetricOut:
    return PayMetricOut.model_validate(m.as_dict())


@router.get("/analytics", response_model=PayAnalyticsOut)
async def pay_analytics(
    actor: CurrentActor,
    session: OrgSession,
    since: date | None = None,
    until: date | None = None,
) -> PayAnalyticsOut:
    authorize(actor, ANALYTICS_READ)
    if since is not None and until is not None and until < since:
        raise BadRequestError("until is before since")
    facts = await load_facts(session, since=since, until=until)
    facts = replace(facts, payables=await load_payables(session, COMMITTED))
    now = datetime.now(UTC)
    result = compute(facts, now=now)
    return PayAnalyticsOut(
        generated_at=now,
        since=since,
        until=until,
        invoices=result.invoices,
        touchless_rate=metric_out(result.touchless_rate),
        cycle_time=[metric_out(m) for m in result.cycle_time],
        cost_per_invoice=metric_out(result.cost_per_invoice),
        exception_rate=[metric_out(m) for m in result.exception_rate],
        duplicates_prevented=[metric_out(m) for m in result.duplicates_prevented],
        bank_changes_blocked=metric_out(result.bank_changes_blocked),
        fraud_flags=[metric_out(m) for m in result.fraud_flags],
        approval_sla_breaches=metric_out(result.approval_sla_breaches),
        on_time_payment_rate=metric_out(result.on_time_payment_rate),
        days_payable_outstanding=metric_out(result.days_payable_outstanding),
        open_payables=[metric_out(m) for m in result.open_payables],
    )
