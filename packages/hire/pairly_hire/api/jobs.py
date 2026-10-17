# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Jobs, AI drafts, text checks, the rubric builder and application forms.

All under ``/v1/hire/jobs`` with a signed-in person or key (``CurrentActor``):

* ``GET /jobs`` and ``GET /jobs/{id}`` (``hire.job.read``).
* ``POST /jobs`` and ``PATCH /jobs/{id}`` (``hire.job.write``): title, team, location,
  work model, employment type, salary range with show or hide, English and Arabic text.
* ``POST /jobs/{id}/publish``, ``/pause``, ``/close`` (``hire.job.publish``, second
  factor). Publishing needs a locked rubric.
* ``POST /jobs/draft-description`` (``hire.job.write``): the AI drafts a job description
  from a short brief; the response carries the deterministic checks of the draft.
  Nothing is saved: the person copies what they want into the job.
* ``POST /jobs/check-text`` (``hire.job.read``) and ``GET /jobs/{id}/checks``: the
  inclusive-language and requirements checks, no model.
* ``GET /jobs/{id}/rubric`` (``hire.rubric.read``): the draft, the locked version and
  every version.
* ``POST /jobs/{id}/rubric/draft`` (``hire.rubric.write``): the AI drafts a rubric from
  the job text and stores it as the draft.
* ``PUT /jobs/{id}/rubric`` (``hire.rubric.write``): a person's edited draft.
* ``POST /jobs/{id}/rubric/lock`` (``hire.rubric.lock``, second factor).
* ``GET`` and ``PUT /jobs/{id}/form`` (read: ``hire.job.read``; save: ``hire.job.write``).
* ``PUT /jobs/{id}/recruiters`` (``hire.job.write``): the owner recruiter and up to ten
  co-recruiters, the people told about candidate replies, uploads and SLA alerts.

Writes are people only (an API key or agent may read) and audited in the request's
transaction. Model failures answer 503 and nothing changes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import select

from pairly_core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from pairly_core.identity import Context, CurrentActor, OrgSession
from pairly_core.llm.errors import ModelUnavailable
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_hire.api.common import model_router
from pairly_hire.drafting import MAX_BRIEF, check_texts, draft_job_description, draft_rubric
from pairly_hire.forms import ApplicationForm
from pairly_hire.jobs import (
    CLOSE,
    PAUSE,
    PUBLISH,
    Transition,
    change_status,
    create_job,
    current_form,
    get_job,
    save_form,
    update_job,
)
from pairly_hire.models import HireJob, JobStatus
from pairly_hire.org_time import org_today
from pairly_hire.permissions import (
    JOB_PUBLISH,
    JOB_READ,
    JOB_WRITE,
    RUBRIC_LOCK,
    RUBRIC_READ,
    RUBRIC_WRITE,
    SEES_DRAFTS,
)
from pairly_hire.recruiters import MAX_CO_RECRUITERS, assign_recruiters
from pairly_hire.rubric import Rubric, parse_rubric
from pairly_hire.rubrics import (
    get_draft,
    lock_rubric,
    locked_rubric,
    rubric_versions,
    save_draft,
)

router = APIRouter(prefix="/jobs", tags=["hire"])

_Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=160)]
_Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)]
_Text = Annotated[str, StringConstraints(max_length=20_000)]
_Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
_Country = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
# Plain literals, not the model enums: OpenAPI schema names are global, so a Hire enum
# must never take a generic name such as ``JobStatus``.
_WorkModel = Literal["onsite", "hybrid", "remote"]
_EmploymentType = Literal["full_time", "part_time", "contract", "temporary", "internship"]
_SalaryPeriod = Literal["year", "month", "hour"]
_JobStatus = Literal["draft", "open", "paused", "closed"]
_Money = Annotated[Decimal, Field(ge=0, le=Decimal("999999999999"), decimal_places=2)]


# ------------------------------------------------------------------ schemas


class HireJobPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: _Title | None = None
    title_ar: _Short | None = None
    team: _Short | None = None
    location: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=160)]
        | None
    ) = None
    country: _Country | None = None
    work_model: _WorkModel | None = None
    employment_type: _EmploymentType | None = None
    salary_min: _Money | None = None
    salary_max: _Money | None = None
    salary_currency: _Currency | None = None
    salary_period: _SalaryPeriod | None = None
    show_salary: bool | None = None
    description_en: _Text | None = None
    description_ar: _Text | None = None


class HireJobIn(HireJobPatch):
    title: _Title
    location: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=160)]
    work_model: _WorkModel
    employment_type: _EmploymentType


