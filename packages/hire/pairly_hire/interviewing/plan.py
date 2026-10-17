# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology

"""A job's interview plan (brief 3.1.5, 5.3): stages, interviewers per stage, and a
scorecard per stage mapped to rubric criteria.

* A stage is one of the job's pipeline stages (its ``key``), with a name, the people who
  usually interview at it (the default panel when scheduling), and its scorecard.
* A scorecard has 3 to 5 questions, each mapped to one criterion of the job's locked
  rubric (one rating per criterion, so a criterion appears once per stage). Every
  criterion is rated 1 to 4; each rating has an anchor description: the question's own
  four anchors, or ``DEFAULT_ANCHORS``.
* ``sla_hours``: scorecards are due this long after the interview ends (feedback SLA).

Until a person saves one, a job's plan is ``default_plan``: one "Interview" stage at the
``interview`` pipeline stage with the first must-have criteria (then nice-to-haves) of
the locked rubric. A job without a locked rubric has no plan: there is nothing to map.

Saving checks every criterion against the locked rubric, every stage against the job's
pipeline, and every interviewer against the people who may sit on a panel. A save names
the version it edited (``expected_version``) and raises it; a stale save is a conflict.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import audit_actor_action
from pairly_core.errors import BadRequestError, ConflictError
from pairly_core.policy_versions import PolicyError, get_policy
from pairly_core.rbac import Actor
from pairly_hire.interviewing.models import HireInterviewPlan
from pairly_hire.models import HireJob
from pairly_hire.pipeline.stages import stages_for
from pairly_hire.rubric import Criterion, Rubric, parse_rubric

WORKFLOW: Final = "hire"
ENTITY: Final = "hire.interview_plan"
MIN_QUESTIONS: Final = 3
MAX_QUESTIONS: Final = 5
MAX_STAGES: Final = 6
MAX_INTERVIEWERS: Final = 10
RATING_MIN: Final = 1
RATING_MAX: Final = 4
DEFAULT_SLA_HOURS: Final = 24
DEFAULT_STAGE: Final = "interview"
# What each rating means when a question gives no anchors of its own (index = rating - 1).
DEFAULT_ANCHORS: Final[tuple[str, ...]] = (
    "No evidence, or answers raised real concerns.",
    "Some evidence, with clear gaps for this role.",
    "Solid evidence: concrete examples that meet the bar.",
    "Strong evidence: several concrete examples above the bar.",
)
DEFAULT_ANCHORS_AR: Final[tuple[str, ...]] = (
    "لا يوجد دليل، أو أثارت الإجابات مخاوف حقيقية.",
    "بعض الأدلة، مع فجوات واضحة لهذه الوظيفة.",
    "أدلة جيدة: أمثلة ملموسة تحقق المستوى المطلوب.",
    "أدلة قوية: عدة أمثلة ملموسة تتجاوز المستوى المطلوب.",
)

_Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
_Criterion = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,47}$")]
_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
_NameAr = Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)]
_Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=300)]
_QuestionAr = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
_Anchor = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=200)]


class PlanQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: _Criterion
    question: _Question
    question_ar: _QuestionAr = ""
    # What ratings 1 to 4 mean for this criterion at this stage. Empty: the defaults.
    anchors: tuple[_Anchor, ...] = ()

    @model_validator(mode="after")
    def _four_anchors(self) -> PlanQuestion:
        if self.anchors and len(self.anchors) != RATING_MAX - RATING_MIN + 1:
            raise ValueError("anchors must describe every rating from 1 to 4, in order")
        return self

    def scale(self, locale: str = "en") -> tuple[str, ...]:
        if self.anchors:
            return self.anchors
        return DEFAULT_ANCHORS_AR if locale == "ar" else DEFAULT_ANCHORS


class PlanStage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: _Key
    name: _Name
    name_ar: _NameAr = ""
    interviewer_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_INTERVIEWERS)
    questions: list[PlanQuestion] = Field(min_length=MIN_QUESTIONS, max_length=MAX_QUESTIONS)

    @model_validator(mode="after")
    def _unique(self) -> PlanStage:
        criteria = [q.criterion_id for q in self.questions]
        if len(set(criteria)) != len(criteria):
            raise ValueError("each criterion is rated once per stage")
        if len(set(self.interviewer_ids)) != len(self.interviewer_ids):
            raise ValueError("an interviewer is listed twice")
        return self

    def criteria(self) -> list[str]:
        return [q.criterion_id for q in self.questions]

    def question_for(self, criterion_id: str) -> PlanQuestion | None:
        return next((q for q in self.questions if q.criterion_id == criterion_id), None)


class InterviewPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stages: list[PlanStage] = Field(min_length=1, max_length=MAX_STAGES)
    sla_hours: int = Field(default=DEFAULT_SLA_HOURS, ge=4, le=168)

    @model_validator(mode="after")
    def _unique_stages(self) -> InterviewPlan:
        keys = [s.key for s in self.stages]
        if len(set(keys)) != len(keys):
            raise ValueError("each pipeline stage has one interview stage at most")
        return self

    def stage(self, key: str) -> PlanStage | None:
        return next((s for s in self.stages if s.key == key), None)


def _question(criterion: Criterion) -> PlanQuestion:
    name = criterion.name.rstrip(".")
    return PlanQuestion(
        criterion_id=criterion.id,
        question=(
            f"{name}: walk me through a recent example from your own work. "
            "What did you do, and what was the result?"
        )[:300],
        question_ar=(f"{name}: حدثني عن مثال حديث من عملك. ماذا فعلت بنفسك، وما النتيجة؟")[:300],
    )


def default_plan(rubric: Rubric) -> InterviewPlan:
    """One interview stage scoring the first criteria of the rubric (3 to 5)."""
    criteria = [*rubric.must_haves, *rubric.nice_to_haves][:MAX_QUESTIONS]
    if len(criteria) < MIN_QUESTIONS:
        # A scorecard rates 3 to 5 criteria: a smaller rubric needs more criteria first.
        raise ConflictError("the rubric needs at least 3 criteria for an interview scorecard")
    return InterviewPlan(
        stages=[
            PlanStage(
                key=DEFAULT_STAGE,
                name="Interview",
                name_ar="المقابلة",
                questions=[_question(c) for c in criteria],
            )
        ]
    )


async def locked_rubric(session: AsyncSession, job: HireJob) -> Rubric | None:
    if job.rubric_policy_id is None:
        return None
    policy = await get_policy(session, job.rubric_policy_id)
    if policy is None:
        return None
    try:
        return parse_rubric(policy.content)
    except PolicyError:
        return None


def criteria_by_id(rubric: Rubric) -> dict[str, tuple[Criterion, str]]:
    found: dict[str, tuple[Criterion, str]] = {c.id: (c, "must") for c in rubric.must_haves}
    for c in rubric.nice_to_haves:
        found.setdefault(c.id, (c, "nice"))
    return found


@dataclass(frozen=True, slots=True)
class LoadedPlan:
    plan: InterviewPlan
    # None: the default plan, never saved.
    version: int | None
    rubric: Rubric


async def plan_for(session: AsyncSession, job: HireJob) -> LoadedPlan | None:
    """The job's plan (saved, or the default from its locked rubric). None without a
    locked rubric."""
    rubric = await locked_rubric(session, job)
    if rubric is None:
        return None
    row = await session.scalar(select(HireInterviewPlan).where(HireInterviewPlan.job_id == job.id))
    if row is not None:
        try:
            return LoadedPlan(InterviewPlan.model_validate(row.plan), row.version, rubric)
        except ValidationError:
            pass
    try:
        return LoadedPlan(default_plan(rubric), None, rubric)
    except ConflictError:
        return None


async def stage_plan(
    session: AsyncSession, job: HireJob, stage_key: str
) -> tuple[PlanStage, LoadedPlan] | None:
    """The plan stage for a pipeline stage, with its plan; None when the plan has none."""
    loaded = await plan_for(session, job)
    if loaded is None:
        return None
    stage = loaded.plan.stage(stage_key)
    if stage is None:
        return None
    return stage, loaded


async def interview_stage(
    session: AsyncSession, job: HireJob, stage_key: str
) -> tuple[PlanStage, LoadedPlan] | None:
    """The plan stage an interview at pipeline stage ``stage_key`` uses: that stage's,
    else the plan's first stage (an interview booked from another pipeline stage)."""
    loaded = await plan_for(session, job)
    if loaded is None:
        return None
    stage = loaded.plan.stage(stage_key) or loaded.plan.stages[0]
    return stage, loaded


