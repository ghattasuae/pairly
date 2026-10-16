# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/retention``: retention rules, settings, the deletion preview and legal holds.

Read (``core.retention.read``: admins and auditors):

* ``GET /v1/retention/rules``      the rules: what ages, after how many days, how it goes
* ``GET /v1/retention/report``     the organisation's latest run: per rule, how many rows
  or subjects are due, when they are scheduled for deletion and what the run deleted
* ``GET /v1/retention/settings``   each rule on or off with its period, and the daily cap
* ``GET /v1/retention/schedule``   the preview list: every subject scheduled for deletion
  with its date (seven days after it was listed), held, being erased
  (``?include_done=true``: and deleted)
* ``GET /v1/retention/holds``      the legal holds in force

Change (``core.retention.manage``: admins, second factor), all audited:

* ``PUT  /v1/retention/settings``  ``{"daily_cap", "rules": [{"rule", "enabled", "days",
  "period"}]}``
* ``POST /v1/retention/holds``     ``{"subject_type", "subject_id", "reason"}``: a legal
  hold on a candidate (``hire.candidate``) or a vendor (``pay.vendor``)
* ``POST /v1/retention/holds/{id}/release``
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from pairly_core.crypto.types import unseal
from pairly_core.identity import OrgSession
from pairly_core.rbac import Actor, require
from pairly_core.retention.models import PREVIEW_DAYS, RetentionHold
from pairly_core.retention.permissions import RETENTION_MANAGE, RETENTION_READ
from pairly_core.retention.rules import RetentionRegistry
from pairly_core.retention.service import (
    RuleChange,
    daily_cap,
    holds,
    latest_report,
    place_hold,
    release_hold,
    rule_states,
    schedule,
    update_settings,
)

router = APIRouter(prefix="/retention", tags=["retention"])

ReadActor = Annotated[Actor, Depends(require(RETENTION_READ))]
ManageActor = Annotated[Actor, Depends(require(RETENTION_MANAGE))]


def retention_registry(request: Request) -> RetentionRegistry:
    registry = getattr(request.app.state, "retention_registry", None)
    return registry if isinstance(registry, RetentionRegistry) else RetentionRegistry()


Registry = Annotated[RetentionRegistry, Depends(retention_registry)]


class RuleOut(BaseModel):
    key: str
    description: str
    days: int
    mode: str
    workflow: str


class ReportRowOut(BaseModel):
    rule: str
    cutoff: datetime
    would_delete: int
    scheduled_for: datetime | None
    enabled: bool
    deleted: int


class ReportOut(BaseModel):
    run_id: uuid.UUID | None
    created_at: datetime | None
    # Kept for clients of the dry-run report: a report row itself never deletes.
    dry_run: bool
    preview_days: int
    rules: list[ReportRowOut]


class RetentionPeriodOut(BaseModel):
    unit: Literal["months", "days"]
    value: int
    default: int
    minimum: int
    maximum: int


class RetentionRuleSettingOut(BaseModel):
    key: str
    description: str
    mode: str
    workflow: str
    enabled: bool
    default_enabled: bool
    days: int
    default_days: int
    configurable_days: bool
    customised: bool
    period: RetentionPeriodOut | None


class RetentionSettingsOut(BaseModel):
    daily_cap: int
    preview_days: int
    hold_subjects: list[str]
    rules: list[RetentionRuleSettingOut]


class RetentionRuleChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    enabled: bool | None = None
    days: Annotated[int, Field(ge=0, le=36500)] | None = None
    period: Annotated[int, Field(ge=0, le=36500)] | None = None


class RetentionSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_cap: Annotated[int, Field(ge=1, le=10000)] | None = None
    rules: list[RetentionRuleChangeIn] = Field(default_factory=list, max_length=100)


class RetentionScheduleItemOut(BaseModel):
    id: uuid.UUID
    rule: str
    subject_type: str
    subject_id: uuid.UUID
    label: str | None
    due_at: datetime
    listed_at: datetime
    delete_after: datetime
    status: Literal["scheduled", "held", "erasing", "deleted"]
    attempts: int
    error: str | None
    decision_id: str | None
    deleted_at: datetime | None


class RetentionHoldOut(BaseModel):
    id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    placed_by: uuid.UUID
    placed_at: datetime
    released_at: datetime | None
    # Only for those who may change holds (it may name people).
    reason: str | None


class RetentionHoldIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_type: Annotated[str, StringConstraints(min_length=3, max_length=64)]
    subject_id: uuid.UUID
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


