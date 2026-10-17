# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Bias reporting routes (M3, brief 3.10): the bias and adverse-impact report and its
audit pack. The one place staff see anything derived from self-identification, and only
as counts per group with small groups suppressed (``pairly_hire.analytics.bias``).

* ``GET /bias?job_id=&since=&until=&min_sample=``  the report (``hire.bias.read``), each
  view audited (``hire.bias.report_viewed``)
* ``POST /fairness/runs``  ``{"sample_size"?, "threshold"?}``: start a counterfactual
  fairness run now (``hire.fairness.run``); it runs after the response (202)
* ``GET /fairness/runs`` and ``GET /fairness/runs/{id}``  runs and their copies
  (``hire.analytics.read`` or ``hire.bias.read``): synthetic values, redaction results
  and, from live runs only, score differences; applications by reference
* ``GET /bias/export?...``  the bias audit pack, a zip of ``data.csv``, ``results.csv``,
  ``report.json`` and ``METHOD.md`` (``hire.bias.export``: its ``export`` action needs a
  two-factor sign-in). Every export is audited (``hire.bias.exported``) with the scope,
  the settings and the SHA-256 of the file.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from pairly_core.audit import audit_actor_action
from pairly_core.errors import ForbiddenError, NotFoundError, ServiceUnavailableError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_hire.analytics import bias, fairness
from pairly_hire.analytics.models import HireFairnessResult, HireFairnessRun
from pairly_hire.models import HireApplication
from pairly_hire.permissions import ANALYTICS_READ, BIAS_EXPORT, BIAS_READ, FAIRNESS_RUN

router = APIRouter(tags=["hire"])

_MinSample = Annotated[int, Query(ge=5, le=1000)]


class HireBiasGroupOut(BaseModel):
    group: str
    label_en: str
    label_ar: str
    suppressed: bool
    base: int | None
    selected: int | None
    rate: float | None
    impact_ratio: float | None
    small_sample: bool
    fisher_p: float | None
    judgement: str


class HireBiasStepOut(BaseModel):
    from_stage: str
    to_stage: str
    reference_group: str | None
    groups: list[HireBiasGroupOut]


class HireBiasCategoryOut(BaseModel):
    category: str
    label_en: str
    label_ar: str
    steps: list[HireBiasStepOut]


class HireBiasSettingsOut(BaseModel):
    min_sample: int
    min_group: int


class HireBiasReportOut(BaseModel):
    generated_at: str
    job_id: str | None
    since: str | None
    until: str | None
    applications: int
    settings: HireBiasSettingsOut
    stages: list[str]
    categories: list[HireBiasCategoryOut]
    fairness: dict[str, Any] | None
    notes: list[str]


async def _report(
    session: OrgSession,
    job_id: uuid.UUID | None,
    since: date | None,
    until: date | None,
    min_sample: int,
) -> bias.BiasReport:
    report = await bias.build_report(
        session,
        job_id=job_id,
        since=since,
        until=until,
        settings=bias.BiasSettings(min_sample=min_sample),
    )
    report.fairness = await fairness.latest_summary(session)
    return report


@router.get("/bias", response_model=HireBiasReportOut)
async def bias_report(
    actor: CurrentActor,
    session: OrgSession,
    job_id: uuid.UUID | None = None,
    since: date | None = None,
    until: date | None = None,
    min_sample: _MinSample = bias.DEFAULT_MIN_SAMPLE,
) -> HireBiasReportOut:
    authorize(actor, BIAS_READ)
    report = await _report(session, job_id, since, until, min_sample)
    # Like every read of self-identification totals, each view is audited.
    await audit_actor_action(
        session,
        actor,
        "hire.bias.report_viewed",
        entity_type="hire.job" if job_id else None,
        entity_id=job_id,
        workflow="hire",
        payload={"applications": report.applications, "min_sample": min_sample},
    )
    return HireBiasReportOut.model_validate(report.as_dict())


@router.get("/bias/export", response_class=Response)
async def bias_export(
    actor: CurrentActor,
    session: OrgSession,
    job_id: uuid.UUID | None = None,
    since: date | None = None,
    until: date | None = None,
    min_sample: _MinSample = bias.DEFAULT_MIN_SAMPLE,
) -> Response:
    authorize(actor, BIAS_READ)
    authorize(actor, BIAS_EXPORT)
    report = await _report(session, job_id, since, until, min_sample)
    pack = bias.audit_pack(report)
    digest = bias.pack_digest(pack)
    await audit_actor_action(
        session,
        actor,
        "hire.bias.exported",
        entity_type="hire.bias_pack",
        entity_id=digest[:32],
        workflow="hire",
        payload={
            "sha256": digest,
            "job_id": report.job_id,
            "since": report.since,
            "until": report.until,
            "applications": report.applications,
            "min_sample": report.settings.min_sample,
            "min_group": report.settings.min_group,
        },
    )
    return Response(
        pack,
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="pairly-bias-audit-pack.zip"',
            "X-Content-Type-Options": "nosniff",
            "X-Pairly-Sha256": digest,
        },
    )


