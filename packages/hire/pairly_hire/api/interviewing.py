# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Interview routes (M3, brief 3.7). Signed in unless stated; every change audited.

* ``GET  /jobs/{id}/interview-plan``   the plan (saved, or the default from the locked
                                        rubric), the rubric's criteria, the job's stages
                                        and who may sit on a panel
* ``PUT  /jobs/{id}/interview-plan``   save it (``hire.interview_plan.write``), naming
                                        the version edited (``expected_version``)
* ``GET  /applications/{id}/interview-kit?stage=&locale=``  the candidate's interview kit
                                        for a stage (generated through the model router on
                                        first read, then kept); blind safe
* ``GET|PUT /interviews/{id}/scorecard``, ``POST .../submit``  your own scorecard of an
                                        interview you sit on (signed in)
* ``GET  /applications/{id}/scorecards?stage=``  everyone's submitted scorecards, never
                                        for a panelist who has not submitted their own
* ``POST /public/scorecards/open|later``  the one-click links (no sign-in, rate limited)
* ``GET|PUT /scorecard-session/me``, ``POST .../submit|later``  with a scorecard session
* ``GET  /applications/{id}/debrief?stage=``  the debrief summary (``hire.scorecard.read``)
* ``POST /applications/{id}/debrief/decision``  advance, hold or reject with a reason
                                        (``hire.candidate.decide``, second factor), recorded
                                        as a decision record
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.errors import ForbiddenError, NotFoundError, ServiceUnavailableError
from pairly_core.identity import Context, CurrentActor, OrgSession, RequestSession
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_hire.antispam import SlidingWindowLimiter, client_ip
from pairly_hire.api.common import model_router
from pairly_hire.interviewing import debrief as debriefs
from pairly_hire.interviewing import kits, links, scorecards
from pairly_hire.interviewing import plan as plans
from pairly_hire.jobs import get_job
from pairly_hire.models import HireJob
from pairly_hire.permissions import (
    CANDIDATE_DECIDE,
    CANDIDATE_READ,
    JOB_READ,
    PLAN_WRITE,
    SCORECARD_READ,
    SEES_DRAFTS,
)
from pairly_hire.pipeline.records import load_record
from pairly_hire.pipeline.stages import stages_for
from pairly_hire.scheduling import api as scheduling
from pairly_hire.scheduling.emails import candidate_view
from pairly_hire.scheduling.permissions import INTERVIEW_READ

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


# ------------------------------------------------------------------ kits

