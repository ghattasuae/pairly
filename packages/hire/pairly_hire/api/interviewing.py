# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Interview routes (M3, brief 3.7). Signed in unless stated; every change audited.

* ``GET  /jobs/{id}/interview-plan``   the plan (saved, or the default from the locked
                                        rubric), the rubric's criteria, the job's stages
                                        and who may sit on a panel
* ``PUT  /jobs/{id}/interview-plan``   save it (``hire.interview_plan.write``), naming
                                        the version edited (``expected_version``)
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from pairly_core.errors import ForbiddenError, NotFoundError
from pairly_core.identity import Context, CurrentActor, OrgSession
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_hire.interviewing import plan as plans
from pairly_hire.jobs import get_job
from pairly_hire.models import HireJob
from pairly_hire.permissions import (
    CANDIDATE_READ,
    JOB_READ,
    PLAN_WRITE,
    SEES_DRAFTS,
)
from pairly_hire.pipeline.stages import stages_for
from pairly_hire.scheduling import api as scheduling

router = APIRouter(tags=["hire"])


def _person(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can do this")


async def _team_job(session: OrgSession, actor: Actor, job_id: uuid.UUID) -> HireJob:
    """A job of the hiring team (drafts included); others never see a plan."""
    authorize(actor, JOB_READ)
    if not (actor.has(CANDIDATE_READ) or any(actor.has(p) for p in SEES_DRAFTS)):
        raise NotFoundError("job not found")
    return await get_job(session, job_id)


# ------------------------------------------------------------------ plan


class HirePlanCriterionOut(BaseModel):
    id: str
    name: str
    kind: str
    evidence: str


class HirePlanStageOptionOut(BaseModel):
    key: str
    label: str
    label_ar: str


class HirePlanPersonOut(BaseModel):
    id: uuid.UUID
    name: str


class HireInterviewPlanOut(BaseModel):
    job_id: uuid.UUID
    rubric_locked: bool
    saved: bool
    version: int | None
    plan: plans.InterviewPlan | None
    criteria: list[HirePlanCriterionOut]
    pipeline_stages: list[HirePlanStageOptionOut]
    team: list[HirePlanPersonOut]
    default_anchors: list[str]
    default_anchors_ar: list[str]
    can_edit: bool


class HireInterviewPlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: dict[str, Any]
    expected_version: int | None = None


async def _plan_out(
    session: OrgSession, actor: Actor, ctx: Any, job: HireJob, loaded: plans.LoadedPlan | None
) -> HireInterviewPlanOut:
    stages = await stages_for(session, job.id)
    team = await scheduling.team(session, ctx.registry)
    criteria: list[HirePlanCriterionOut] = []
    if loaded is not None:
        criteria = [
            HirePlanCriterionOut(id=c.id, name=c.name, kind=kind, evidence=c.evidence)
            for c, kind in plans.criteria_by_id(loaded.rubric).values()
        ]
    return HireInterviewPlanOut(
        job_id=job.id,
        rubric_locked=job.rubric_policy_id is not None,
        saved=loaded is not None and loaded.version is not None,
        version=loaded.version if loaded else None,
        plan=loaded.plan if loaded else None,
        criteria=criteria,
        pipeline_stages=[
            HirePlanStageOptionOut(key=s.key, label=s.label, label_ar=s.label_ar)
            for s in stages.stages
            if not s.terminal
        ],
        team=[HirePlanPersonOut(id=p.id, name=p.name) for p in team],
        default_anchors=list(plans.DEFAULT_ANCHORS),
        default_anchors_ar=list(plans.DEFAULT_ANCHORS_AR),
        can_edit=actor.has(PLAN_WRITE),
    )


@router.get("/jobs/{job_id}/interview-plan", response_model=HireInterviewPlanOut)
async def get_plan(
    job_id: uuid.UUID, actor: CurrentActor, session: OrgSession, ctx: Context
) -> HireInterviewPlanOut:
    job = await _team_job(session, actor, job_id)
    return await _plan_out(session, actor, ctx, job, await plans.plan_for(session, job))


@router.put("/jobs/{job_id}/interview-plan", response_model=HireInterviewPlanOut)
async def save_plan(
    job_id: uuid.UUID,
    body: HireInterviewPlanIn,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
) -> HireInterviewPlanOut:
    authorize(actor, PLAN_WRITE)
    _person(actor)
    job = await get_job(session, job_id, lock=True)
    plan = plans.parse_plan(body.plan)
    allowed = {p.id for p in await scheduling.team(session, ctx.registry)}
    loaded = await plans.save_plan(
        session,
        actor,
        job,
        plan,
        expected_version=body.expected_version,
        allowed_interviewers=allowed,
    )
    return await _plan_out(session, actor, ctx, job, loaded)
