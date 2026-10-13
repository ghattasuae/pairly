# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Candidate experience survey routes (M3, brief 3.8.5).

The candidate (no sign-in; rate limited; the token in the body, never in a URL):

* ``POST /public/surveys/view``    ``{"token", "locale"}``: the questions (the link stays
                                   usable)
* ``POST /public/surveys/answer``  ``{"token", "answers", "attach_name"}``: once;
                                   anonymous unless ``attach_name``

HR and admins (``hire.survey.read``):

* ``GET /surveys/results?job_id=&since=``  totals and averages, small groups hidden
* ``GET /surveys/named?job_id=``           only answers whose authors attached their name
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from pairly_core.identity import CurrentActor, OrgSession, RequestSession
from pairly_core.rbac import authorize
from pairly_hire.antispam import SlidingWindowLimiter, client_ip
from pairly_hire.permissions import CANDIDATE_READ, SURVEY_READ
from pairly_hire.survey import service

router = APIRouter(tags=["hire"])

_LIMITS = {"view": (60, 3600.0), "answer": (20, 3600.0)}
_NO_STORE = {"Cache-Control": "no-store"}


def _hit(request: Request, kind: str) -> None:
    store: dict[str, SlidingWindowLimiter] | None = getattr(
        request.app.state, "hire_survey_limiters", None
    )
    if store is None:
        store = {k: SlidingWindowLimiter(n, w) for k, (n, w) in _LIMITS.items()}
        request.app.state.hire_survey_limiters = store
    store[kind].hit(client_ip(request))


_Token = Annotated[str, StringConstraints(min_length=10, max_length=service.MAX_TOKEN)]


class HireSurveyViewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: _Token
    locale: Literal["en", "ar"] = "en"


class HireSurveyQuestionOut(BaseModel):
    id: str
    kind: str
    required: bool
    label: str
    options: list[str]


class HireSurveyFormOut(BaseModel):
    company: str
    job_title: str
    questions: list[HireSurveyQuestionOut]


class HireSurveyAnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: _Token
    answers: dict[str, str | int | None]
    attach_name: bool = False


class HireSurveyAnsweredOut(BaseModel):
    ok: bool
    named: bool


class HireSurveyGroupOut(BaseModel):
    responses: int
    suppressed: bool
    averages: dict[str, float | None]
    recommend: dict[str, int]


class HireSurveyResultsOut(BaseModel):
    invited: int
    responses: int
    response_rate: float | None
    overall: HireSurveyGroupOut
    by_outcome: dict[str, HireSurveyGroupOut]
    min_group: int


class HireSurveyNamedOut(BaseModel):
    response_id: uuid.UUID
    application_id: uuid.UUID
    outcome: str
    answers: dict[str, str | int]
    comment: str | None
    answered_on: date


@router.post("/public/surveys/view", response_model=HireSurveyFormOut)
async def view_survey(
    body: HireSurveyViewIn, request: Request, session: RequestSession
) -> JSONResponse:
    _hit(request, "view")
    found = await service.open_form(session, body.token, body.locale)
    out = HireSurveyFormOut(
        company=found.company,
        job_title=found.job_title,
        questions=[HireSurveyQuestionOut(**q) for q in found.questions],
    )
    return JSONResponse(out.model_dump(mode="json"), headers=_NO_STORE)


@router.post("/public/surveys/answer", response_model=HireSurveyAnsweredOut)
async def answer_survey(
    body: HireSurveyAnswerIn, request: Request, session: RequestSession
) -> JSONResponse:
    _hit(request, "answer")
    await service.answer(session, body.token, dict(body.answers), attach_name=body.attach_name)
    out = HireSurveyAnsweredOut(ok=True, named=body.attach_name)
    return JSONResponse(out.model_dump(mode="json"), headers=_NO_STORE)


def _group(g: service.GroupResult) -> HireSurveyGroupOut:
    return HireSurveyGroupOut(
        responses=g.responses,
        suppressed=g.suppressed,
        averages=dict(g.averages),
        recommend=dict(g.recommend),
    )


@router.get("/surveys/results", response_model=HireSurveyResultsOut)
async def survey_results(
    actor: CurrentActor,
    session: OrgSession,
    job_id: uuid.UUID | None = None,
    since: date | None = None,
) -> HireSurveyResultsOut:
    authorize(actor, SURVEY_READ)
    r = await service.aggregate_results(session, job_id=job_id, since=since)
    return HireSurveyResultsOut(
        invited=r.invited,
        responses=r.responses,
        response_rate=r.response_rate,
        overall=_group(r.overall),
        by_outcome={k: _group(v) for k, v in r.by_outcome.items()},
        min_group=r.min_group,
    )


@router.get("/surveys/named", response_model=list[HireSurveyNamedOut])
async def named(
    actor: CurrentActor, session: OrgSession, job_id: uuid.UUID | None = None
) -> list[HireSurveyNamedOut]:
    authorize(actor, SURVEY_READ)
    authorize(actor, CANDIDATE_READ)
    return [
        HireSurveyNamedOut(
            response_id=n.response_id,
            application_id=n.application_id,
            outcome=n.outcome,
            answers=n.answers,
            comment=n.comment,
            answered_on=n.answered_on,
        )
        for n in await service.named_responses(session, job_id=job_id)
    ]