_StageKey = Annotated[str, Query(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


class HireKitItemOut(BaseModel):
    criterion_id: str
    criterion_name: str
    kind: str
    screening_result: str
    plan_question: str
    questions: list[str]
    probes: list[str]
    listen_for: str
    anchors: list[str]
    source: str
    quotes: list[str]


class HireKitOut(BaseModel):
    application_id: uuid.UUID
    job_id: uuid.UUID
    job_title: str
    candidate_label: str
    identity_hidden: bool
    stage_key: str
    stage_name: str
    locale: str
    source: str
    prompt_version: str
    from_recording: bool
    items: list[HireKitItemOut]


def kit_out(kit: kits.Kit, job_title: str, label: str) -> HireKitOut:
    return HireKitOut(
        application_id=kit.application_id,
        job_id=kit.job_id,
        job_title=job_title,
        candidate_label=label,
        identity_hidden=kit.blind,
        stage_key=kit.stage_key,
        stage_name=kit.stage_name,
        locale=kit.locale,
        source=kit.source,
        prompt_version=kit.prompt_version,
        from_recording=kit.from_recording,
        items=[
            HireKitItemOut(
                criterion_id=i.criterion_id,
                criterion_name=i.criterion_name,
                kind=i.kind,
                screening_result=i.screening_result,
                plan_question=i.plan_question,
                questions=i.questions,
                probes=i.probes,
                listen_for=i.listen_for,
                anchors=i.anchors,
                source=i.source,
                quotes=i.quotes,
            )
            for i in kit.items
        ],
    )


def optional_router(request: Request) -> Any:
    """The model router, or None (the template kit): AI helps, never blocks."""
    try:
        return model_router(request)
    except ServiceUnavailableError:
        return None


@router.get("/applications/{application_id}/interview-kit", response_model=HireKitOut)
async def get_kit(
    application_id: uuid.UUID,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
    stage: _StageKey = "interview",
    locale: Literal["en", "ar"] = "en",
) -> HireKitOut:
    authorize(actor, INTERVIEW_READ)
    authorize(actor, CANDIDATE_READ)
    record = await load_record(session, application_id)
    kit = await kits.build_kit(
        session,
        record,
        stage,
        locale,
        router=optional_router(request),
        actor_id=actor.id if actor.kind is ActorKind.USER else None,
    )
    label, _summary, _hidden = await candidate_view(session, record)
    title = record.job.title_ar if locale == "ar" and record.job.title_ar else record.job.title
    return kit_out(kit, title, label)


# ------------------------------------------------------------------ scorecards


class HireScorecardCriterionOut(BaseModel):
    id: str
    name: str
    question: str
    anchors: list[str]


class HireOtherScorecardOut(BaseModel):
    interviewer_id: uuid.UUID
    interviewer_name: str
    ratings: dict[str, int]
    notes: str
    recommendation: str
    submitted_at: datetime


class HireScorecardOut(BaseModel):
    interview_id: uuid.UUID
    application_id: uuid.UUID
    job_id: uuid.UUID
    job_title: str
    candidate_label: str
    identity_hidden: bool
    stage_key: str
    stage_name: str
    starts_at: datetime | None
    ends_at: datetime | None
    criteria: list[HireScorecardCriterionOut]
    ratings: dict[str, int]
    notes: str
    recommendation: str | None
    status: str
    submitted_at: datetime | None
    due_at: datetime | None
    sla_state: str
    snoozed_until: datetime | None
    # None until your own scorecard is submitted (hidden until you submit).
    others: list[HireOtherScorecardOut] | None
    others_submitted: int
    panel_size: int


class HireScorecardIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ratings: dict[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,47}$")], int] = (
        Field(default_factory=dict, max_length=5)
    )
    notes: Annotated[str, StringConstraints(max_length=scorecards.MAX_NOTES)] = ""
    recommendation: Literal["strong_no", "no", "yes", "strong_yes"] | None = None


class HireScorecardPageOut(BaseModel):
    scorecard: HireScorecardOut
    kit: HireKitOut | None


def scorecard_out(view: scorecards.ScorecardView) -> HireScorecardOut:
    own = view.scorecard
    title = view.record.job.title
    if view.locale == "ar" and view.record.job.title_ar:
        title = view.record.job.title_ar
    return HireScorecardOut(
        interview_id=view.interview.id,
        application_id=view.record.app.id,
        job_id=view.record.job.id,
        job_title=title,
        candidate_label=view.candidate_label,
        identity_hidden=view.identity_hidden,
        stage_key=own.stage_key if own else view.stage.key,
        stage_name=kits.stage_name(view.stage, view.locale),
        starts_at=view.interview.starts_at,
        ends_at=view.interview.ends_at,
        criteria=[
            HireScorecardCriterionOut(id=c.id, name=c.name, question=c.question, anchors=c.anchors)
            for c in view.criteria
        ],
        ratings={str(k): int(v) for k, v in (own.ratings if own else {}).items()},
        notes=own.notes if own else "",
        recommendation=own.recommendation if own else None,
        status=own.status if own else "draft",
        submitted_at=own.submitted_at if own else None,
        due_at=view.due_at,
        sla_state=view.sla_state,
        snoozed_until=own.snoozed_until if own else None,
        others=(
            [
                HireOtherScorecardOut(
                    interviewer_id=o.interviewer_id,
                    interviewer_name=o.interviewer_name,
                    ratings=o.ratings,
                    notes=o.notes,
                    recommendation=o.recommendation,
                    submitted_at=o.submitted_at,
                )
                for o in view.others
            ]
            if view.others is not None
            else None
        ),
        others_submitted=view.others_submitted,
        panel_size=view.panel_size,
    )


async def _page(
    request: Request,
    session: AsyncSession,
    interview_id: uuid.UUID,
    user_id: uuid.UUID,
    locale: str,
    *,
    with_kit: bool,
) -> HireScorecardPageOut:
    view = await scorecards.scorecard_view(session, interview_id, user_id, locale=locale)
    kit_view: HireKitOut | None = None
    if with_kit:
        try:
            kit = await kits.build_kit(
                session,
                view.record,
                view.stage.key,
                locale,
                router=optional_router(request),
                actor_id=user_id,
            )
            kit_view = kit_out(kit, scorecard_out(view).job_title, view.candidate_label)
        except NotFoundError:
            kit_view = None
    return HireScorecardPageOut(scorecard=scorecard_out(view), kit=kit_view)


def _user(actor: Actor) -> uuid.UUID:
    if actor.kind is not ActorKind.USER or actor.id is None:
        raise ForbiddenError("only a person can do this")
    return actor.id


# Signed in: your own scorecard of an interview you sit on.


@router.get("/interviews/{interview_id}/scorecard", response_model=HireScorecardPageOut)
async def my_scorecard(
    interview_id: uuid.UUID,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
    locale: Literal["en", "ar"] = "en",
) -> HireScorecardPageOut:
    authorize(actor, INTERVIEW_READ)
    return await _page(request, session, interview_id, _user(actor), locale, with_kit=True)


@router.put("/interviews/{interview_id}/scorecard", response_model=HireScorecardPageOut)
async def save_my_scorecard(
    interview_id: uuid.UUID,
    body: HireScorecardIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
) -> HireScorecardPageOut:
    authorize(actor, INTERVIEW_READ)
    user = _user(actor)
    await scorecards.save(
        session,
        interview_id,
        user,
        ratings=body.ratings,
        notes=body.notes,
        recommendation=body.recommendation,
        submit=False,
    )
    return await _page(request, session, interview_id, user, "en", with_kit=False)


@router.post("/interviews/{interview_id}/scorecard/submit", response_model=HireScorecardPageOut)
async def submit_my_scorecard(
    interview_id: uuid.UUID,
    body: HireScorecardIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
) -> HireScorecardPageOut:
    authorize(actor, INTERVIEW_READ)
    user = _user(actor)
    await scorecards.save(
        session,
        interview_id,
        user,
        ratings=body.ratings,
        notes=body.notes,
        recommendation=body.recommendation,
        submit=True,
    )
    return await _page(request, session, interview_id, user, "en", with_kit=False)


class HireScorecardListOut(BaseModel):
    application_id: uuid.UUID
    stage_key: str
    # True: you sit on this panel and have not submitted yours: nothing is shown.
    hidden: bool
    expected: int
    items: list[HireOtherScorecardOut]


@router.get("/applications/{application_id}/scorecards", response_model=HireScorecardListOut)
async def application_scorecards(
    application_id: uuid.UUID,
    actor: CurrentActor,
    session: OrgSession,
    stage: _StageKey = "interview",
) -> HireScorecardListOut:
    authorize(actor, INTERVIEW_READ)
    visible = await scorecards.visible_scorecards(
        session,
        application_id,
        stage,
        actor.id if actor.kind is ActorKind.USER else None,
        can_read_all=actor.has(SCORECARD_READ),
    )
    return HireScorecardListOut(
        application_id=application_id,
        stage_key=stage,
        hidden=visible.hidden,
        expected=visible.expected,
        items=[
            HireOtherScorecardOut(
                interviewer_id=o.interviewer_id,
                interviewer_name=o.interviewer_name,
                ratings=o.ratings,
                notes=o.notes,
                recommendation=o.recommendation,
                submitted_at=o.submitted_at,
            )
            for o in visible.items
        ],
    )


# Public: the one-click link from the briefing, the calendar invitation or a reminder.
# No staff sign-in; rate limited; the token in the body, never in a URL.

SESSION_HEADER = "X-Pairly-Scorecard-Session"
_LIMITS = {"link": (30, 3600.0), "session": (600, 3600.0)}
_NO_STORE = {"Cache-Control": "no-store"}


def _hit(request: Request, kind: str, key: str) -> None:
    store: dict[str, SlidingWindowLimiter] | None = getattr(
        request.app.state, "hire_scorecard_limiters", None
    )
    if store is None:
        store = {k: SlidingWindowLimiter(n, w) for k, (n, w) in _LIMITS.items()}
        request.app.state.hire_scorecard_limiters = store
    store[kind].hit(key)


class HireScorecardTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, StringConstraints(min_length=10, max_length=links.MAX_TOKEN)]