def _hold_out(row: RetentionHold, actor: Actor) -> RetentionHoldOut:
    return RetentionHoldOut(
        id=row.id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        placed_by=row.placed_by,
        placed_at=row.placed_at,
        released_at=row.released_at,
        reason=str(unseal(row, "reason")) if actor.has(RETENTION_MANAGE) else None,
    )


async def _settings(session: OrgSession, registry: RetentionRegistry) -> RetentionSettingsOut:
    out: list[RetentionRuleSettingOut] = []
    for s in await rule_states(session, registry):
        rule = s.rule
        hook = rule.period
        out.append(
            RetentionRuleSettingOut(
                key=rule.key,
                description=rule.description,
                mode=rule.mode.value,
                workflow=rule.workflow,
                enabled=s.enabled,
                default_enabled=rule.default_enabled,
                days=s.days,
                default_days=rule.days,
                configurable_days=rule.configurable_days and hook is None,
                customised=s.customised,
                period=RetentionPeriodOut(
                    unit=hook.unit,  # type: ignore[arg-type]
                    value=s.period_value if s.period_value is not None else hook.default,
                    default=hook.default,
                    minimum=hook.minimum,
                    maximum=hook.maximum,
                )
                if hook is not None
                else None,
            )
        )
    return RetentionSettingsOut(
        daily_cap=await daily_cap(session),
        preview_days=PREVIEW_DAYS,
        hold_subjects=sorted(registry.hold_subjects),
        rules=out,
    )


@router.get("/rules", response_model=list[RuleOut])
async def rules(actor: ReadActor, registry: Registry) -> list[RuleOut]:
    return [
        RuleOut(
            key=r.key,
            description=r.description,
            days=r.days,
            mode=r.mode.value,
            workflow=r.workflow,
        )
        for r in registry.rules
    ]


@router.get("/report", response_model=ReportOut)
async def report(actor: ReadActor, session: OrgSession) -> ReportOut:
    rows = await latest_report(session)
    return ReportOut(
        run_id=rows[0].run_id if rows else None,
        created_at=min(r.created_at for r in rows) if rows else None,
        dry_run=True,
        preview_days=PREVIEW_DAYS,
        rules=[
            ReportRowOut(
                rule=r.rule,
                cutoff=r.cutoff,
                would_delete=r.would_delete,
                scheduled_for=r.scheduled_for,
                enabled=r.enabled,
                deleted=r.deleted,
            )
            for r in rows
        ],
    )


@router.get("/settings", response_model=RetentionSettingsOut)
async def get_settings(
    actor: ReadActor, session: OrgSession, registry: Registry
) -> RetentionSettingsOut:
    return await _settings(session, registry)


@router.put("/settings", response_model=RetentionSettingsOut)
async def put_settings(
    body: RetentionSettingsIn, actor: ManageActor, session: OrgSession, registry: Registry
) -> RetentionSettingsOut:
    await update_settings(
        session,
        actor,
        registry,
        changes=[
            RuleChange(rule=c.rule, enabled=c.enabled, days=c.days, period=c.period)
            for c in body.rules
        ],
        cap=body.daily_cap,
    )
    return await _settings(session, registry)


@router.get("/schedule", response_model=list[RetentionScheduleItemOut])
async def get_schedule(
    actor: ReadActor, session: OrgSession, include_done: bool = False
) -> list[RetentionScheduleItemOut]:
    return [
        RetentionScheduleItemOut(
            id=i.id,
            rule=i.rule,
            subject_type=i.subject_type,
            subject_id=i.subject_id,
            label=i.label,
            due_at=i.due_at,
            listed_at=i.listed_at,
            delete_after=i.delete_after,
            status=i.status,  # type: ignore[arg-type]
            attempts=i.attempts,
            error=i.error,
            decision_id=i.decision_id,
            deleted_at=i.deleted_at,
        )
        for i in await schedule(session, include_done=include_done)
    ]


@router.get("/holds", response_model=list[RetentionHoldOut])
async def get_holds(actor: ReadActor, session: OrgSession) -> list[RetentionHoldOut]:
    return [_hold_out(h, actor) for h in await holds(session)]


@router.post("/holds", response_model=RetentionHoldOut, status_code=201)
async def post_hold(
    body: RetentionHoldIn, actor: ManageActor, session: OrgSession, registry: Registry
) -> RetentionHoldOut:
    row = await place_hold(
        session,
        actor,
        registry,
        subject_type=body.subject_type,
        subject_id=body.subject_id,
        reason=body.reason,
    )
    return _hold_out(row, actor)


@router.post("/holds/{hold_id}/release", response_model=RetentionHoldOut)
async def post_release(
    hold_id: uuid.UUID, actor: ManageActor, session: OrgSession
) -> RetentionHoldOut:
    return _hold_out(await release_hold(session, actor, hold_id), actor)