def parse_plan(raw: Any) -> InterviewPlan:
    try:
        return InterviewPlan.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors()
        if not errors:
            raise BadRequestError("invalid interview plan") from None
        first = errors[0]
        where = ".".join(str(p) for p in first["loc"])
        message = str(first["msg"])
        raise BadRequestError(f"{where}: {message}" if where else message) from None


async def save_plan(
    session: AsyncSession,
    actor: Actor,
    job: HireJob,
    plan: InterviewPlan,
    *,
    expected_version: int | None,
    allowed_interviewers: set[uuid.UUID],
) -> LoadedPlan:
    rubric = await locked_rubric(session, job)
    if rubric is None:
        raise ConflictError("lock the job's rubric before planning its interviews")
    known = criteria_by_id(rubric)
    stages = await stages_for(session, job.id)
    for stage in plan.stages:
        pipeline_stage = stages.get(stage.key)
        if pipeline_stage is None or pipeline_stage.terminal:
            raise BadRequestError(f"{stage.key!r} is not an active stage of this job's pipeline")
        for question in stage.questions:
            if question.criterion_id not in known:
                raise BadRequestError(
                    f"{question.criterion_id!r} is not a criterion of the job's locked rubric"
                )
        unknown = [u for u in stage.interviewer_ids if u not in allowed_interviewers]
        if unknown:
            raise BadRequestError("an interviewer cannot sit on interview panels")
    row = await session.scalar(
        select(HireInterviewPlan).where(HireInterviewPlan.job_id == job.id).with_for_update()
    )
    current = row.version if row is not None else None
    if current != expected_version:
        raise ConflictError("someone else changed this interview plan: reload it")
    now = datetime.now(UTC)
    data = plan.model_dump(mode="json")
    if actor.id is None:
        raise BadRequestError("only a person can edit an interview plan")
    if row is None:
        row = HireInterviewPlan(
            id=uuid.uuid4(),
            org_id=actor.org_id,
            job_id=job.id,
            plan=data,
            version=1,
            updated_by=actor.id,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.plan = data
        row.version += 1
        row.updated_by = actor.id
        row.updated_at = now
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        "hire.interview_plan.saved",
        entity_type=ENTITY,
        entity_id=job.id,
        workflow=WORKFLOW,
        payload={
            "version": row.version,
            "stages": [
                {
                    "key": s.key,
                    "criteria": s.criteria(),
                    "interviewers": [str(u) for u in s.interviewer_ids],
                }
                for s in plan.stages
            ],
            "sla_hours": plan.sla_hours,
        },
    )
    return LoadedPlan(plan, row.version, rubric)


async def planned_interviewers(
    session: AsyncSession, job: HireJob, stage_key: str
) -> list[uuid.UUID]:
    """The usual interviewers of a stage (the default panel when scheduling)."""
    row = await session.scalar(select(HireInterviewPlan).where(HireInterviewPlan.job_id == job.id))
    if row is None:
        return []
    try:
        plan = InterviewPlan.model_validate(row.plan)
    except ValidationError:
        return []
    stage = plan.stage(stage_key)
    return list(stage.interviewer_ids) if stage is not None else []
