# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Hiring analytics routes (M3, brief 3.10). HR and talent leads (recruiters) and admins,
``hire.analytics.read``.

* ``GET /analytics?job_id=&since=&until=&ghost_days=&agreement_threshold=&minutes_per_booking=``
  every metric of ``pairly_hire.analytics.metrics`` with the records behind it, plus the
  candidate experience survey totals (``survey.service.aggregate_results``, small groups
  hidden; those totals link to the survey page, never to single answers).

Computed from the organisation's own tables at request time (Row Level Security); no
number is stored or invented. The records are application references (never names),
so the page stays blind-safe.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import authorize
from pairly_hire.analytics.facts import load_facts
from pairly_hire.analytics.metrics import Metric, Settings, compute
from pairly_hire.api.survey import HireSurveyResultsOut, results_out
from pairly_hire.messaging.sla import sla_days
from pairly_hire.permissions import ANALYTICS_READ, SURVEY_READ
from pairly_hire.survey import service as survey_service

router = APIRouter(tags=["hire"])


class HireRecordLinkOut(BaseModel):
    kind: str
    id: str
    job_id: str | None
    label: str


class HireMetricOut(BaseModel):
    key: str
    value: float | None
    unit: str
    n: int
    records: list[HireRecordLinkOut]
    records_total: int
    detail: dict[str, Any]


class HireAnalyticsSettingsOut(BaseModel):
    sla_days: int
    ghost_days: int
    agreement_threshold: float
    minutes_per_booking: int


class HireAnalyticsOut(BaseModel):
    generated_at: datetime
    job_id: uuid.UUID | None
    since: date | None
    until: date | None
    applications: int
    settings: HireAnalyticsSettingsOut
    time_to_first_response: HireMetricOut
    time_in_stage: list[HireMetricOut]
    time_to_hire: HireMetricOut
    stage_conversion: list[HireMetricOut]
    sources: list[HireMetricOut]
    sla: list[HireMetricOut]
    ghosting_rate: HireMetricOut
    interviewer_load: list[HireMetricOut]
    scheduling_time_saved: HireMetricOut
    agreement: list[HireMetricOut]
    override_rate: HireMetricOut
    flags: list[HireMetricOut]
    cost_per_screened_application: HireMetricOut
    survey: HireSurveyResultsOut | None
    jobs: dict[str, str]


def metric_out(m: Metric) -> HireMetricOut:
    return HireMetricOut.model_validate(m.as_dict())


@router.get("/analytics", response_model=HireAnalyticsOut)
async def analytics(
    actor: CurrentActor,
    session: OrgSession,
    job_id: uuid.UUID | None = None,
    since: date | None = None,
    until: date | None = None,
    ghost_days: Annotated[int | None, Query(ge=1, le=365)] = None,
    agreement_threshold: Annotated[float, Query(ge=0, le=100)] = 60.0,
    minutes_per_booking: Annotated[int, Query(ge=0, le=240)] = 30,
) -> HireAnalyticsOut:
    authorize(actor, ANALYTICS_READ)
    days = await sla_days(session)
    settings = Settings(
        sla_days=days,
        ghost_days=ghost_days or days,
        agreement_threshold=agreement_threshold,
        minutes_per_booking=minutes_per_booking,
    )
    facts = await load_facts(session, job_id=job_id, since=since, until=until)
    now = datetime.now(UTC)
    result = compute(facts, now=now, settings=settings)
    survey = (
        results_out(await survey_service.aggregate_results(session, job_id=job_id, since=since))
        if actor.has(SURVEY_READ)
        else None
    )
    return HireAnalyticsOut(
        generated_at=now,
        job_id=job_id,
        since=since,
        until=until,
        applications=result.applications,
        settings=HireAnalyticsSettingsOut(
            sla_days=settings.sla_days,
            ghost_days=settings.ghost_days,
            agreement_threshold=settings.agreement_threshold,
            minutes_per_booking=settings.minutes_per_booking,
        ),
        time_to_first_response=metric_out(result.time_to_first_response),
        time_in_stage=[metric_out(m) for m in result.time_in_stage],
        time_to_hire=metric_out(result.time_to_hire),
        stage_conversion=[metric_out(m) for m in result.stage_conversion],
        sources=[metric_out(m) for m in result.sources],
        sla=[metric_out(m) for m in result.sla],
        ghosting_rate=metric_out(result.ghosting_rate),
        interviewer_load=[metric_out(m) for m in result.interviewer_load],
        scheduling_time_saved=metric_out(result.scheduling_time_saved),
        agreement=[metric_out(m) for m in result.agreement],
        override_rate=metric_out(result.override_rate),
        flags=[metric_out(m) for m in result.flags],
        cost_per_screened_application=metric_out(result.cost_per_screened_application),
        survey=survey,
        jobs=result.jobs,
    )
