# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Load the facts hiring analytics are computed from (``metrics``).

Everything comes from the organisation's own tables, through the caller's tenant-bound
session (Row Level Security): no sample data, no estimates here. The scope is the
applications submitted in ``[since, until)`` (dates, UTC), optionally of one job.

* applications: reference, job, source, submission time, status, current stage and
  the last update the candidate got (``hire_pipeline_entries``);
* stage moves, with undone moves and their undos left out (``hire_stage_moves``);
* the first message a person sent the candidate (``hire_messages``, outbound, queued);
  the automatic acknowledgement on applying is not a message row, so it never counts;
* the newest screening per application: its status (the human decision), overall score,
  flag codes and guardrail review reasons (``hire_screenings``);
* how many human overrides each application has (``decision_records``);
* the model calls of those screenings: cost, count, how many came from recordings
  (``model_calls``);
* interviews with their panels and whether the candidate booked through the
  self-scheduling link (the ``hire.interview.booked`` audit event says ``by: candidate``);
* the steps of the Hire approvals created in the period, with their due times.

Self-identification answers are never read here (``pairly_hire.selfid.reporting``).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.models import Approval, ApprovalStep
from pairly_core.decisions.models import DecisionRecord
from pairly_core.models import User
from pairly_core.models.audit import AuditEvent
from pairly_core.models.llm import ModelCall
from pairly_hire.messaging.models import HireMessage
from pairly_hire.models import ApplicationStatus, HireApplication, HireJob
from pairly_hire.pipeline.models import HirePipelineEntry, HireStageMove
from pairly_hire.pipeline.stages import StageSet, stages_for_jobs
from pairly_hire.scheduling.models import HireInterview, HireInterviewPanelist
from pairly_hire.screening.models import HireScreening

SCREENING_ENTITY: Final = "hire.screening"
APPLICATION_ENTITY: Final = "hire.application"
OVERRIDE_KIND: Final = "human_override"
SENT_STATUSES: Final = ("queued", "sent")
# Chunk ``IN (...)`` lists so a large organisation never builds one huge statement.
_CHUNK: Final = 1000


@dataclass(frozen=True, slots=True)
class Move:
    from_stage: str
    to_stage: str
    at: datetime


@dataclass(frozen=True, slots=True)
class ScreeningFact:
    id: uuid.UUID
    status: str
    overall: float | None
    flag_codes: tuple[str, ...]
    review_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AppFact:
    id: uuid.UUID
    job_id: uuid.UUID
    reference: str
    source: str
    submitted_at: datetime
    status: str
    stage: str
    last_update_at: datetime | None
    moves: tuple[Move, ...] = ()
    first_response_at: datetime | None = None
    screening: ScreeningFact | None = None
    overrides: int = 0
    cost_micros: int = 0
    model_calls: int = 0
    recorded_calls: int = 0


@dataclass(frozen=True, slots=True)
class InterviewFact:
    id: uuid.UUID
    application_id: uuid.UUID
    job_id: uuid.UUID
    status: str
    starts_at: datetime | None
    panel: tuple[uuid.UUID, ...]
    self_scheduled: bool


@dataclass(frozen=True, slots=True)
class ApprovalStepFact:
    approval_id: uuid.UUID
    approval_type: str
    step: str
    status: str
    due_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class Facts:
    apps: tuple[AppFact, ...]
    interviews: tuple[InterviewFact, ...] = ()
    steps: tuple[ApprovalStepFact, ...] = ()
    stages: dict[uuid.UUID, StageSet] = field(default_factory=dict)
    people: dict[uuid.UUID, str] = field(default_factory=dict)
    jobs: dict[uuid.UUID, str] = field(default_factory=dict)


def day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, UTC)


def _chunks(ids: Sequence[Any]) -> Iterable[list[Any]]:
    for i in range(0, len(ids), _CHUNK):
        yield list(ids[i : i + _CHUNK])


def effective_moves(rows: Iterable[tuple[uuid.UUID, uuid.UUID | None, Move]]) -> list[Move]:
    """Moves in time order without the ones undone and without the undos themselves."""
    items = list(rows)
    undone = {undo_of for _, undo_of, _ in items if undo_of is not None}
    kept = [m for mid, undo_of, m in items if undo_of is None and mid not in undone]
    return sorted(kept, key=lambda m: m.at)