# ------------------------------------------------------------------ fairness runs


class HireFairnessRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_size: int = Field(default=fairness.DEFAULT_SAMPLE, ge=1, le=fairness.MAX_SAMPLE)
    threshold: Decimal = Field(default=fairness.DEFAULT_THRESHOLD, ge=0, le=100)


class HireFairnessRunOut(BaseModel):
    id: uuid.UUID
    status: str
    trigger: str
    mode: str
    threshold: float
    sample_size: int
    applications: int
    copies: int
    flagged: int
    summary: dict[str, Any]
    started_at: datetime
    finished_at: datetime | None
    failure_reason: str | None


class HireFairnessResultOut(BaseModel):
    application_id: uuid.UUID
    job_id: uuid.UUID | None
    reference: str | None
    dimension: str
    inserted: list[str]
    leaked: list[str]
    redaction_complete: bool
    input_identical: bool
    blind: bool
    model_status: str
    control_score: float | None
    copy_score: float | None
    delta: float | None
    flagged: bool | None


class HireFairnessRunDetailOut(HireFairnessRunOut):
    results: list[HireFairnessResultOut]


def _fairness_reader(actor: Actor) -> None:
    if not (actor.has(ANALYTICS_READ) or actor.has(BIAS_READ)):
        authorize(actor, ANALYTICS_READ)


def _run_out(run: HireFairnessRun) -> HireFairnessRunOut:
    return HireFairnessRunOut(
        id=run.id,
        status=run.status,
        trigger=run.trigger,
        mode=run.mode,
        threshold=float(run.threshold),
        sample_size=run.sample_size,
        applications=run.applications,
        copies=run.copies,
        flagged=run.flagged,
        summary=dict(run.summary),
        started_at=run.started_at,
        finished_at=run.finished_at,
        failure_reason=run.failure_reason,
    )


def _optional(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


async def _execute(request_state: Any, org_id: uuid.UUID, run_id: uuid.UUID) -> None:
    runtime: WorkflowRuntime = request_state.workflow_runtime
    # A failure marks the run failed with the reason; nothing to raise after the response.
    with contextlib.suppress(Exception):
        await fairness.execute_run(
            request_state.pairly.sessionmaker, runtime.services, org_id, run_id
        )


@router.post("/fairness/runs", response_model=HireFairnessRunOut, status_code=202)
async def start_fairness_run(
    body: HireFairnessRunIn,
    request: Request,
    background: BackgroundTasks,
    actor: CurrentActor,
    session: OrgSession,
) -> HireFairnessRunOut:
    authorize(actor, FAIRNESS_RUN)
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can start a fairness run")
    runtime = getattr(request.app.state, "workflow_runtime", None)
    if not isinstance(runtime, WorkflowRuntime):
        raise ServiceUnavailableError("fairness runs are not available here; try again later")
    run = await fairness.start_run(
        session,
        trigger="manual",
        started_by=str(actor.id),
        mode=fairness.model_mode(runtime.services),
        sample_size=body.sample_size,
        threshold=body.threshold,
    )
    background.add_task(_execute, request.app.state, actor.org_id, run.id)
    return _run_out(run)


@router.get("/fairness/runs", response_model=list[HireFairnessRunOut])
async def fairness_runs(actor: CurrentActor, session: OrgSession) -> list[HireFairnessRunOut]:
    _fairness_reader(actor)
    rows = await session.scalars(
        select(HireFairnessRun).order_by(HireFairnessRun.started_at.desc()).limit(20)
    )
    return [_run_out(r) for r in rows]


@router.get("/fairness/runs/{run_id}", response_model=HireFairnessRunDetailOut)
async def fairness_run(
    run_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> HireFairnessRunDetailOut:
    _fairness_reader(actor)
    run = await session.scalar(select(HireFairnessRun).where(HireFairnessRun.id == run_id))
    if run is None:
        raise NotFoundError("fairness run not found")
    rows = list(
        await session.scalars(
            select(HireFairnessResult)
            .where(HireFairnessResult.run_id == run.id)
            .order_by(HireFairnessResult.created_at, HireFairnessResult.dimension)
        )
    )
    apps = (
        {
            a.id: a
            for a in await session.scalars(
                select(HireApplication).where(
                    HireApplication.id.in_(list({r.application_id for r in rows}))
                )
            )
        }
        if rows
        else {}
    )
    results = [
        HireFairnessResultOut(
            application_id=r.application_id,
            job_id=apps[r.application_id].job_id if r.application_id in apps else None,
            reference=apps[r.application_id].reference if r.application_id in apps else None,
            dimension=r.dimension,
            inserted=list(r.inserted),
            leaked=list(r.leaked),
            redaction_complete=r.redaction_complete,
            input_identical=r.input_identical,
            blind=r.blind,
            model_status=r.model_status,
            control_score=_optional(r.control_score),
            copy_score=_optional(r.copy_score),
            delta=_optional(r.delta),
            flagged=r.flagged,
        )
        for r in rows
    ]
    return HireFairnessRunDetailOut(**_run_out(run).model_dump(), results=results)