class HireScorecardSessionOut(BaseModel):
    session_token: str
    expires_at: datetime


class HireScorecardLaterOut(BaseModel):
    snoozed_until: datetime


async def scorecard_identity(request: Request, session: RequestSession) -> links.ScorecardIdentity:
    identity = await links.authenticate(session, request.headers.get(SESSION_HEADER))
    _hit(request, "session", str(identity.session_id))
    return identity


ScorecardSession = Annotated[links.ScorecardIdentity, Depends(scorecard_identity)]


@router.post("/public/scorecards/open", response_model=HireScorecardSessionOut)
async def open_scorecard(
    body: HireScorecardTokenIn, request: Request, session: RequestSession
) -> JSONResponse:
    _hit(request, "link", client_ip(request))
    opened = await links.open_session(session, body.token)
    out = HireScorecardSessionOut(session_token=opened.token, expires_at=opened.expires_at)
    return JSONResponse(out.model_dump(mode="json"), headers=_NO_STORE)


@router.post("/public/scorecards/later", response_model=HireScorecardLaterOut)
async def later_from_link(
    body: HireScorecardTokenIn, request: Request, session: RequestSession
) -> JSONResponse:
    """The "remind me later" link of a reminder: one click, single use."""
    _hit(request, "link", client_ip(request))
    link = await links.use_link(session, links.LATER_PREFIX, body.token)
    row = await scorecards.snooze(session, link.interview_id, link.user_id)
    assert row.snoozed_until is not None  # noqa: S101 (just set)
    out = HireScorecardLaterOut(snoozed_until=row.snoozed_until)
    return JSONResponse(out.model_dump(mode="json"), headers=_NO_STORE)