class HireJobOut(BaseModel):
    id: uuid.UUID
    slug: str
    status: str
    title: str
    title_ar: str
    team: str | None
    location: str
    country: str | None
    work_model: str
    employment_type: str
    salary_min: Decimal | None
    salary_max: Decimal | None
    salary_currency: str | None
    salary_period: str | None
    show_salary: bool
    description_en: str
    description_ar: str
    rubric_policy_id: uuid.UUID | None
    form_policy_id: uuid.UUID | None
    created_by: uuid.UUID | None
    owner_id: uuid.UUID | None
    co_recruiter_ids: list[uuid.UUID]
    requisition_id: uuid.UUID | None = None
    published_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, job: HireJob) -> HireJobOut:
        return cls.model_validate(job, from_attributes=True)


class HireRecruitersIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_id: uuid.UUID
    co_recruiter_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_CO_RECRUITERS)


class HireJobList(BaseModel):
    items: list[HireJobOut]


class HireFindingOut(BaseModel):
    rule: str
    category: str
    severity: str
    start: int
    end: int
    match: str
    message: str
    suggestions: list[str]


class HireTextChecks(BaseModel):
    inclusive_en: list[HireFindingOut]
    inclusive_ar: list[HireFindingOut]
    sanity_en: list[HireFindingOut]
    sanity_ar: list[HireFindingOut]


class HireCheckTextIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: _Short = ""
    title_ar: _Short = ""
    description_en: _Text = ""
    description_ar: _Text = ""


class HireBriefIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=10, max_length=MAX_BRIEF)
    ]


class HireModelCallOut(BaseModel):
    call_id: uuid.UUID
    prompt_version: str
    model: str
    from_recording: bool


class HireJobDraftOut(BaseModel):
    title: str
    title_ar: str
    summary: str
    responsibilities: list[str]
    requirements: list[str]
    nice_to_have: list[str]
    description_en: str
    description_ar: str
    checks: HireTextChecks
    model: HireModelCallOut


class HireRubricVersionOut(BaseModel):
    id: uuid.UUID
    version: int
    content_hash: str
    created_at: datetime


class HireRubricOut(BaseModel):
    job_id: uuid.UUID
    draft: dict[str, Any] | None
    draft_source: str | None
    draft_updated_at: datetime | None
    locked: dict[str, Any] | None
    locked_version: HireRubricVersionOut | None
    # True when the draft differs from the locked version (locking makes a new one).
    draft_differs: bool
    versions: list[HireRubricVersionOut]


class HireRubricLockOut(BaseModel):
    job_id: uuid.UUID
    version: HireRubricVersionOut
    new_version: bool
    applications_on_earlier_versions: int


class HireFormOut(BaseModel):
    job_id: uuid.UUID
    version: int | None
    form: dict[str, Any]
    knockout_questions: list[dict[str, Any]]


# ------------------------------------------------------------------ helpers


def _sees_drafts(actor: Actor) -> bool:
    return any(actor.has(p) for p in SEES_DRAFTS)


async def _readable_job(session: OrgSession, actor: Actor, job_id: uuid.UUID) -> HireJob:
    """A job the actor may read: any job for the hiring team, open jobs for the others."""
    authorize(actor, JOB_READ)
    job = await get_job(session, job_id)
    if job.status != JobStatus.OPEN.value and not _sees_drafts(actor):
        raise NotFoundError("job not found")
    return job


def _person(actor: Actor, what: str) -> None:
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError(f"only a person can {what}")


def _checks_out(checks: dict[str, list[dict[str, Any]]]) -> HireTextChecks:
    return HireTextChecks.model_validate(checks)


def _version(policy: Any) -> HireRubricVersionOut:
    return HireRubricVersionOut(
        id=policy.id,
        version=policy.version,
        content_hash=policy.content_hash,
        created_at=policy.created_at,
    )


# ------------------------------------------------------------------ jobs