async def _applications(
    session: AsyncSession, job_id: uuid.UUID | None, since: date | None, until: date | None
) -> list[AppFact]:
    query = (
        select(
            HireApplication.id,
            HireApplication.job_id,
            HireApplication.reference,
            HireApplication.source,
            HireApplication.submitted_at,
            HireApplication.status,
            HirePipelineEntry.stage_key,
            HirePipelineEntry.last_update_at,
        )
        .outerjoin(HirePipelineEntry, HirePipelineEntry.application_id == HireApplication.id)
        .where(
            HireApplication.status != ApplicationStatus.INCOMPLETE.value,
            HireApplication.submitted_at.is_not(None),
        )
    )
    if job_id is not None:
        query = query.where(HireApplication.job_id == job_id)
    if since is not None:
        query = query.where(HireApplication.submitted_at >= day_start(since))
    if until is not None:
        query = query.where(HireApplication.submitted_at < day_start(until))
    out: list[AppFact] = []
    for row in (await session.execute(query.order_by(HireApplication.submitted_at))).all():
        out.append(
            AppFact(
                id=row.id,
                job_id=row.job_id,
                reference=row.reference,
                source=row.source,
                submitted_at=row.submitted_at,
                status=row.status,
                stage=row.stage_key or "applied",
                last_update_at=row.last_update_at,
            )
        )
    return out


async def _moves(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[Move]]:
    raw: dict[uuid.UUID, list[tuple[uuid.UUID, uuid.UUID | None, Move]]] = defaultdict(list)
    for chunk in _chunks(ids):
        rows = await session.execute(
            select(
                HireStageMove.id,
                HireStageMove.application_id,
                HireStageMove.undo_of,
                HireStageMove.from_stage,
                HireStageMove.to_stage,
                HireStageMove.created_at,
            ).where(HireStageMove.application_id.in_(chunk))
        )
        for r in rows.all():
            raw[r.application_id].append(
                (r.id, r.undo_of, Move(r.from_stage, r.to_stage, r.created_at))
            )
    return {app_id: effective_moves(items) for app_id, items in raw.items()}


