# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pipeline routes (WP2.5): board, stages, moves, undo, bulk, profile, notes, tags,
waiting list, talent rediscovery and referrals.

Reads need ``hire.candidate.read``; moves ``hire.candidate.decide`` (a person signed in
with a second factor); notes and tags ``hire.note.write``; stage lists
``hire.pipeline.write``; referrals ``hire.referral.create`` and ``hire.referral.read``.
Every write is a person's and is audited in the request's transaction.

* ``GET  /jobs/{job_id}/pipeline``            the board: stages, cards, blind mode, SLA days
* ``GET|PUT /jobs/{job_id}/stages``           the job's stages (customise)
* ``POST /pipeline/applications/{id}/move``   ``{"to_stage", "reason"?}``
* ``POST /pipeline/applications/{id}/advance`` the next stage (one click or one key)
* ``POST /pipeline/moves/undo``               ``{"move_ids"}``: the last moves, within a minute
* ``POST /jobs/{job_id}/pipeline/bulk/preview`` what a bulk move would do (nothing changes)
* ``POST /jobs/{job_id}/pipeline/bulk/move``  the bulk move (one batch, one undo)
* ``GET  /pipeline/applications/{id}``        the profile with its timeline
* ``POST /pipeline/applications/{id}/notes``  ``{"body", "mentions"}``
* ``PUT  /pipeline/applications/{id}/tags``   ``{"tags"}``
* ``GET  /pipeline/team``                     who a note may mention
* ``GET  /pipeline/waiting``                  candidates waiting longest for an update
* ``POST /rediscovery/search``                past applicants who agreed, by skill
* ``POST /referrals/links``                   the caller's referral link for an open job
* ``GET  /referrals/mine``                    the caller's referrals, stage only
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select

from pairly_api.idempotency import idempotent
from pairly_core.errors import ForbiddenError, ServiceUnavailableError
from pairly_core.identity import Context, CurrentActor, OrgSession
from pairly_core.models import Organisation
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_core.retrieval.embeddings import Embedder, EmbeddingUnavailable
from pairly_core.search.service import search
from pairly_core.search.sources import SearchRegistry
from pairly_hire.api.common import web_url
from pairly_hire.jobs import get_job
from pairly_hire.links import LinkConfig, links_from_settings
from pairly_hire.messaging.sla import sla_days, waiting
from pairly_hire.models import HireApplication
from pairly_hire.permissions import (
    CANDIDATE_READ,
    PIPELINE_WRITE,
)
from pairly_hire.pipeline import moves, notes, referrals, views
from pairly_hire.pipeline.hooks import blind_applications, blind_mode
from pairly_hire.pipeline.records import AppRecord
from pairly_hire.pipeline.rediscovery import KIND as CANDIDATE_KIND
from pairly_hire.pipeline.rediscovery import candidate_applications
from pairly_hire.pipeline.stages import Stage, StageSet, save_stages, stages_for

router = APIRouter(tags=["hire"])

MAX_IDS: Final = moves.MAX_BULK
_StageKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


def links(request: Request) -> LinkConfig:
    config = links_from_settings(getattr(request.app.state, "notifications", None))
    if config.web_url is None:
        return LinkConfig(config.secret, web_url(request))
    return config


def _person(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can do this")


# ------------------------------------------------------------------ schemas


class HireStageOut(BaseModel):
    key: str
    label: str
    label_ar: str
    kind: str
    reveal_identity: bool


class HireStagesOut(BaseModel):
    job_id: uuid.UUID
    stages: list[HireStageOut]


class HireCardOut(BaseModel):
    application_id: uuid.UUID
    reference: str
    label: str
    identity_hidden: bool
    stage_key: str
    stage_entered_at: datetime
    submitted_at: datetime | None
    waiting_days: int
    sla_overdue: bool
    tags: list[str]
    needs_human_review: bool
    review_reasons: list[str]
    duplicate_hints: int
    referred: bool
    open_message: str | None
    open_message_id: uuid.UUID | None


class HireBoardOut(BaseModel):
    job_id: uuid.UUID
    job_title: str
    job_status: str
    blind_mode: bool
    sla_days: int
    stages: list[HireStageOut]
    cards: list[HireCardOut]


class HireMoveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_stage: _StageKey
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class HireMoveOut(BaseModel):
    move_id: uuid.UUID
    application_id: uuid.UUID
    from_stage: str
    to_stage: str
    draft_id: uuid.UUID | None
    undo_seconds: int


class HireUndoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    move_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_IDS)


