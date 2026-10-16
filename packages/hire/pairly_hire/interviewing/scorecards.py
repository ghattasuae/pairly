# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Independent scorecards (brief 3.7.2, 5.3): each interviewer submits their own.

* A seat is one interview and one person on its panel (``removed_at`` NULL). Only that
  person reads or writes that scorecard; there is no other way in.
* Criteria: the plan stage's (``plan.interview_stage``) when the scorecard is created,
  kept on the row, so a later plan edit never changes what was rated.
* ``save``: a draft (partial ratings allowed). ``submit``: every criterion rated 1 to 4
  and an overall recommendation; final (the API refuses changes, and a trigger too).
* Hidden until you submit, enforced here: ``others`` returns nothing until the
  viewer's own scorecard for that interview is submitted. A panelist who has not
  submitted never reads anyone else's scorecards for that candidate and stage, on any
  route (``visible_scorecards``). People who are not on the panel read them only with
  ``hire.scorecard.read``.
* SLA: due ``sla_hours`` (plan) after the interview ends; ``sla_state``.
* "Remind me later" (``snooze``): no reminder for ``SNOOZE`` hours, then one more.

Notes are free text about the candidate: while the candidate is blind, others read
them through the blind redactor. Every change is audited (``hire.scorecard.*``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from pairly_hire.interviewing.models import RECOMMENDATIONS, HireScorecard
from pairly_hire.interviewing.plan import (
    RATING_MAX,
    RATING_MIN,
    LoadedPlan,
    PlanStage,
    criteria_by_id,
    interview_stage,
)
from pairly_hire.pipeline.hooks import application_blind, identity_redactor
from pairly_hire.pipeline.records import AppRecord, load_record
from pairly_hire.scheduling.models import HireInterview, HireInterviewPanelist, InterviewStatus
from pairly_hire.scheduling.service import panel_people

WORKFLOW: Final = "hire"
ENTITY: Final = "hire.scorecard"
SNOOZE: Final = timedelta(hours=3)
DUE_SOON: Final = timedelta(hours=4)
MAX_NOTES: Final = 1000
SCOREABLE: Final = frozenset(
    {
        InterviewStatus.BOOKED.value,
        InterviewStatus.RESCHEDULED.value,
        InterviewStatus.COMPLETED.value,
    }
)


@dataclass(frozen=True, slots=True)
class Criterion:
    id: str
    name: str
    question: str
    anchors: list[str]


@dataclass(frozen=True, slots=True)
class OtherScorecard:
    interviewer_id: uuid.UUID
    interviewer_name: str
    ratings: dict[str, int]
    notes: str
    recommendation: str
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class ScorecardView:
    interview: HireInterview
    record: AppRecord
    stage: PlanStage
    loaded: LoadedPlan
    locale: str
    criteria: list[Criterion]
    scorecard: HireScorecard | None
    due_at: datetime | None
    sla_state: str
    others: list[OtherScorecard] | None
    others_submitted: int
    panel_size: int
    candidate_label: str = ""
    identity_hidden: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def submitted(self) -> bool:
        return self.scorecard is not None and self.scorecard.status == "submitted"


def sla_state(due_at: datetime | None, submitted: bool, now: datetime) -> str:
    if submitted:
        return "submitted"
    if due_at is None:
        return "on_time"
    if now >= due_at:
        return "overdue"
    if due_at - now <= DUE_SOON:
        return "due_soon"
    return "on_time"


async def _interview(
    session: AsyncSession, interview_id: uuid.UUID, *, lock: bool
) -> HireInterview:
    query = select(HireInterview).where(HireInterview.id == interview_id)
    if lock:
        query = query.with_for_update()
    interview = await session.scalar(query)
    if interview is None:
        raise NotFoundError("interview not found")
    return interview


async def on_panel(session: AsyncSession, interview_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(HireInterviewPanelist.id).where(
            HireInterviewPanelist.interview_id == interview_id,
            HireInterviewPanelist.user_id == user_id,
            HireInterviewPanelist.removed_at.is_(None),
        )
    )
    return found is not None


async def _own(
    session: AsyncSession, interview_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False
) -> HireScorecard | None:
    query = select(HireScorecard).where(
        HireScorecard.interview_id == interview_id, HireScorecard.interviewer_id == user_id
    )
    if lock:
        query = query.with_for_update()
    return await session.scalar(query)


def _criteria(stage: PlanStage, loaded: LoadedPlan, ids: list[str], locale: str) -> list[Criterion]:
    known = criteria_by_id(loaded.rubric)
    out: list[Criterion] = []
    for cid in ids:
        question = stage.question_for(cid)
        found = known.get(cid)
        name = found[0].name if found else cid
        if question is None:
            out.append(Criterion(cid, name, "", list(stage.questions[0].scale(locale))))
            continue
        text = (
            question.question_ar if locale == "ar" and question.question_ar else question.question
        )
        out.append(Criterion(cid, name, text, list(question.scale(locale))))
    return out


async def _redactor_for(session: AsyncSession, record: AppRecord, hidden: bool) -> Any:
    if not hidden:
        return None
    return await identity_redactor(session, record.app.id, record.identity_contacts())


async def stage_scorecards(
    session: AsyncSession, application_id: uuid.UUID, stage_key: str
) -> list[HireScorecard]:
    """Submitted scorecards of an application at a plan stage (interviews not cancelled)."""
    return list(
        await session.scalars(
            select(HireScorecard)
            .join(HireInterview, HireInterview.id == HireScorecard.interview_id)
            .where(
                HireScorecard.application_id == application_id,
                HireScorecard.stage_key == stage_key,
                HireScorecard.status == "submitted",
                HireInterview.status.in_(SCOREABLE),
            )
            .order_by(HireScorecard.submitted_at, HireScorecard.id)
        )
    )


async def _others(
    session: AsyncSession,
    record: AppRecord,
    stage_key: str,
    viewer_id: uuid.UUID | None,
    hidden: bool,
) -> list[OtherScorecard]:
    rows = [
        r
        for r in await stage_scorecards(session, record.app.id, stage_key)
        if r.interviewer_id != viewer_id
    ]
    people = {p.id: p.name for p in await panel_people(session, [r.interviewer_id for r in rows])}
    redact = await _redactor_for(session, record, hidden)
    out: list[OtherScorecard] = []
    for r in rows:
        assert r.submitted_at is not None  # noqa: S101 (submitted)
        out.append(
            OtherScorecard(
                interviewer_id=r.interviewer_id,
                interviewer_name=people.get(r.interviewer_id, "Interviewer"),
                ratings={str(k): int(v) for k, v in r.ratings.items()},
                notes=redact(r.notes) if redact is not None else r.notes,
                recommendation=r.recommendation or "",
                submitted_at=r.submitted_at,
            )
        )
    return out


async def scorecard_view(
    session: AsyncSession,
    interview_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    locale: str = "en",
    now: datetime | None = None,
) -> ScorecardView:
    """The person's own scorecard of an interview (they must be on its panel), and the
    others' scorecards only once theirs is submitted."""
    now = now or datetime.now(UTC)
    interview = await _interview(session, interview_id, lock=False)
    if not await on_panel(session, interview.id, user_id):
        raise NotFoundError("interview not found")
    record = await load_record(session, interview.application_id)
    found = await interview_stage(session, record.job, interview.stage_key)
    if found is None:
        raise ConflictError("this job has no interview plan yet: lock its rubric first")
    stage, loaded = found
    own = await _own(session, interview.id, user_id)
    ids = list(own.criteria) if own is not None else stage.criteria()
    due = interview.ends_at + timedelta(hours=loaded.plan.sla_hours) if interview.ends_at else None
    submitted = own is not None and own.status == "submitted"
    blind = await application_blind(session, record.app.id)
    hidden = record.identity_hidden(blind)
    stage_key = own.stage_key if own is not None else stage.key
    others = await _others(session, record, stage_key, user_id, hidden) if submitted else None
    count = len(
        [
            r
            for r in await stage_scorecards(session, record.app.id, stage_key)
            if r.interviewer_id != user_id
        ]
    )
    panel = await session.scalars(
        select(HireInterviewPanelist.user_id).where(
            HireInterviewPanelist.interview_id == interview.id,
            HireInterviewPanelist.removed_at.is_(None),
        )
    )
    return ScorecardView(
        interview=interview,
        record=record,
        stage=stage,
        loaded=loaded,
        locale="ar" if locale == "ar" else "en",
        criteria=_criteria(stage, loaded, ids, locale),
        scorecard=own,
        due_at=due,
        sla_state=sla_state(due, submitted, now),
        others=others,
        others_submitted=count,
        panel_size=len(list(panel)),
        candidate_label=record.label(blind),
        identity_hidden=hidden,
    )


def _clean_ratings(
    raw: Mapping[str, Any], criteria: list[str], *, complete: bool
) -> dict[str, int]:
    ratings: dict[str, int] = {}
    for key, value in raw.items():
        if key not in criteria:
            raise BadRequestError(f"{key!r} is not rated at this stage")
        if isinstance(value, bool) or not isinstance(value, int):
            raise BadRequestError("ratings are whole numbers from 1 to 4")
        if not RATING_MIN <= value <= RATING_MAX:
            raise BadRequestError("ratings are whole numbers from 1 to 4")
        ratings[key] = value
    if complete:
        missing = [c for c in criteria if c not in ratings]
        if missing:
            raise BadRequestError(f"rate every criterion first: {', '.join(missing)}")
    return ratings


async def _seat_for_write(
    session: AsyncSession, interview_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[HireInterview, HireScorecard]:
    interview = await _interview(session, interview_id, lock=True)
    if not await on_panel(session, interview.id, user_id):
        raise NotFoundError("interview not found")
    if interview.status not in SCOREABLE:
        raise ConflictError("this interview is not taking scorecards")
    own = await _own(session, interview.id, user_id, lock=True)
    if own is None:
        record = await load_record(session, interview.application_id)
        found = await interview_stage(session, record.job, interview.stage_key)
        if found is None:
            raise ConflictError("this job has no interview plan yet: lock its rubric first")
        stage, _loaded = found
        now = datetime.now(UTC)
        own = HireScorecard(
            id=uuid.uuid4(),
            org_id=interview.org_id,
            interview_id=interview.id,
            application_id=interview.application_id,
            job_id=interview.job_id,
            stage_key=stage.key,
            interviewer_id=user_id,
            criteria=stage.criteria(),
            ratings={},
            notes="",
            status="draft",
            created_at=now,
            updated_at=now,
        )
        session.add(own)
        await session.flush()
    return interview, own


async def _audit(
    session: AsyncSession, action: str, row: HireScorecard, extra: dict[str, Any]
) -> None:
    await append_audit(
        session,
        action=action,
        actor_type=ActorType.USER,
        actor_id=str(row.interviewer_id),
        entity_type=ENTITY,
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={
            "interview_id": str(row.interview_id),
            "application_id": str(row.application_id),
            "stage_key": row.stage_key,
            "status": row.status,
        }
        | extra,
    )


async def save(
    session: AsyncSession,
    interview_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    ratings: Mapping[str, Any],
    notes: str,
    recommendation: str | None,
    submit: bool,
) -> HireScorecard:
    _iv, own = await _seat_for_write(session, interview_id, user_id)
    if own.status == "submitted":
        raise ConflictError("you already submitted this scorecard")
    criteria = list(own.criteria)
    clean = _clean_ratings(ratings, criteria, complete=submit)
    text = notes.strip()
    if len(text) > MAX_NOTES:
        raise BadRequestError(f"notes are at most {MAX_NOTES} characters")
    if recommendation is not None and recommendation not in RECOMMENDATIONS:
        raise BadRequestError("unknown recommendation")
    if submit and recommendation is None:
        raise BadRequestError("choose an overall recommendation")
    now = datetime.now(UTC)
    own.ratings = clean
    own.notes = text
    own.recommendation = recommendation
    own.updated_at = now
    if submit:
        own.status = "submitted"
        own.submitted_at = now
        own.snoozed_until = None
    await session.flush()
    await _audit(
        session,
        "hire.scorecard.submitted" if submit else "hire.scorecard.saved",
        own,
        {"rated": sorted(clean), "recommendation": recommendation}
        if submit
        else {"rated": sorted(clean)},
    )
    return own


async def snooze(
    session: AsyncSession,
    interview_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> HireScorecard:
    """Remind me later: no reminder for ``SNOOZE``, then one more."""
    now = now or datetime.now(UTC)
    _interview_row, own = await _seat_for_write(session, interview_id, user_id)
    if own.status == "submitted":
        raise ConflictError("you already submitted this scorecard")
    own.snoozed_until = now + SNOOZE
    own.updated_at = now
    await session.flush()
    await _audit(session, "hire.scorecard.snoozed", own, {"until": own.snoozed_until.isoformat()})
    return own


@dataclass(frozen=True, slots=True)
class Visible:
    # True: the viewer sits on the panel and has not submitted: nothing is shown.
    hidden: bool
    stage_key: str
    items: list[OtherScorecard]
    expected: int


async def panel_seats(
    session: AsyncSession, application_id: uuid.UUID, stage_key: str, job: Any
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(interview id, user id) of every active seat whose interview uses ``stage_key``."""
    interviews = list(
        await session.scalars(
            select(HireInterview).where(
                HireInterview.application_id == application_id,
                HireInterview.status.in_(SCOREABLE),
            )
        )
    )
    seats: list[tuple[uuid.UUID, uuid.UUID]] = []
    for interview in interviews:
        found = await interview_stage(session, job, interview.stage_key)
        if found is None or found[0].key != stage_key:
            continue
        for user_id in await session.scalars(
            select(HireInterviewPanelist.user_id).where(
                HireInterviewPanelist.interview_id == interview.id,
                HireInterviewPanelist.removed_at.is_(None),
            )
        ):
            seats.append((interview.id, user_id))
    return seats


async def visible_scorecards(
    session: AsyncSession,
    application_id: uuid.UUID,
    stage_key: str,
    viewer_id: uuid.UUID | None,
    *,
    can_read_all: bool,
) -> Visible:
    """Everyone's submitted scorecards for a candidate and stage, as ``viewer_id`` may
    see them: nothing while they sit on that panel without having submitted."""
    record = await load_record(session, application_id)
    seats = await panel_seats(session, application_id, stage_key, record.job)
    mine = [iv for iv, user in seats if user == viewer_id]
    if mine:
        own = list(
            await session.scalars(
                select(HireScorecard).where(
                    HireScorecard.interview_id.in_(mine),
                    HireScorecard.interviewer_id == viewer_id,
                    HireScorecard.status == "submitted",
                )
            )
        )
        if len(own) < len(set(mine)):
            return Visible(True, stage_key, [], len(seats))
    elif not can_read_all:
        raise ForbiddenError("only the panel and the hiring team read scorecards")
    hidden = record.identity_hidden(await application_blind(session, record.app.id))
    others = await _others(session, record, stage_key, None, hidden)
    return Visible(False, stage_key, others, len(seats))