async def _first_responses(
    session: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    out: dict[uuid.UUID, datetime] = {}
    for chunk in _chunks(ids):
        rows = await session.execute(
            select(HireMessage.application_id, func.min(HireMessage.sent_at))
            .where(
                HireMessage.application_id.in_(chunk),
                HireMessage.direction == "outbound",
                HireMessage.sent_at.is_not(None),
                HireMessage.status.in_(SENT_STATUSES),
            )
            .group_by(HireMessage.application_id)
        )
        out.update({app_id: at for app_id, at in rows.all() if at is not None})
    return out


async def _screenings(
    session: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, ScreeningFact]:
    out: dict[uuid.UUID, tuple[datetime, ScreeningFact]] = {}
    for chunk in _chunks(ids):
        rows = await session.execute(
            select(
                HireScreening.id,
                HireScreening.application_id,
                HireScreening.status,
                HireScreening.overall_score,
                HireScreening.flag_codes,
                HireScreening.review_reasons,
                HireScreening.created_at,
            ).where(HireScreening.application_id.in_(chunk))
        )
        for r in rows.all():
            seen = out.get(r.application_id)
            if seen is not None and seen[0] >= r.created_at:
                continue
            overall = r.overall_score
            out[r.application_id] = (
                r.created_at,
                ScreeningFact(
                    id=r.id,
                    status=r.status,
                    overall=float(overall) if isinstance(overall, Decimal | int | float) else None,
                    flag_codes=tuple(str(c) for c in (r.flag_codes or [])),
                    review_reasons=tuple(str(c) for c in (r.review_reasons or [])),
                ),
            )
    return {k: v for k, (_, v) in out.items()}


async def _overrides(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    out: dict[uuid.UUID, int] = {}
    keys = [str(i) for i in ids]
    for chunk in _chunks(keys):
        rows = await session.execute(
            select(DecisionRecord.entity_id, func.count())
            .where(
                DecisionRecord.entity_type == APPLICATION_ENTITY,
                DecisionRecord.kind == OVERRIDE_KIND,
                DecisionRecord.entity_id.in_(chunk),
            )
            .group_by(DecisionRecord.entity_id)
        )
        out.update({uuid.UUID(e): int(n) for e, n in rows.all()})
    return out


async def _costs(
    session: AsyncSession, screening_ids: list[uuid.UUID]
) -> dict[str, tuple[int, int, int]]:
    """Per screening id: total cost (micros), calls, calls served from recordings."""
    out: dict[str, tuple[int, int, int]] = {}
    keys = [str(i) for i in screening_ids]
    for chunk in _chunks(keys):
        rows = await session.execute(
            select(
                ModelCall.entity_id,
                func.coalesce(func.sum(ModelCall.cost_micros), 0),
                func.count(),
                func.count().filter(
                    (ModelCall.from_recording.is_(True)) | (ModelCall.mode == "recorded")
                ),
            )
            .where(ModelCall.entity_type == SCREENING_ENTITY, ModelCall.entity_id.in_(chunk))
            .group_by(ModelCall.entity_id)
        )
        for entity_id, cost, calls, recorded in rows.all():
            out[str(entity_id)] = (int(cost), int(calls), int(recorded))
    return out


async def _interviews(session: AsyncSession, ids: list[uuid.UUID]) -> list[InterviewFact]:
    rows: list[Any] = []
    for chunk in _chunks(ids):
        rows.extend(
            (
                await session.execute(
                    select(
                        HireInterview.id,
                        HireInterview.application_id,
                        HireInterview.job_id,
                        HireInterview.status,
                        HireInterview.starts_at,
                    ).where(HireInterview.application_id.in_(chunk))
                )
            ).all()
        )
    interview_ids = [r.id for r in rows]
    panels: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    booked_by_candidate: set[str] = set()
    for chunk in _chunks(interview_ids):
        for p in (
            await session.execute(
                select(HireInterviewPanelist.interview_id, HireInterviewPanelist.user_id).where(
                    HireInterviewPanelist.interview_id.in_(chunk),
                    HireInterviewPanelist.removed_at.is_(None),
                )
            )
        ).all():
            panels[p.interview_id].append(p.user_id)
        found = await session.scalars(
            select(AuditEvent.entity_id).where(
                AuditEvent.action == "hire.interview.booked",
                AuditEvent.entity_id.in_([str(i) for i in chunk]),
                AuditEvent.payload["by"].astext == "candidate",
            )
        )
        booked_by_candidate |= {str(e) for e in found if e}
    return [
        InterviewFact(
            id=r.id,
            application_id=r.application_id,
            job_id=r.job_id,
            status=r.status,
            starts_at=r.starts_at,
            panel=tuple(sorted(panels.get(r.id, []))),
            self_scheduled=str(r.id) in booked_by_candidate,
        )
        for r in rows
    ]


async def _steps(
    session: AsyncSession, since: date | None, until: date | None
) -> list[ApprovalStepFact]:
    query = (
        select(
            Approval.id,
            Approval.approval_type,
            ApprovalStep.name,
            ApprovalStep.status,
            ApprovalStep.due_at,
            ApprovalStep.completed_at,
        )
        .join(ApprovalStep, ApprovalStep.approval_id == Approval.id)
        .where(Approval.workflow == "hire")
    )
    if since is not None:
        query = query.where(Approval.created_at >= day_start(since))
    if until is not None:
        query = query.where(Approval.created_at < day_start(until))
    return [
        ApprovalStepFact(
            approval_id=r.id,
            approval_type=r.approval_type,
            step=r.name,
            status=r.status,
            due_at=r.due_at,
            completed_at=r.completed_at,
        )
        for r in (await session.execute(query)).all()
    ]


async def _people(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        select(User.id, User.display_name, User.email).where(User.id.in_(list(ids)))
    )
    return {r.id: (r.display_name or r.email) for r in rows.all()}


async def load_facts(
    session: AsyncSession,
    *,
    job_id: uuid.UUID | None = None,
    since: date | None = None,
    until: date | None = None,
) -> Facts:
    apps = await _applications(session, job_id, since, until)
    ids = [a.id for a in apps]
    moves = await _moves(session, ids)
    responses = await _first_responses(session, ids)
    screenings = await _screenings(session, ids)
    overrides = await _overrides(session, ids)
    costs = await _costs(session, [s.id for s in screenings.values()])
    enriched: list[AppFact] = []
    for a in apps:
        screening = screenings.get(a.id)
        cost, calls, recorded = costs.get(str(screening.id), (0, 0, 0)) if screening else (0, 0, 0)
        enriched.append(
            AppFact(
                id=a.id,
                job_id=a.job_id,
                reference=a.reference,
                source=a.source,
                submitted_at=a.submitted_at,
                status=a.status,
                stage=a.stage,
                last_update_at=a.last_update_at,
                moves=tuple(moves.get(a.id, [])),
                first_response_at=responses.get(a.id),
                screening=screening,
                overrides=overrides.get(a.id, 0),
                cost_micros=cost,
                model_calls=calls,
                recorded_calls=recorded,
            )
        )
    interviews = await _interviews(session, ids)
    job_ids = sorted({a.job_id for a in apps} | ({job_id} if job_id else set()))
    stages = await stages_for_jobs(session, job_ids) if job_ids else {}
    titles = (
        dict(
            (
                await session.execute(
                    select(HireJob.id, HireJob.title).where(HireJob.id.in_(job_ids))
                )
            ).all()
        )
        if job_ids
        else {}
    )
    panel_people = {p for i in interviews for p in i.panel}
    return Facts(
        apps=tuple(enriched),
        interviews=tuple(interviews),
        steps=tuple(await _steps(session, since, until)),
        stages=dict(stages),
        people=await _people(session, panel_people),
        jobs={k: str(v) for k, v in titles.items()},
    )