@router.get("/scorecard-session/me", response_model=HireScorecardPageOut)
async def session_scorecard(
    identity: ScorecardSession,
    request: Request,
    session: RequestSession,
    locale: Literal["en", "ar"] = "en",
) -> JSONResponse:
    page = await _page(
        request, session, identity.interview_id, identity.user_id, locale, with_kit=True
    )
    return JSONResponse(page.model_dump(mode="json"), headers=_NO_STORE)


@router.put("/scorecard-session/me", response_model=HireScorecardPageOut)
async def session_save(
    body: HireScorecardIn, identity: ScorecardSession, request: Request, session: RequestSession
) -> JSONResponse:
    await scorecards.save(
        session,
        identity.interview_id,
        identity.user_id,
        ratings=body.ratings,
        notes=body.notes,
        recommendation=body.recommendation,
        submit=False,
    )
    page = await _page(
        request, session, identity.interview_id, identity.user_id, "en", with_kit=False
    )
    return JSONResponse(page.model_dump(mode="json"), headers=_NO_STORE)


@router.post("/scorecard-session/me/submit", response_model=HireScorecardPageOut)
async def session_submit(
    body: HireScorecardIn, identity: ScorecardSession, request: Request, session: RequestSession
) -> JSONResponse:
    await scorecards.save(
        session,
        identity.interview_id,
        identity.user_id,
        ratings=body.ratings,
        notes=body.notes,
        recommendation=body.recommendation,
        submit=True,
    )
    page = await _page(
        request, session, identity.interview_id, identity.user_id, "en", with_kit=False
    )
    return JSONResponse(page.model_dump(mode="json"), headers=_NO_STORE)


@router.post("/scorecard-session/me/later", response_model=HireScorecardLaterOut)
async def session_later(identity: ScorecardSession, session: RequestSession) -> JSONResponse:
    row = await scorecards.snooze(session, identity.interview_id, identity.user_id)
    assert row.snoozed_until is not None  # noqa: S101 (just set)
    out = HireScorecardLaterOut(snoozed_until=row.snoozed_until)
    return JSONResponse(out.model_dump(mode="json"), headers=_NO_STORE)


# ------------------------------------------------------------------ debrief


class HireDebriefRatingOut(BaseModel):
    interviewer: str
    label: str
    rating: int


class HireDebriefCriterionOut(BaseModel):
    id: str
    name: str
    ratings: list[HireDebriefRatingOut]
    mean: float | None
    spread: int
    disagreement: bool
    summary: str | None


class HireDebriefVoiceOut(BaseModel):
    label: str
    interviewer: str
    recommendation: str
    notes: str


class HireDebriefDecisionOut(BaseModel):
    decision: str
    reason: str
    decided_by: uuid.UUID
    decided_at: datetime
    decision_record_id: str | None


