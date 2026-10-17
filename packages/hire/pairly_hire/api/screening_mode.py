# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The recruiter's screening queue (WP2.6, brief 5.3 "Recruiter screening mode").

``GET /v1/hire/jobs/{job_id}/screening-queue`` (``hire.candidate.read`` and
``hire.screening.read``): every submitted application of the job in a stable order
(oldest first, so the position in the queue never jumps after an action), each with its
label (blind: "Candidate <reference>", ``identity_hidden``), stage, tags, the latest
screening's status and overall score, its flag codes, whether it needs a person, and its
open message. Plus what the screen needs around it: the job's stages, the
organisation's blind mode (and whether the caller may change it), and whether scheduling
can take the "S: schedule" action yet (``pairly_hire.interviews``).

Per candidate, the screen reads the existing routes: the explainability panel
(``/applications/{id}/screening``, with rubric anchors for "why not higher") and the
profile (``/pipeline/applications/{id}``). It acts through the pipeline (advance, moves
and undo, notes, tags, bulk with a summary first), messaging (a rejection draft that goes
to the approval queue, never sent at once) and the screening override.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Final

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on

from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import authorize
from pairly_hire.api.pipeline import HireStageOut
from pairly_hire.interviews import scheduling_available
from pairly_hire.jobs import get_job
from pairly_hire.messaging.models import HireMessage
from pairly_hire.models import ApplicationStatus, HireApplication, HireCandidate
from pairly_hire.permissions import CANDIDATE_READ
from pairly_hire.pipeline.models import HirePipelineEntry
from pairly_hire.pipeline.records import AppRecord, hidden_identities
from pairly_hire.pipeline.stages import FIRST, stages_for
from pairly_hire.pipeline.views import OPEN_MESSAGE
from pairly_hire.screening.models import HireScreening
from pairly_hire.screening.permissions import SCREENING_MANAGE, SCREENING_READ
from pairly_hire.screening.service import screening_settings

router = APIRouter(tags=["hire"])

MAX_QUEUE: Final = 500


class HireQueueItemOut(BaseModel):
    application_id: uuid.UUID
    reference: str
    label: str
    identity_hidden: bool
    stage_key: str
    stage_kind: str
    tags: list[str]
    screening_status: str | None
    scored: bool
    overall_score: float | None
    flag_codes: list[str]
    needs_human_review: bool
    open_message: str | None
    open_message_id: uuid.UUID | None
    submitted_at: datetime | None


class HireScreeningQueueOut(BaseModel):
    job_id: uuid.UUID
    job_title: str
    blind_mode: bool
    blind_universities: bool
    can_change_blind_mode: bool
    scheduling_available: bool
    stages: list[HireStageOut]
    items: list[HireQueueItemOut]


def _float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


@router.get("/jobs/{job_id}/screening-queue", response_model=HireScreeningQueueOut)
async def screening_queue(
    job_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> HireScreeningQueueOut:
    authorize(actor, CANDIDATE_READ)
    authorize(actor, SCREENING_READ)
    job = await get_job(session, job_id)
    stages = await stages_for(session, job.id)
    rows = (
        await session.execute(
            select(HireApplication, HireCandidate, HirePipelineEntry)
            .join(HireCandidate, HireCandidate.id == HireApplication.candidate_id)
            .outerjoin(HirePipelineEntry, HirePipelineEntry.application_id == HireApplication.id)
            .where(
                HireApplication.job_id == job.id,
                HireApplication.status != ApplicationStatus.INCOMPLETE.value,
            )
            .order_by(HireApplication.submitted_at.asc(), HireApplication.id)
            .limit(MAX_QUEUE)
        )
    ).all()
    apps = [a for a, _, _ in rows]
    ids = [a.id for a in apps]
    hidden = await hidden_identities(session, apps)
    latest: dict[uuid.UUID, HireScreening] = (
        {
            s.application_id: s
            for s in await session.scalars(
                select(HireScreening)
                .where(HireScreening.application_id.in_(ids))
                .ext(distinct_on(HireScreening.application_id))
                .order_by(HireScreening.application_id, HireScreening.created_at.desc())
            )
        }
        if ids
        else {}
    )
    opened: dict[uuid.UUID, tuple[str, uuid.UUID]] = {}
    if ids:
        for m in await session.scalars(
            select(HireMessage)
            .where(HireMessage.application_id.in_(ids), HireMessage.status.in_(OPEN_MESSAGE))
            .order_by(HireMessage.created_at.asc())
        ):
            opened[m.application_id] = (m.status, m.id)
    items: list[HireQueueItemOut] = []
    for app, candidate, entry in rows:
        record = AppRecord(app, candidate, job, entry, stages)
        stage = record.stage
        screening = latest.get(app.id)
        message = opened.get(app.id)
        items.append(
            HireQueueItemOut(
                application_id=app.id,
                reference=app.reference,
                label=record.label(app.id in hidden),
                identity_hidden=app.id in hidden,
                stage_key=record.stage_key if stages.get(record.stage_key) else FIRST,
                stage_kind=stage.kind,
                tags=list(entry.tags or []) if entry is not None else [],
                screening_status=screening.status if screening is not None else None,
                scored=screening is not None and screening.overall_score is not None,
                overall_score=_float(screening.overall_score) if screening is not None else None,
                flag_codes=list(screening.flag_codes) if screening is not None else [],
                needs_human_review=app.needs_human_review,
                open_message=message[0] if message else None,
                open_message_id=message[1] if message else None,
                submitted_at=app.submitted_at,
            )
        )
    settings = await screening_settings(session)
    return HireScreeningQueueOut(
        job_id=job.id,
        job_title=job.title,
        blind_mode=settings.blind.enabled,
        blind_universities=settings.blind.universities,
        can_change_blind_mode=actor.has(SCREENING_MANAGE),
        scheduling_available=scheduling_available(),
        stages=[HireStageOut(**s.model_dump()) for s in stages.stages],
        items=items,
    )
