# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Calibration mode routes (M3, brief 3.3.11).

People who label (``hire.calibration.write``):

* ``GET /calibration/sample?job_id=&size=``     scored applications of the job to label,
                                               the ones you labelled last; the job's
                                               criteria with their anchors
* ``GET /calibration/items/{application_id}``   the application's CV text as reviewers
                                               see it (blind while blind) and your
                                               labels; never the AI's scores
                                               (also ``hire.screening.read``)
* ``PUT /calibration/items/{application_id}``   ``{"scores": {criterion: 0..4}}``,
                                               audited

HR, talent leads and admins (``hire.analytics.read``):

* ``GET /calibration/report?job_id=``          agreement per criterion and the
                                               deterministic suggestions
* ``POST /calibration/suggest``                 ``{"job_id", "criterion_id"}``: a model
                                               suggestion for a low-agreement criterion
                                               (recorded answers for the demo jobs);
                                               fails closed with a plain reason
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from pairly_core.errors import ConflictError, ForbiddenError, NotFoundError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.llm.errors import ModelUnavailable
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_hire.analytics import calibration
from pairly_hire.api.screening import screening_view
from pairly_hire.models import HireApplication
from pairly_hire.permissions import ANALYTICS_READ, CALIBRATION_WRITE
from pairly_hire.screening.permissions import SCREENING_READ
from pairly_hire.screening.service import rubric_ref

router = APIRouter(tags=["hire"])


class HireCalibrationCriterionOut(BaseModel):
    id: str
    name: str
    description: str
    evidence: str
    anchors: list[str]


class HireCalibrationItemOut(BaseModel):
    application_id: uuid.UUID
    reference: str
    labelled: bool


class HireCalibrationSampleOut(BaseModel):
    job_id: uuid.UUID
    rubric_version: int | None
    criteria: list[HireCalibrationCriterionOut]
    items: list[HireCalibrationItemOut]


class HireCalibrationApplicationOut(BaseModel):
    application_id: uuid.UUID
    reference: str
    blind: bool
    text: str | None
    criteria: list[HireCalibrationCriterionOut]
    my_scores: dict[str, int]


class HireCalibrationLabelsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scores: dict[str, Annotated[int, Field(ge=0, le=4)]]


class HireCalibrationAgreementOut(BaseModel):
    criterion_id: str
    name: str
    n: int
    exact: float | None
    within_one: float | None
    kappa: float | None
    kappa_note: str | None
    mean_difference: float | None
    sources: dict[str, int]
    top_disagreement: dict[str, int] | None
    low_agreement: bool
    direction: str | None
    suggestion: str | None


class HireCalibrationReportOut(BaseModel):
    job_id: str
    rubric_version: int | None
    labels: int
    overrides: int
    criteria: list[HireCalibrationAgreementOut]


class HireCalibrationSuggestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    criterion_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")]


class HireCalibrationSuggestionOut(BaseModel):
    criterion_id: str
    suggestion: str
    from_recording: bool


def _person(actor: Actor) -> uuid.UUID:
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can do this")
    return actor.id


def _criteria(rubric: Any) -> list[HireCalibrationCriterionOut]:
    if rubric is None:
        return []
    return [
        HireCalibrationCriterionOut(
            id=c.id,
            name=c.name,
            description=c.description,
            evidence=c.evidence,
            anchors=list(c.scale_anchors),
        )
        for c in rubric.criteria
    ]


@router.get("/calibration/sample", response_model=HireCalibrationSampleOut)
async def calibration_sample(
    actor: CurrentActor,
    session: OrgSession,
    job_id: uuid.UUID,
    size: Annotated[int, Query(ge=1, le=calibration.MAX_SAMPLE)] = 10,
) -> HireCalibrationSampleOut:
    authorize(actor, CALIBRATION_WRITE)
    user = _person(actor)
    _, rubric, version = await calibration.job_rubric(session, job_id)
    items = await calibration.sample(session, job_id, user, size)
    return HireCalibrationSampleOut(
        job_id=job_id,
        rubric_version=version,
        criteria=_criteria(rubric),
        items=[
            HireCalibrationItemOut(
                application_id=i.application_id, reference=i.reference, labelled=i.labelled
            )
            for i in items
        ],
    )


async def _application(session: OrgSession, application_id: uuid.UUID) -> HireApplication:
    app = await session.get(HireApplication, application_id)
    if app is None:
        raise NotFoundError("application not found")
    return app


@router.get("/calibration/items/{application_id}", response_model=HireCalibrationApplicationOut)
async def calibration_item(
    application_id: uuid.UUID, request: Request, actor: CurrentActor, session: OrgSession
) -> HireCalibrationApplicationOut:
    authorize(actor, CALIBRATION_WRITE)
    authorize(actor, SCREENING_READ)
    user = _person(actor)
    app = await _application(session, application_id)
    row = await calibration.scored_screening(session, application_id)
    view = await screening_view(request, session, app, row)
    _, rubric = await rubric_ref(session, row.rubric_policy_id)
    return HireCalibrationApplicationOut(
        application_id=app.id,
        reference=app.reference,
        blind=view.blind,
        text=view.text,
        criteria=_criteria(rubric),
        my_scores=await calibration.my_labels(session, row.id, user),
    )


@router.put("/calibration/items/{application_id}", response_model=HireCalibrationApplicationOut)
async def calibration_label(
    application_id: uuid.UUID,
    body: HireCalibrationLabelsIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
) -> HireCalibrationApplicationOut:
    authorize(actor, CALIBRATION_WRITE)
    user = _person(actor)
    await calibration.save_labels(session, user, application_id, body.scores)
    return await calibration_item(application_id, request, actor, session)


@router.get("/calibration/report", response_model=HireCalibrationReportOut)
async def calibration_report(
    actor: CurrentActor, session: OrgSession, job_id: uuid.UUID
) -> HireCalibrationReportOut:
    authorize(actor, ANALYTICS_READ)
    report = await calibration.report(session, job_id)
    return HireCalibrationReportOut.model_validate(report.as_dict())


@router.post("/calibration/suggest", response_model=HireCalibrationSuggestionOut)
async def calibration_suggest(
    body: HireCalibrationSuggestIn, request: Request, actor: CurrentActor, session: OrgSession
) -> HireCalibrationSuggestionOut:
    authorize(actor, ANALYTICS_READ)
    user = _person(actor)
    _, rubric, _ = await calibration.job_rubric(session, body.job_id)
    criterion = (
        next((c for c in rubric.criteria if c.id == body.criterion_id), None) if rubric else None
    )
    if criterion is None:
        raise NotFoundError("criterion not found in the job's rubric")
    report = await calibration.report(session, body.job_id)
    stats = next(c for c in report.criteria if c.criterion_id == criterion.id)
    if not stats.low_agreement:
        raise ConflictError("suggestions are for criteria with low agreement")
    runtime = getattr(request.app.state, "workflow_runtime", None)
    model_router = runtime.services.router if isinstance(runtime, WorkflowRuntime) else None
    if model_router is None:
        raise ConflictError("no model is available here; use the suggestion from the anchors")
    try:
        text, recorded = await calibration.model_suggestion(
            model_router,
            actor.org_id,
            criterion,
            stats.direction or "mixed",  # type: ignore[arg-type]
            actor_id=user,
            job_id=body.job_id,
        )
    except ModelUnavailable:
        raise ConflictError(
            "the model could not answer (in recorded mode only recorded questions have an "
            "answer); use the suggestion from the anchors"
        ) from None
    return HireCalibrationSuggestionOut(
        criterion_id=criterion.id, suggestion=text, from_recording=recorded
    )