class HireUndoOut(BaseModel):
    undone: list[HireMoveOut]
    cancelled_drafts: list[uuid.UUID]


class HireBulkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_IDS)
    to_stage: _StageKey


class HireBulkSummaryOut(BaseModel):
    to_stage: str
    to_label: str
    total: int
    will_move: int
    already_there: list[uuid.UUID]
    not_found: list[uuid.UUID]
    from_stages: dict[str, int]
    drafts_to_create: int
    messages_sent_now: int
    needs_approval: bool


class HireBulkOut(BaseModel):
    batch_id: uuid.UUID
    moves: list[HireMoveOut]
    undo_seconds: int


class HireEventOut(BaseModel):
    at: datetime
    kind: str
    title: str
    detail: dict[str, Any]


class HireMessageSummaryOut(BaseModel):
    id: uuid.UUID
    template_key: str | None
    label: str
    status: str
    sensitive: bool
    purpose: str
    subject: str | None
    text: str | None
    approval_id: uuid.UUID | None
    ai_fields: list[str]
    reason_source: str | None
    delivery: str | None
    created_at: datetime
    sent_at: datetime | None


class HireCriterionOut(BaseModel):
    criterion: str
    result: str
    quotes: list[str]


class HireScreeningOut(BaseModel):
    status: str
    overall: str | None
    criteria: list[HireCriterionOut]


class HireProfileOut(BaseModel):
    application_id: uuid.UUID
    job_id: uuid.UUID
    job_title: str
    reference: str
    status: str
    stage_key: str
    stage_label: str
    next_stage: HireStageOut | None
    submitted_at: datetime | None
    blind_mode: bool
    identity_hidden: bool
    label: str
    contact: dict[str, Any] | None
    files: list[dict[str, str]]
    tags: list[str]
    needs_human_review: bool
    review_reasons: list[str]
    updates_email: bool
    talent_pool: bool
    referred_by: str | None
    screening: HireScreeningOut | None
    messages: list[HireMessageSummaryOut]
    timeline: list[HireEventOut]


class HireNoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    mentions: list[uuid.UUID] = Field(default_factory=list, max_length=10)


class HireNoteOut(BaseModel):
    note_id: uuid.UUID
    mentioned: list[uuid.UUID]
    created_at: datetime


class HireTagsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tags: list[Annotated[str, StringConstraints(max_length=32)]] = Field(max_length=20)


class HireTagsOut(BaseModel):
    tags: list[str]


class HireTeamMemberOut(BaseModel):
    id: uuid.UUID
    name: str


class HireWaitingOut(BaseModel):
    application_id: uuid.UUID
    job_id: uuid.UUID
    job_title: str
    label: str
    reference: str
    stage_key: str
    stage_label: str
    waiting_days: int
    since: datetime
    sla_overdue: bool
    has_open_draft: bool


class HireWaitingList(BaseModel):
    sla_days: int
    items: list[HireWaitingOut]


class HireRediscoveryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    limit: Annotated[int, Field(ge=1, le=25)] = 10


class HireRediscoveryHit(BaseModel):
    candidate_id: uuid.UUID
    label: str
    identity_hidden: bool
    applications: list[dict[str, Any]]
    snippet: str
    score: float


class HireReferralLinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    locale: Annotated[str, StringConstraints(pattern=r"^(en|ar)$")] = "en"


class HireReferralLinkOut(BaseModel):
    link_id: uuid.UUID
    job_id: uuid.UUID
    url: str


