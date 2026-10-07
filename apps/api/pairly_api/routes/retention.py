# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/retention``: retention rules and the latest dry-run report (WP1.12).

* ``GET /v1/retention/rules``    the rules: what ages, after how many days
* ``GET /v1/retention/report``   the organisation's latest dry run: per rule, how many
  rows would be deleted. Nothing is deleted (dry run only).

Both need ``core.retention.read``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from pairly_core.identity import OrgSession
from pairly_core.rbac import Actor, require
from pairly_core.retention.permissions import RETENTION_READ
from pairly_core.retention.rules import RetentionRegistry
from pairly_core.retention.service import latest_report

router = APIRouter(prefix="/retention", tags=["retention"])

ReadActor = Annotated[Actor, Depends(require(RETENTION_READ))]


def retention_registry(request: Request) -> RetentionRegistry:
    registry = getattr(request.app.state, "retention_registry", None)
    return registry if isinstance(registry, RetentionRegistry) else RetentionRegistry()


Registry = Annotated[RetentionRegistry, Depends(retention_registry)]


class RuleOut(BaseModel):
    key: str
    description: str
    days: int


class ReportRowOut(BaseModel):
    rule: str
    cutoff: datetime
    would_delete: int


class ReportOut(BaseModel):
    run_id: uuid.UUID | None
    created_at: datetime | None
    dry_run: bool
    rules: list[ReportRowOut]


@router.get("/rules", response_model=list[RuleOut])
async def rules(actor: ReadActor, registry: Registry) -> list[RuleOut]:
    return [RuleOut(key=r.key, description=r.description, days=r.days) for r in registry.rules]


@router.get("/report", response_model=ReportOut)
async def report(actor: ReadActor, session: OrgSession) -> ReportOut:
    rows = await latest_report(session)
    return ReportOut(
        run_id=rows[0].run_id if rows else None,
        created_at=min(r.created_at for r in rows) if rows else None,
        dry_run=True,
        rules=[
            ReportRowOut(rule=r.rule, cutoff=r.cutoff, would_delete=r.would_delete) for r in rows
        ],
    )
