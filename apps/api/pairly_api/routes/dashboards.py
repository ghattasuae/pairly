# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Shared and system dashboards on Home (brief 5.1 items 12 to 14), read only.

* ``GET /v1/dashboards/auditor``        ``core.audit.read``
* ``GET /v1/dashboards/admin``          ``core.admin_dashboard.read``
* ``GET /v1/dashboards/ai-governance``  ``core.ai_governance.read`` (admin, auditor)

Each is built in the viewer's tenant-bound session (Row Level Security), so it only ever
holds the viewer's organisation; within it, each section needs the permission of the
page it summarises. Workflow plugins add their sections (``dashboard_sections``). The
platform operations dashboard (item 15) is ``pairly_api.routes.ops``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from pairly_api.decision_services import DecisionServices, get_decision_services
from pairly_core.dashboards.admin import AdminInputs, admin_dashboard
from pairly_core.dashboards.auditor import auditor_dashboard
from pairly_core.dashboards.evals import default_evals_dir
from pairly_core.dashboards.governance import GovernanceInputs, governance_dashboard
from pairly_core.dashboards.model import Dashboard, SectionRegistry
from pairly_core.dashboards.permissions import (
    ADMIN_DASHBOARD_READ,
    AI_GOVERNANCE_READ,
    AUDITOR_DASHBOARD_READ,
)
from pairly_core.identity import OrgSession
from pairly_core.rbac import Actor, require
from pairly_core.retention.rules import RetentionRegistry

router = APIRouter(prefix="/dashboards", tags=["dashboards"])

Scalar = str | int | float | bool | None


class SysDashItemOut(BaseModel):
    kind: str
    id: str
    label: str
    href: str | None
    at: datetime | None
    status: str | None
    detail: dict[str, Scalar]


class SysDashStatOut(BaseModel):
    key: str
    # None: no data (never shown as 0).
    value: float | None
    unit: str
    href: str | None
    text: str | None
    n: int | None


class SysDashSectionOut(BaseModel):
    key: str
    # None: no data.
    count: int | None
    href: str | None
    items: list[SysDashItemOut]
    stats: list[SysDashStatOut]
    source: str | None
    note: str | None
    workflow: str | None


class SysDashLinkOut(BaseModel):
    key: str
    href: str


class SysDashboardOut(BaseModel):
    board: str
    generated_at: datetime
    action: list[SysDashSectionOut]
    sections: list[SysDashSectionOut]
    links: list[SysDashLinkOut]


def dashboard_out(board: Dashboard) -> SysDashboardOut:
    return SysDashboardOut.model_validate(board, from_attributes=True)


def section_registry(request: Request) -> SectionRegistry:
    registry = getattr(request.app.state, "dashboard_sections", None)
    return registry if isinstance(registry, SectionRegistry) else SectionRegistry()


def admin_inputs(request: Request) -> AdminInputs:
    state = request.app.state
    notify = getattr(state, "notifications", None)
    retention = getattr(state, "retention_registry", None)
    # The settings were validated at startup; what is left is whether email really goes
    # out (a development transport, or SMTP without a host in development, does not).
    transport = str(getattr(notify, "email_transport", "unknown"))
    problems: list[str] = []
    if transport == "smtp" and not getattr(notify, "smtp_host", None):
        problems.append("smtp_host_missing")
    elif transport in {"fake", "outbox", "unknown"}:
        problems.append("development_transport")
    return AdminInputs(
        email_transport=transport,
        email_problems=problems,
        default_budget_micros=getattr(state, "default_budget_micros", None),
        retention=retention if isinstance(retention, RetentionRegistry) else RetentionRegistry(),
    )


Sections = Annotated[SectionRegistry, Depends(section_registry)]
Inputs = Annotated[AdminInputs, Depends(admin_inputs)]
Services = Annotated[DecisionServices, Depends(get_decision_services)]


@router.get("/auditor", response_model=SysDashboardOut)
async def auditor(
    actor: Annotated[Actor, Depends(require(AUDITOR_DASHBOARD_READ))],
    session: OrgSession,
    sections: Sections,
    services: Services,
) -> SysDashboardOut:
    board = await auditor_dashboard(
        session,
        actor,
        now=datetime.now(UTC),
        sections=sections,
        anchor_store=services.anchor_store,
        anchor_public_key=services.anchor_public_key,
        large_chain_events=services.large_chain_events,
    )
    return dashboard_out(board)


@router.get("/admin", response_model=SysDashboardOut)
async def admin(
    actor: Annotated[Actor, Depends(require(ADMIN_DASHBOARD_READ))],
    session: OrgSession,
    sections: Sections,
    inputs: Inputs,
) -> SysDashboardOut:
    board = await admin_dashboard(
        session, actor, now=datetime.now(UTC), sections=sections, inputs=inputs
    )
    return dashboard_out(board)


@router.get("/ai-governance", response_model=SysDashboardOut)
async def ai_governance(
    actor: Annotated[Actor, Depends(require(AI_GOVERNANCE_READ))],
    session: OrgSession,
    sections: Sections,
    inputs: Inputs,
    request: Request,
) -> SysDashboardOut:
    evals_dir = getattr(request.app.state, "evals_dir", None)
    board = await governance_dashboard(
        session,
        actor,
        now=datetime.now(UTC),
        sections=sections,
        inputs=GovernanceInputs(
            admin=inputs, evals_dir=evals_dir if evals_dir is not None else default_evals_dir()
        ),
    )
    return dashboard_out(board)