class HireReferralOut(BaseModel):
    referral_id: uuid.UUID
    job_title: str
    candidate: str
    stage: str
    stage_label: str
    referred_at: datetime


# ------------------------------------------------------------------ helpers


def _stage_out(stage: Stage) -> HireStageOut:
    return HireStageOut(**stage.model_dump())


def _stages_out(stages: StageSet) -> list[HireStageOut]:
    return [_stage_out(s) for s in stages.stages]


def _move_out(result: moves.MoveResult) -> HireMoveOut:
    return HireMoveOut(
        move_id=result.move_id,
        application_id=result.application_id,
        from_stage=result.from_stage,
        to_stage=result.to_stage,
        draft_id=result.draft_id,
        undo_seconds=moves.UNDO_SECONDS,
    )


# ------------------------------------------------------------------ board and stages


@router.get("/jobs/{job_id}/pipeline", response_model=HireBoardOut)
async def get_board(job_id: uuid.UUID, actor: CurrentActor, session: OrgSession) -> HireBoardOut:
    authorize(actor, CANDIDATE_READ)
    job = await get_job(session, job_id)
    result = await views.board(
        session, job, blind=await blind_mode(session), sla_days=await sla_days(session)
    )
    return HireBoardOut(
        job_id=job.id,
        job_title=job.title,
        job_status=job.status,
        blind_mode=result.blind_mode,
        sla_days=result.sla_days,
        stages=_stages_out(result.stages),
        cards=[HireCardOut.model_validate(c, from_attributes=True) for c in result.cards],
    )


@router.get("/jobs/{job_id}/stages", response_model=HireStagesOut)
async def get_stages(job_id: uuid.UUID, actor: CurrentActor, session: OrgSession) -> HireStagesOut:
    authorize(actor, CANDIDATE_READ)
    job = await get_job(session, job_id)
    return HireStagesOut(job_id=job.id, stages=_stages_out(await stages_for(session, job.id)))


@router.put("/jobs/{job_id}/stages", response_model=HireStagesOut)
async def put_stages(
    job_id: uuid.UUID, body: list[dict[str, Any]], actor: CurrentActor, session: OrgSession
) -> HireStagesOut:
    authorize(actor, PIPELINE_WRITE)
    _person(actor)
    job = await get_job(session, job_id, lock=True)
    saved = await save_stages(session, actor, job, body)
    return HireStagesOut(job_id=job.id, stages=_stages_out(saved.stages))


# ------------------------------------------------------------------ moves


@router.post(
    "/pipeline/applications/{application_id}/move",
    response_model=HireMoveOut,
    dependencies=[Depends(idempotent, scope="function")],
)
async def move_application(
    application_id: uuid.UUID,
    body: HireMoveIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
) -> HireMoveOut:
    result = await moves.move(
        session, actor, application_id, body.to_stage, links=links(request), reason=body.reason
    )
    return _move_out(result)


@router.post(
    "/pipeline/applications/{application_id}/advance",
    response_model=HireMoveOut,
    dependencies=[Depends(idempotent, scope="function")],
)
async def advance_application(
    application_id: uuid.UUID, request: Request, actor: CurrentActor, session: OrgSession
) -> HireMoveOut:
    return _move_out(await moves.move_next(session, actor, application_id, links=links(request)))


@router.post("/pipeline/moves/undo", response_model=HireUndoOut)
async def undo_moves(body: HireUndoIn, actor: CurrentActor, session: OrgSession) -> HireUndoOut:
    result = await moves.undo(session, actor, body.move_ids)
    return HireUndoOut(
        undone=[_move_out(r) for r in result.undone], cancelled_drafts=result.cancelled_drafts
    )


@router.post("/jobs/{job_id}/pipeline/bulk/preview", response_model=HireBulkSummaryOut)
async def bulk_preview(
    job_id: uuid.UUID, body: HireBulkIn, actor: CurrentActor, session: OrgSession
) -> HireBulkSummaryOut:
    await get_job(session, job_id)
    summary = await moves.bulk_summary(session, actor, job_id, body.application_ids, body.to_stage)
    return HireBulkSummaryOut.model_validate(summary, from_attributes=True)


