# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pay role dashboards for Home (``pairly_pay.dashboards``), one small route each.

* ``GET /dashboards/ap-clerk``            ``pay.invoice.process``
* ``GET /dashboards/approver``            ``pay.invoice.approve``
* ``GET /dashboards/finance-controller``  ``pay.analytics.read``

Org scoped (Row Level Security); the approver's lists are the person's own. Amounts are
strings at the currency's minor unit; a metric without data is ``null``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel

from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import authorize
from pairly_pay.api.analytics import PayMetricOut, metric_out
from pairly_pay.dashboards import Board, ap_clerk, approver, finance_controller
from pairly_pay.permissions import ANALYTICS_READ, INVOICE_APPROVE, INVOICE_PROCESS

router = APIRouter(tags=["pay"])


class PayDashItemOut(BaseModel):
    kind: str
    id: str
    label: str
    href: str | None
    at: datetime | None
    detail: dict[str, str | int | float | bool | None]


class PayDashSectionOut(BaseModel):
    key: str
    count: int
    href: str | None
    items: list[PayDashItemOut]


class PayDashAmountOut(BaseModel):
    key: str
    currency: str
    amount: str
    count: int
    href: str


class PayDashboardOut(BaseModel):
    role: str
    generated_at: datetime
    action: list[PayDashSectionOut]
    sections: list[PayDashSectionOut]
    amounts: list[PayDashAmountOut]
    metrics: list[PayMetricOut]
    links: list[str]


def _out(board: Board) -> PayDashboardOut:
    return PayDashboardOut(
        role=board.role,
        generated_at=board.generated_at,
        action=[PayDashSectionOut.model_validate(s, from_attributes=True) for s in board.action],
        sections=[
            PayDashSectionOut.model_validate(s, from_attributes=True) for s in board.sections
        ],
        amounts=[PayDashAmountOut.model_validate(a, from_attributes=True) for a in board.amounts],
        metrics=[metric_out(m) for m in board.metrics],
        links=board.links,
    )


@router.get("/dashboards/ap-clerk", response_model=PayDashboardOut)
async def ap_clerk_dashboard(actor: CurrentActor, session: OrgSession) -> PayDashboardOut:
    authorize(actor, INVOICE_PROCESS)
    return _out(await ap_clerk(session, actor, datetime.now(UTC)))


@router.get("/dashboards/approver", response_model=PayDashboardOut)
async def approver_dashboard(actor: CurrentActor, session: OrgSession) -> PayDashboardOut:
    authorize(actor, INVOICE_APPROVE)
    return _out(await approver(session, actor, datetime.now(UTC)))


@router.get("/dashboards/finance-controller", response_model=PayDashboardOut)
async def finance_controller_dashboard(actor: CurrentActor, session: OrgSession) -> PayDashboardOut:
    authorize(actor, ANALYTICS_READ)
    return _out(await finance_controller(session, actor, datetime.now(UTC)))
