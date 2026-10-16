# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Role dashboards (brief 5.1 items 1 to 7): one small read endpoint per role, each an
aggregate of existing services (``pairly_hire.dashboards``). The web Home picks the
dashboards a person's roles call for; the API checks the permissions behind each.

* ``GET /dashboards/recruiter``       ``hire.candidate.read`` and ``hire.pipeline.write``
* ``GET /dashboards/hiring-manager``  ``hire.candidate.read`` and ``hire.requisition.create``
* ``GET /dashboards/interviewer``     ``hire.interview.read``, a person (their own seats)
* ``GET /dashboards/talent``          ``hire.analytics.read`` (survey totals with
                                      ``hire.survey.read``, the bias link with
                                      ``hire.bias.read``)
* ``GET /dashboards/referrer``        ``hire.referral.read``

Sections that need a further permission are left out for people without it (the
approval inbox shows only approvals the person may decide). Candidates appear by
reference or by the pipeline's blind-safe label only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from pairly_core.errors import ForbiddenError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_hire import dashboards
from pairly_hire.api.analytics import HireMetricOut, metric_out
from pairly_hire.api.survey import HireSurveyResultsOut, results_out
from pairly_hire.permissions import (
    ANALYTICS_READ,
    BIAS_READ,
    CANDIDATE_READ,
    PIPELINE_WRITE,
    REFERRAL_READ,
    REQUISITION_CREATE,
    SURVEY_READ,
)
from pairly_hire.scheduling.permissions import INTERVIEW_READ
from pairly_hire.survey import service as survey_service

router = APIRouter(tags=["hire"])


class HireDashStageOut(BaseModel):
    key: str
    label: str
    label_ar: str
    count: int


class HireDashItemOut(BaseModel):
    kind: str
    id: str
    label: str
    job_id: str | None
    application_id: str | None
    at: datetime | None
    detail: dict[str, str | int | float | bool | None]
    stages: list[HireDashStageOut]


class HireDashSectionOut(BaseModel):
    key: str
    count: int
    items: list[HireDashItemOut]


class HireDashboardOut(BaseModel):
    role: str
    generated_at: datetime
    timezone: str
    scope: str
    action: list[HireDashSectionOut]
    sections: list[HireDashSectionOut]
    metrics: list[HireMetricOut]
    links: list[str]
    survey: HireSurveyResultsOut | None = None


def _out(board: dashboards.Dashboard, **extra: Any) -> HireDashboardOut:
    return HireDashboardOut(
        role=board.role,
        generated_at=board.generated_at,
        timezone=board.timezone,
        scope=board.scope,
        action=[HireDashSectionOut.model_validate(s, from_attributes=True) for s in board.action],
        sections=[
            HireDashSectionOut.model_validate(s, from_attributes=True) for s in board.sections
        ],
        metrics=[metric_out(m) for m in board.metrics],
        links=board.links,
        **extra,
    )


def _person(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER or actor.id is None:
        raise ForbiddenError("only a person has a dashboard")


@router.get("/dashboards/recruiter", response_model=HireDashboardOut)
async def recruiter(actor: CurrentActor, session: OrgSession) -> HireDashboardOut:
    authorize(actor, CANDIDATE_READ)
    authorize(actor, PIPELINE_WRITE)
    _person(actor)
    board = await dashboards.recruiter(session, actor, datetime.now(UTC))
    if not actor.has(ANALYTICS_READ):
        board.metrics = []
    return _out(board)


@router.get("/dashboards/hiring-manager", response_model=HireDashboardOut)
async def hiring_manager(actor: CurrentActor, session: OrgSession) -> HireDashboardOut:
    authorize(actor, CANDIDATE_READ)
    authorize(actor, REQUISITION_CREATE)
    _person(actor)
    return _out(await dashboards.hiring_manager(session, actor, datetime.now(UTC)))


@router.get("/dashboards/interviewer", response_model=HireDashboardOut)
async def interviewer(actor: CurrentActor, session: OrgSession) -> HireDashboardOut:
    authorize(actor, INTERVIEW_READ)
    _person(actor)
    return _out(await dashboards.interviewer(session, actor, datetime.now(UTC)))


@router.get("/dashboards/talent", response_model=HireDashboardOut)
async def talent(actor: CurrentActor, session: OrgSession) -> HireDashboardOut:
    authorize(actor, ANALYTICS_READ)
    _person(actor)
    survey = actor.has(SURVEY_READ)
    board = await dashboards.talent(
        session, actor, datetime.now(UTC), survey=survey, bias=actor.has(BIAS_READ)
    )
    totals = results_out(await survey_service.aggregate_results(session)) if survey else None
    return _out(board, survey=totals)


@router.get("/dashboards/referrer", response_model=HireDashboardOut)
async def referrer(actor: CurrentActor, session: OrgSession) -> HireDashboardOut:
    authorize(actor, REFERRAL_READ)
    _person(actor)
    return _out(await dashboards.referrer(session, actor, datetime.now(UTC)))