@router.post(
    "/jobs/{job_id}/pipeline/bulk/move",
    response_model=HireBulkOut,
    dependencies=[Depends(idempotent, scope="function")],
)
async def bulk_move(
    job_id: uuid.UUID, body: HireBulkIn, request: Request, actor: CurrentActor, session: OrgSession
) -> HireBulkOut:
    await get_job(session, job_id)
    batch, results = await moves.bulk_move(
        session, actor, job_id, body.application_ids, body.to_stage, links=links(request)
    )
    return HireBulkOut(
        batch_id=batch, moves=[_move_out(r) for r in results], undo_seconds=moves.UNDO_SECONDS
    )


# ------------------------------------------------------------------ profile, notes, tags


@router.get("/pipeline/applications/{application_id}", response_model=HireProfileOut)
async def get_profile(
    application_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> HireProfileOut:
    authorize(actor, CANDIDATE_READ)
    result = await views.profile(session, application_id, blind=await blind_mode(session))
    record: AppRecord = result.record
    following = record.stages.next_after(record.stage_key)
    screening = None
    if result.screening is not None:
        screening = HireScreeningOut(
            status=result.screening.status,
            overall=result.screening.overall,
            criteria=[
                HireCriterionOut(criterion=c.criterion, result=c.result, quotes=list(c.quotes))
                for c in result.screening.criteria
            ],
        )
    messages = [
        HireMessageSummaryOut.model_validate(m, from_attributes=True) for m in result.messages
    ]
    return HireProfileOut(
        application_id=record.app.id,
        job_id=record.job.id,
        job_title=record.job.title,
        reference=record.app.reference,
        status=record.app.status,
        stage_key=record.stage_key,
        stage_label=record.stage.label,
        next_stage=_stage_out(following) if following is not None else None,
        submitted_at=record.app.submitted_at,
        blind_mode=result.blind_mode,
        identity_hidden=result.identity_hidden,
        label=result.label,
        contact=result.contact,
        files=result.files,
        tags=result.tags,
        needs_human_review=record.app.needs_human_review,
        review_reasons=list(record.app.review_reasons or []),
        updates_email=result.updates_email,
        talent_pool=result.talent_pool,
        referred_by=result.referred_by,
        screening=screening,
        messages=messages,
        timeline=[
            HireEventOut(at=e.at, kind=e.kind, title=e.title, detail=e.detail)
            for e in result.timeline
        ],
    )


@router.post(
    "/pipeline/applications/{application_id}/notes", status_code=201, response_model=HireNoteOut
)
async def add_note(
    application_id: uuid.UUID,
    body: HireNoteIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
) -> HireNoteOut:
    app = await session.scalar(
        select(HireApplication.job_id).where(HireApplication.id == application_id)
    )
    link = (
        f"{links(request).web_url}/en/hire/jobs/{app}/pipeline?candidate={application_id}"
        if app is not None and links(request).web_url
        else None
    )
    added = await notes.add_note(
        session,
        actor,
        ctx.registry,
        application_id,
        body=body.body,
        mentions=body.mentions,
        link=link,
    )
    return HireNoteOut(
        note_id=added.note.id, mentioned=added.mentioned, created_at=added.note.created_at
    )


@router.put("/pipeline/applications/{application_id}/tags", response_model=HireTagsOut)
async def put_tags(
    application_id: uuid.UUID, body: HireTagsIn, actor: CurrentActor, session: OrgSession
) -> HireTagsOut:
    return HireTagsOut(tags=await notes.set_tags(session, actor, application_id, body.tags))


@router.get("/pipeline/team", response_model=list[HireTeamMemberOut])
async def team(actor: CurrentActor, session: OrgSession, ctx: Context) -> list[HireTeamMemberOut]:
    authorize(actor, CANDIDATE_READ)
    members = await notes.hiring_team(session, ctx.registry)
    return [HireTeamMemberOut(id=m.id, name=m.name) for m in members]


# ------------------------------------------------------------------ waiting longest


@router.get("/pipeline/waiting", response_model=HireWaitingList)
async def waiting_longest(
    actor: CurrentActor,
    session: OrgSession,
    job_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> HireWaitingList:
    authorize(actor, CANDIDATE_READ)
    days = await sla_days(session)
    items = (await waiting(session, job_id=job_id))[:limit]
    blind = await blind_applications(session, [w.record.app.id for w in items])
    return HireWaitingList(
        sla_days=days,
        items=[
            HireWaitingOut(
                application_id=w.record.app.id,
                job_id=w.record.job.id,
                job_title=w.record.job.title,
                label=w.record.label(w.record.app.id in blind),
                reference=w.record.app.reference,
                stage_key=w.record.stage_key,
                stage_label=w.record.stage.label,
                waiting_days=w.waiting_days,
                since=w.since,
                sla_overdue=w.waiting_days >= days,
                has_open_draft=w.has_open_draft,
            )
            for w in items
        ],
    )


# ------------------------------------------------------------------ rediscovery


def _search_registry(request: Request) -> SearchRegistry:
    registry = getattr(request.app.state, "search_registry", None)
    if not isinstance(registry, SearchRegistry):
        raise ServiceUnavailableError("search is not available here")
    return registry


def _embedder(request: Request) -> Embedder:
    from pairly_api.routes.policies import get_embedder

    return get_embedder(request)


@router.post("/rediscovery/search", response_model=list[HireRediscoveryHit])
async def rediscover(
    body: HireRediscoveryIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
) -> list[HireRediscoveryHit]:
    authorize(actor, CANDIDATE_READ)
    try:
        hits = await search(
            session,
            actor,
            roles=ctx.registry,
            registry=_search_registry(request),
            embedder=_embedder(request),
            query=body.query,
            kinds=[CANDIDATE_KIND.key],
            limit=body.limit * 3,
        )
    except EmbeddingUnavailable as exc:
        raise ServiceUnavailableError("search is not available right now") from exc
    best: dict[uuid.UUID, Any] = {}
    for hit in hits:
        if hit.source_id not in best:
            best[hit.source_id] = hit
    out: list[HireRediscoveryHit] = []
    for candidate_id, hit in list(best.items())[: body.limit]:
        apps = await candidate_applications(session, candidate_id)
        if not apps:
            continue
        # Shown by name only when one of the candidate's applications shows it.
        blind = await blind_applications(session, [uuid.UUID(a["application_id"]) for a in apps])
        hidden = not any(a["revealed"] or uuid.UUID(a["application_id"]) not in blind for a in apps)
        out.append(
            HireRediscoveryHit(
                candidate_id=candidate_id,
                label=hit.title if hidden else apps[0]["name"],
                identity_hidden=hidden,
                applications=[
                    {k: v for k, v in a.items() if k not in {"name", "revealed"}} for a in apps
                ],
                snippet=hit.text[:300],
                score=hit.score,
            )
        )
    return out


# ------------------------------------------------------------------ referrals


@router.post("/referrals/links", response_model=HireReferralLinkOut)
async def referral_link(
    body: HireReferralLinkIn, request: Request, actor: CurrentActor, session: OrgSession
) -> HireReferralLinkOut:
    slug = await session.scalar(select(Organisation.slug).where(Organisation.id == actor.org_id))
    link = await referrals.referral_link(
        session,
        actor,
        body.job_id,
        config=links(request),
        org_slug=str(slug or ""),
        locale=body.locale,
    )
    return HireReferralLinkOut(link_id=link.id, job_id=link.job_id, url=link.url)


@router.get("/referrals/mine", response_model=list[HireReferralOut])
async def my_referrals(actor: CurrentActor, session: OrgSession) -> list[HireReferralOut]:
    rows = await referrals.my_referrals(session, actor)
    return [HireReferralOut.model_validate(r, from_attributes=True) for r in rows]