@router.get("", response_model=HireJobList)
async def list_jobs(
    actor: CurrentActor,
    session: OrgSession,
    status: Annotated[_JobStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> HireJobList:
    authorize(actor, JOB_READ)
    query = select(HireJob).order_by(HireJob.updated_at.desc(), HireJob.id)
    if status is not None:
        query = query.where(HireJob.status == status)
    if not _sees_drafts(actor):
        query = query.where(HireJob.status == JobStatus.OPEN.value)
    rows = await session.scalars(query.limit(limit))
    return HireJobList(items=[HireJobOut.of(j) for j in rows])


@router.post("", status_code=201, response_model=HireJobOut)
async def create(body: HireJobIn, actor: CurrentActor, session: OrgSession) -> HireJobOut:
    authorize(actor, JOB_WRITE)
    _person(actor, "create a job")
    values = body.model_dump(exclude_unset=True, mode="python")
    return HireJobOut.of(await create_job(session, actor, values))


@router.post("/draft-description", response_model=HireJobDraftOut)
async def draft_description(
    body: HireBriefIn, request: Request, actor: CurrentActor, session: OrgSession
) -> HireJobDraftOut:
    authorize(actor, JOB_WRITE)
    _person(actor, "ask for a draft")
    router_ = model_router(request)
    try:
        result = await draft_job_description(
            router_, actor.org_id, brief=body.brief, actor_id=actor.id
        )
    except ModelUnavailable as exc:
        raise ModelUnavailable(
            "AI drafting is not available right now. Write the description yourself."
        ) from exc
    draft = result.value
    checks = check_texts(
        title=draft.title,
        title_ar=draft.title_ar,
        description_en=draft.description_en,
        description_ar=draft.description_ar,
        today=await org_today(session, actor.org_id),
    )
    return HireJobDraftOut(
        **draft.model_dump(),
        checks=_checks_out(checks.as_dict()),
        model=HireModelCallOut(
            call_id=result.call_id,
            prompt_version=f"{result.task}/{result.prompt_version}",
            model=result.model,
            from_recording=result.from_recording,
        ),
    )


@router.post("/check-text", response_model=HireTextChecks)
async def check_text(
    body: HireCheckTextIn, actor: CurrentActor, session: OrgSession
) -> HireTextChecks:
    authorize(actor, JOB_READ)
    checks = check_texts(
        title=body.title,
        title_ar=body.title_ar,
        description_en=body.description_en,
        description_ar=body.description_ar,
        today=await org_today(session, actor.org_id),
    )
    return _checks_out(checks.as_dict())


@router.get("/{job_id}", response_model=HireJobOut)
async def get(job_id: uuid.UUID, actor: CurrentActor, session: OrgSession) -> HireJobOut:
    return HireJobOut.of(await _readable_job(session, actor, job_id))


@router.patch("/{job_id}", response_model=HireJobOut)
async def patch(
    job_id: uuid.UUID, body: HireJobPatch, actor: CurrentActor, session: OrgSession
) -> HireJobOut:
    authorize(actor, JOB_WRITE)
    _person(actor, "edit a job")
    changes = body.model_dump(exclude_unset=True, mode="python")
    for key in ("title", "location", "work_model", "employment_type", "show_salary"):
        if key in changes and changes[key] is None:
            raise BadRequestError(f"{key.replace('_', ' ')} cannot be empty")
    job = await get_job(session, job_id, lock=True)
    return HireJobOut.of(await update_job(session, actor, job, changes))


@router.put("/{job_id}/recruiters", response_model=HireJobOut)
async def put_recruiters(
    job_id: uuid.UUID,
    body: HireRecruitersIn,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
) -> HireJobOut:
    authorize(actor, JOB_WRITE)
    _person(actor, "assign recruiters")
    job = await get_job(session, job_id, lock=True)
    job = await assign_recruiters(
        session,
        actor,
        ctx.registry,
        job,
        owner_id=body.owner_id,
        co_recruiter_ids=body.co_recruiter_ids,
    )
    return HireJobOut.of(job)


@router.get("/{job_id}/checks", response_model=HireTextChecks)
async def job_checks(job_id: uuid.UUID, actor: CurrentActor, session: OrgSession) -> HireTextChecks:
    job = await _readable_job(session, actor, job_id)
    checks = check_texts(
        title=job.title,
        title_ar=job.title_ar,
        description_en=job.description_en,
        description_ar=job.description_ar,
        today=await org_today(session, actor.org_id),
    )
    return _checks_out(checks.as_dict())


async def _transition(
    job_id: uuid.UUID, actor: Actor, session: OrgSession, transition: Transition
) -> HireJobOut:
    authorize(actor, JOB_PUBLISH)
    _person(actor, "change a job's status")
    job = await get_job(session, job_id, lock=True)
    return HireJobOut.of(await change_status(session, actor, job, transition))


@router.post("/{job_id}/publish", response_model=HireJobOut)
async def publish(job_id: uuid.UUID, actor: CurrentActor, session: OrgSession) -> HireJobOut:
    return await _transition(job_id, actor, session, PUBLISH)


@router.post("/{job_id}/pause", response_model=HireJobOut)
async def pause(job_id: uuid.UUID, actor: CurrentActor, session: OrgSession) -> HireJobOut:
    return await _transition(job_id, actor, session, PAUSE)


@router.post("/{job_id}/close", response_model=HireJobOut)
async def close(job_id: uuid.UUID, actor: CurrentActor, session: OrgSession) -> HireJobOut:
    return await _transition(job_id, actor, session, CLOSE)


# ------------------------------------------------------------------ rubric


async def _rubric_out(session: OrgSession, job: HireJob) -> HireRubricOut:
    draft = await get_draft(session, job.id)
    versions = await rubric_versions(session, job)
    locked = next((v for v in versions if v.id == job.rubric_policy_id), None)
    draft_rubric_ = parse_rubric(draft.content) if draft is not None else None
    return HireRubricOut(
        job_id=job.id,
        draft=draft_rubric_.model_dump(mode="json") if draft_rubric_ else None,
        draft_source=draft.source if draft else None,
        draft_updated_at=draft.updated_at if draft else None,
        locked=parse_rubric(locked.content).model_dump(mode="json") if locked else None,
        locked_version=_version(locked) if locked else None,
        draft_differs=draft is not None and (locked is None or draft.content != locked.content),
        versions=[_version(v) for v in versions],
    )


@router.get("/{job_id}/rubric", response_model=HireRubricOut)
async def get_rubric(job_id: uuid.UUID, actor: CurrentActor, session: OrgSession) -> HireRubricOut:
    authorize(actor, RUBRIC_READ)
    return await _rubric_out(session, await get_job(session, job_id))


@router.post("/{job_id}/rubric/draft", response_model=HireRubricOut)
async def ai_rubric_draft(
    job_id: uuid.UUID, request: Request, actor: CurrentActor, session: OrgSession
) -> HireRubricOut:
    authorize(actor, RUBRIC_WRITE)
    _person(actor, "ask for a draft")
    job = await get_job(session, job_id, lock=True)
    if not job.description_en.strip():
        raise ConflictError("write the job description first: the rubric is drafted from it")
    router_ = model_router(request)
    try:
        result = await draft_rubric(
            router_,
            actor.org_id,
            job_id=job.id,
            title=job.title,
            description=job.description_en,
            actor_id=actor.id,
        )
    except ModelUnavailable as exc:
        raise ModelUnavailable(
            "AI drafting is not available right now. Write the rubric yourself."
        ) from exc
    await save_draft(session, actor, job, result.value, source="ai", model_call_id=result.call_id)
    return await _rubric_out(session, job)


def _validated[T: BaseModel](model: type[T], data: dict[str, Any], what: str) -> T:
    """Validate a JSON body against a Hire model, with a plain message on failure.

    Bodies are typed as plain objects in OpenAPI so the shared rubric and form models
    never take global schema names (``Rubric``, ``Question``) from other workflows.
    """
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
        where = ".".join(str(p) for p in errors[0]["loc"]) if errors else ""
        message = str(errors[0]["msg"]) if errors else "invalid value"
        raise BadRequestError(f"invalid {what}: {where}: {message}" if where else message) from None


@router.put("/{job_id}/rubric", response_model=HireRubricOut)
async def put_rubric(
    job_id: uuid.UUID, body: dict[str, Any], actor: CurrentActor, session: OrgSession
) -> HireRubricOut:
    authorize(actor, RUBRIC_WRITE)
    _person(actor, "edit a rubric")
    rubric = _validated(Rubric, body, "rubric")
    job = await get_job(session, job_id, lock=True)
    await save_draft(session, actor, job, rubric, source="person")
    return await _rubric_out(session, job)


@router.post("/{job_id}/rubric/lock", response_model=HireRubricLockOut)
async def lock(job_id: uuid.UUID, actor: CurrentActor, session: OrgSession) -> HireRubricLockOut:
    authorize(actor, RUBRIC_LOCK)
    _person(actor, "lock a rubric")
    job = await get_job(session, job_id, lock=True)
    result = await lock_rubric(session, actor, job)
    return HireRubricLockOut(
        job_id=job.id,
        version=_version(result.policy),
        new_version=result.new_version,
        applications_on_earlier_versions=result.applications_on_earlier_versions,
    )


# ------------------------------------------------------------------ form


async def _form_out(session: OrgSession, job: HireJob) -> HireFormOut:
    form, version = await current_form(session, job)
    knockouts: list[dict[str, Any]] = []
    if job.rubric_policy_id is not None:
        _, rubric = await locked_rubric(session, job)
        knockouts = [k.model_dump(mode="json") for k in rubric.knockout_questions]
    return HireFormOut(
        job_id=job.id,
        version=version,
        form=form.model_dump(mode="json"),
        knockout_questions=knockouts,
    )


@router.get("/{job_id}/form", response_model=HireFormOut)
async def get_form(job_id: uuid.UUID, actor: CurrentActor, session: OrgSession) -> HireFormOut:
    return await _form_out(session, await _readable_job(session, actor, job_id))


@router.put("/{job_id}/form", response_model=HireFormOut)
async def put_form(
    job_id: uuid.UUID, body: dict[str, Any], actor: CurrentActor, session: OrgSession
) -> HireFormOut:
    authorize(actor, JOB_WRITE)
    _person(actor, "edit a form")
    form = _validated(ApplicationForm, body, "form")
    job = await get_job(session, job_id, lock=True)
    await save_form(session, actor, job, form)
    if job.status == JobStatus.CLOSED.value:  # pragma: no cover - save_form refuses
        raise NotFoundError("job not found")
    return await _form_out(session, job)
