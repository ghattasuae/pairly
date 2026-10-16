# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Reference check routes (M3, brief 3.8.2 and 5.1.6). Every change audited.

Signed in:

* ``GET  /applications/{id}/references?locale=``  referees, their answers, the summary
  and the consistency flags (``hire.reference.read``). Blind safe: while the candidate
  is blind, referees show as "Referee 1" without name or address, and free text answers
  go through the blind redactor.
* ``POST /applications/{id}/references``  ask a referee (``hire.reference.write``);
  the email with the single use link goes out at once
* ``POST /references/{id}/send``    a new link (the old one stops working)
* ``POST /references/{id}/cancel``  cancel an open request

The referee (no sign-in; rate limited; the token in the body, never in a URL):

* ``POST /public/references/open``  ``{"token"}``: use the link once, get a session
* ``GET|PUT /reference-session/me``  the form with saved answers; save progress
* ``POST /reference-session/me/submit``  submit (every required answer)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from pairly_core.errors import ForbiddenError
from pairly_core.identity import CurrentActor, OrgSession, RequestSession
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_hire.antispam import client_ip, limiters
from pairly_hire.api.interviewing import optional_router
from pairly_hire.api.pipeline import links
from pairly_hire.permissions import CANDIDATE_READ, REFERENCE_READ, REFERENCE_WRITE
from pairly_hire.pipeline.records import load_record
from pairly_hire.references import links as referee_links
from pairly_hire.references import service
from pairly_hire.references import summary as summaries

router = APIRouter(tags=["hire"])

SESSION_HEADER = "X-Pairly-Reference-Session"
_LIMITS = {"link": (30, 3600.0), "session": (300, 3600.0)}
_NO_STORE = {"Cache-Control": "no-store"}


def _user(actor: Actor) -> uuid.UUID:
    if actor.kind is not ActorKind.USER or actor.id is None:
        raise ForbiddenError("only a person can do this")
    return actor.id


async def _hit(request: Request, kind: str, key: str) -> None:
    await limiters(request, "hire_reference_limiters", _LIMITS, "hire.references")[kind].hit(key)


# ------------------------------------------------------------------ the hiring team


class HireReferenceFlagOut(BaseModel):
    kind: str
    referee: str
    detail: str


class HireReferenceOut(BaseModel):
    id: uuid.UUID
    label: str
    referee_name: str | None
    referee_email: str | None
    relationship: str
    locale: str
    status: str
    sent_at: datetime | None
    opened_at: datetime | None
    submitted_at: datetime | None
    answers: dict[str, Any] | None


class HireReferenceSummaryOut(BaseModel):
    summary: str
    strengths: list[str]
    development: list[str]
    inconsistencies: list[str]
    source: str
    prompt_version: str
    from_recording: bool


class HireReferencesOut(BaseModel):
    application_id: uuid.UUID
    candidate_label: str
    identity_hidden: bool
    items: list[HireReferenceOut]
    submitted: int
    summary: HireReferenceSummaryOut | None
    flags: list[HireReferenceFlagOut]
    can_write: bool


class HireReferenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)]
    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=254)]
    relationship: Literal["manager", "peer", "direct_report", "client", "other"]
    locale: Literal["en", "ar"] = "en"


async def _references_out(
    request: Request,
    session: OrgSession,
    actor: Actor,
    application_id: uuid.UUID,
    locale: str,
) -> HireReferencesOut:
    built = await summaries.build_summary(
        session,
        application_id,
        locale,
        router=optional_router(request),
        actor_id=actor.id if actor.kind is ActorKind.USER else None,
    )
    hidden = built.identity_hidden
    ar = built.locale == "ar"
    items = [
        HireReferenceOut(
            id=item.request.id,
            label=item.label,
            referee_name=None if hidden else service.referee_name(item.request),
            referee_email=None if hidden else service.referee_email(item.request),
            relationship=item.request.relationship,
            locale=item.request.locale,
            status=item.request.status,
            sent_at=item.request.sent_at,
            opened_at=item.request.opened_at,
            submitted_at=item.request.submitted_at,
            answers=built.shown.get(item.request.id),
        )
        for item in built.items
    ]
    draft = built.draft
    return HireReferencesOut(
        application_id=application_id,
        candidate_label=built.record.label(hidden),
        identity_hidden=hidden,
        items=items,
        submitted=built.submitted,
        summary=HireReferenceSummaryOut(
            summary=draft.summary,
            strengths=list(draft.strengths),
            development=list(draft.development),
            inconsistencies=list(draft.inconsistencies),
            source=built.source,
            prompt_version=built.prompt_version,
            from_recording=built.from_recording,
        )
        if draft is not None
        else None,
        flags=[
            HireReferenceFlagOut(
                kind=f.kind, referee=f.referee, detail=f.detail_ar if ar else f.detail
            )
            for f in built.flags
        ],
        can_write=actor.has(REFERENCE_WRITE),
    )


def _reader(actor: Actor) -> None:
    authorize(actor, CANDIDATE_READ)
    authorize(actor, REFERENCE_READ)