class HireDebriefOut(BaseModel):
    application_id: uuid.UUID
    job_id: uuid.UUID
    job_title: str
    candidate_label: str
    identity_hidden: bool
    stage_key: str
    stage_name: str
    # ``waiting`` until every panelist has submitted, then ``ready``.
    status: str
    expected: int
    submitted: int
    waiting_for: list[str]
    criteria: list[HireDebriefCriterionOut]
    voices: list[HireDebriefVoiceOut]
    summary: str | None
    disagreements: list[str]
    open_questions: list[str]
    source: str
    prompt_version: str
    from_recording: bool
    decision: HireDebriefDecisionOut | None
    can_decide: bool


class HireDebriefDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")] = "interview"
    decision: Literal["advance", "hold", "reject"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=1000)]


def debrief_out(d: debriefs.Debrief, actor: Actor) -> HireDebriefOut:
    notes = {c.criterion_id: c.summary for c in (d.draft.criteria if d.draft else [])}
    row = d.row
    decided = (
        HireDebriefDecisionOut(
            decision=row.decision,
            reason=row.decision_reason or "",
            decided_by=row.decided_by,
            decided_at=row.decided_at,
            decision_record_id=row.decision_record_id,
        )
        if row is not None
        and row.decision is not None
        and row.decided_by is not None
        and row.decided_at is not None
        else None
    )
    title = d.record.job.title
    if d.locale == "ar" and d.record.job.title_ar:
        title = d.record.job.title_ar
    return HireDebriefOut(
        application_id=d.record.app.id,
        job_id=d.record.job.id,
        job_title=title,
        candidate_label=d.candidate_label,
        identity_hidden=d.identity_hidden,
        stage_key=d.stage_key,
        stage_name=d.stage_name,
        status="ready" if d.ready else "waiting",
        expected=d.expected,
        submitted=d.submitted,
        waiting_for=d.waiting_for,
        criteria=[
            HireDebriefCriterionOut(
                id=r.criterion_id,
                name=r.name,
                ratings=[
                    HireDebriefRatingOut(interviewer=name, label=label, rating=value)
                    for label, name, value in r.ratings
                ],
                mean=r.mean,
                spread=r.spread,
                disagreement=r.disagreement,
                summary=notes.get(r.criterion_id),
            )
            for r in d.rated
        ],
        voices=[
            HireDebriefVoiceOut(
                label=v.label, interviewer=v.name, recommendation=v.recommendation, notes=v.notes
            )
            for v in d.voices
        ],
        summary=d.draft.summary if d.draft else None,
        disagreements=list(d.draft.disagreements) if d.draft else [],
        open_questions=list(d.draft.open_questions) if d.draft else [],
        source=d.source,
        prompt_version=d.prompt_version,
        from_recording=d.from_recording,
        decision=decided,
        can_decide=d.ready and decided is None and actor.has(CANDIDATE_DECIDE),
    )


async def _debrief_reader(
    session: AsyncSession, actor: Actor, application_id: uuid.UUID, stage: str
) -> None:
    """The hiring team reads a debrief; a panelist only after submitting their own."""
    authorize(actor, SCORECARD_READ)
    visible = await scorecards.visible_scorecards(
        session,
        application_id,
        stage,
        actor.id if actor.kind is ActorKind.USER else None,
        can_read_all=True,
    )
    if visible.hidden:
        raise ForbiddenError("submit your own scorecard before reading the debrief")


@router.get("/applications/{application_id}/debrief", response_model=HireDebriefOut)
async def get_debrief(
    application_id: uuid.UUID,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
    stage: _StageKey = "interview",
    locale: Literal["en", "ar"] = "en",
) -> HireDebriefOut:
    await _debrief_reader(session, actor, application_id, stage)
    d = await debriefs.build_debrief(
        session,
        application_id,
        stage,
        locale,
        router=optional_router(request),
        actor_id=actor.id if actor.kind is ActorKind.USER else None,
    )
    return debrief_out(d, actor)


@router.post("/applications/{application_id}/debrief/decision", response_model=HireDebriefOut)
async def decide_debrief(
    application_id: uuid.UUID,
    body: HireDebriefDecisionIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
) -> HireDebriefOut:
    authorize(actor, CANDIDATE_DECIDE)
    user = _user(actor)
    await _debrief_reader(session, actor, application_id, body.stage)
    d = await debriefs.decide(
        session,
        application_id,
        body.stage,
        decision=body.decision,
        reason=body.reason,
        actor_id=user,
        router=optional_router(request),
    )
    return debrief_out(d, actor)