@router.get("/applications/{application_id}/references", response_model=HireReferencesOut)
async def list_references(
    application_id: uuid.UUID,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
    locale: Literal["en", "ar"] = "en",
) -> HireReferencesOut:
    _reader(actor)
    return await _references_out(request, session, actor, application_id, locale)


@router.post(
    "/applications/{application_id}/references",
    response_model=HireReferencesOut,
    status_code=201,
)
async def ask_referee(
    application_id: uuid.UUID,
    body: HireReferenceIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
) -> HireReferencesOut:
    _reader(actor)
    authorize(actor, REFERENCE_WRITE)
    user = _user(actor)
    await load_record(session, application_id)
    row = await service.create_request(
        session,
        user,
        application_id,
        name=body.name,
        email=body.email,
        relationship=body.relationship,
        locale=body.locale,
    )
    await service.send(session, row, actor_id=user, links=links(request))
    return await _references_out(request, session, actor, application_id, body.locale)


@router.post("/references/{reference_id}/send", response_model=HireReferencesOut)
async def resend(
    reference_id: uuid.UUID, request: Request, actor: CurrentActor, session: OrgSession
) -> HireReferencesOut:
    _reader(actor)
    authorize(actor, REFERENCE_WRITE)
    user = _user(actor)
    row = await service.get_request(session, reference_id, lock=True)
    await service.send(session, row, actor_id=user, links=links(request))
    return await _references_out(request, session, actor, row.application_id, "en")


@router.post("/references/{reference_id}/cancel", response_model=HireReferencesOut)
async def cancel(
    reference_id: uuid.UUID, request: Request, actor: CurrentActor, session: OrgSession
) -> HireReferencesOut:
    _reader(actor)
    authorize(actor, REFERENCE_WRITE)
    user = _user(actor)
    row = await service.get_request(session, reference_id, lock=True)
    await service.cancel(session, row, actor_id=user)
    return await _references_out(request, session, actor, row.application_id, "en")


# ------------------------------------------------------------------ the referee


class HireRefereeTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, StringConstraints(min_length=10, max_length=referee_links.MAX_TOKEN)]


class HireRefereeSessionOut(BaseModel):
    session_token: str
    expires_at: datetime


class HireRefereeQuestionOut(BaseModel):
    id: str
    kind: str
    required: bool
    label: str
    options: list[str]


class HireRefereePageOut(BaseModel):
    company: str
    candidate_name: str
    job_title: str
    referee_name: str
    status: str
    answers: dict[str, Any]
    questions: list[HireRefereeQuestionOut]


class HireRefereeAnswersIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: dict[str, str | int | None]


async def referee_identity(
    request: Request, session: RequestSession
) -> referee_links.RefereeIdentity:
    identity = await referee_links.authenticate(session, request.headers.get(SESSION_HEADER))
    await _hit(request, "session", str(identity.session_id))
    return identity


RefereeSession = Annotated[referee_links.RefereeIdentity, Depends(referee_identity)]


async def _page(
    session: RequestSession, identity: referee_links.RefereeIdentity, locale: str
) -> JSONResponse:
    page = await service.referee_page(session, identity, locale)
    out = HireRefereePageOut(
        company=page.company,
        candidate_name=page.candidate_name,
        job_title=page.job_title,
        referee_name=page.referee_name,
        status=page.status,
        answers=page.answers,
        questions=[HireRefereeQuestionOut(**q) for q in page.questions],
    )
    return JSONResponse(out.model_dump(mode="json"), headers=_NO_STORE)


@router.post("/public/references/open", response_model=HireRefereeSessionOut)
async def open_reference(
    body: HireRefereeTokenIn, request: Request, session: RequestSession
) -> JSONResponse:
    await _hit(request, "link", client_ip(request))
    opened = await referee_links.open_session(session, body.token)
    out = HireRefereeSessionOut(session_token=opened.token, expires_at=opened.expires_at)
    return JSONResponse(out.model_dump(mode="json"), headers=_NO_STORE)


@router.get("/reference-session/me", response_model=HireRefereePageOut)
async def referee_form(
    identity: RefereeSession, session: RequestSession, locale: Literal["en", "ar"] = "en"
) -> JSONResponse:
    return await _page(session, identity, locale)


@router.put("/reference-session/me", response_model=HireRefereePageOut)
async def referee_save(
    body: HireRefereeAnswersIn,
    identity: RefereeSession,
    session: RequestSession,
    locale: Literal["en", "ar"] = "en",
) -> JSONResponse:
    await service.save(session, identity, dict(body.answers))
    return await _page(session, identity, locale)


@router.post("/reference-session/me/submit", response_model=HireRefereePageOut)
async def referee_submit(
    body: HireRefereeAnswersIn,
    identity: RefereeSession,
    session: RequestSession,
    locale: Literal["en", "ar"] = "en",
) -> JSONResponse:
    await service.submit(session, identity, dict(body.answers))
    return await _page(session, identity, locale)
