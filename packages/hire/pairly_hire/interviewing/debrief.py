# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The debrief summary (brief 3.7.4, 5.3): when every scorecard of a stage is in, one
page for the hiring manager: criteria, ratings, disagreements, open questions and the
interviewers' recommendations. Humans decide.

* Ready when every person on the panel of every interview of the application at that
  plan stage has submitted (``scorecards.panel_seats``). Before that: who is still due,
  and nothing else (scorecards stay independent).
* The numbers are computed here (``aggregate``): ratings per criterion, mean, spread, a
  disagreement when ratings are two or more apart. Never a model's.
* The model (prompt ``hire_debrief_summary/v1`` through the router; recorded for the
  demo) writes the plain-language summary, per criterion notes, disagreements and open
  questions. What it sees (``debrief_inputs``): the job title, the stage, the criteria
  and their ratings, and per interviewer a label ("Interviewer 1"), the recommendation
  and the notes. Never a name; while the candidate is blind the notes go through the
  blind redactor first. ``check_draft`` refuses unsafe text and unknown criteria; a
  refused or failed call falls back to ``template_draft``, built from the numbers.
* Kept per hash of the scorecards it summarises; audited (``hire.debrief.generated``).
* The decision (``decide``): advance, hold or reject, with a reason, by a person who may
  decide on candidates (second factor, the API checks), recorded as a decision record
  (``approval`` kind) and audited. It does not move the candidate: the pipeline does,
  as always, so nothing happens without a person.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from statistics import mean
from typing import TYPE_CHECKING, Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.decisions.models import DecisionKind
from pairly_core.decisions.service import DecisionDraft, record_decision
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.llm.errors import ModelUnavailable
from pairly_core.llm.types import CallContext, TaskSpec
from pairly_hire.interviewing.kits import _problems, stage_name
from pairly_hire.interviewing.models import DECISIONS, HireDebrief, HireScorecard
from pairly_hire.interviewing.plan import criteria_by_id, plan_for
from pairly_hire.interviewing.scorecards import panel_seats, stage_scorecards
from pairly_hire.pipeline.hooks import application_blind, identity_redactor
from pairly_hire.pipeline.records import AppRecord, load_record
from pairly_hire.scheduling.service import panel_people

if TYPE_CHECKING:
    from pairly_core.llm.router import ModelRouter

TASK: Final = TaskSpec(name="hire_debrief_summary", prompt_version="v1")
PROMPT_VERSION: Final = f"{TASK.name}/{TASK.prompt_version}"
TEMPLATE_VERSION: Final = "template/v1"
WORKFLOW: Final = "hire"
ENTITY: Final = "hire.application"
DISAGREE_SPREAD: Final = 2

_Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]


class CriterionNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,47}$")]
    summary: _Line


class DebriefDraft(BaseModel):
    """What the model returns."""

    model_config = ConfigDict(extra="forbid")

    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=600)]
    criteria: list[CriterionNote] = Field(default_factory=list, max_length=5)
    disagreements: list[_Line] = Field(default_factory=list, max_length=5)
    open_questions: list[_Line] = Field(default_factory=list, max_length=5)


# ------------------------------------------------------------------ numbers


@dataclass(frozen=True, slots=True)
class Rated:
    criterion_id: str
    name: str
    # (interviewer label, interviewer name, rating) in interviewer order.
    ratings: list[tuple[str, str, int]]

    @property
    def values(self) -> list[int]:
        return [r for _, _, r in self.ratings]

    @property
    def mean(self) -> float | None:
        return round(mean(self.values), 1) if self.values else None

    @property
    def spread(self) -> int:
        return max(self.values) - min(self.values) if self.values else 0

    @property
    def disagreement(self) -> bool:
        return self.spread >= DISAGREE_SPREAD


@dataclass(frozen=True, slots=True)
class Voice:
    label: str
    name: str
    recommendation: str
    notes: str


def debrief_inputs(
    *, job_title: str, stage: str, locale: str, rated: list[Rated], voices: list[Voice]
) -> dict[str, Any]:
    return {
        "job_title": job_title,
        "stage": stage,
        "language": "ar" if locale == "ar" else "en",
        "criteria": [{"id": r.criterion_id, "name": r.name, "ratings": r.values} for r in rated],
        "interviewers": [
            {"label": v.label, "recommendation": v.recommendation, "notes": v.notes} for v in voices
        ],
    }


def check_draft(draft: DebriefDraft, inputs: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    known = {c["id"] for c in inputs.get("criteria", [])}
    problems += _problems("summary", draft.summary)
    for i, c in enumerate(draft.criteria):
        if c.criterion_id not in known:
            problems.append(f"criteria[{i}]: unknown criterion {c.criterion_id!r}")
        problems += _problems(f"criteria[{i}]", c.summary)
    for i, d in enumerate(draft.disagreements):
        problems += _problems(f"disagreements[{i}]", d)
    for i, q in enumerate(draft.open_questions):
        problems += _problems(f"open_questions[{i}]", q)
    return sorted(set(problems))


_REC_EN: Final = {"strong_no": "strong no", "no": "no", "yes": "yes", "strong_yes": "strong yes"}
_REC_AR: Final = {"strong_no": "لا بشدة", "no": "لا", "yes": "نعم", "strong_yes": "نعم بشدة"}


def template_draft(inputs: dict[str, Any]) -> DebriefDraft:
    """The summary from the numbers alone (no model): also the demo's scripted answer."""
    ar = inputs.get("language") == "ar"
    criteria = list(inputs.get("criteria", []))
    voices = list(inputs.get("interviewers", []))
    recs = _REC_AR if ar else _REC_EN
    tally = ", ".join(
        f"{v['label']}: {recs.get(v['recommendation'], v['recommendation'])}" for v in voices
    )
    notes: list[CriterionNote] = []
    disagreements: list[str] = []
    questions: list[str] = []
    for c in criteria[:5]:
        values = [int(x) for x in c.get("ratings", [])]
        if not values:
            continue
        avg = round(mean(values), 1)
        low, high = min(values), max(values)
        if ar:
            notes.append(
                CriterionNote(
                    criterion_id=c["id"],
                    summary=f"{c['name']}: متوسط {avg} من 4، من {low} إلى {high}.",
                )
            )
        else:
            notes.append(
                CriterionNote(
                    criterion_id=c["id"],
                    summary=f"{c['name']}: average {avg} of 4, from {low} to {high}.",
                )
            )
        if high - low >= DISAGREE_SPREAD:
            labels = [
                voices[i]["label"]
                for i, v in enumerate(values)
                if v in {low, high} and i < len(voices)
            ]
            joined = (" و" if ar else " and ").join(labels)
            disagreements.append(
                f"{c['name']}: تتراوح التقييمات من {low} إلى {high} ({joined})."
                if ar
                else f"{c['name']}: ratings range from {low} to {high} ({joined})."
            )
        if low <= 2:
            questions.append(
                f"{c['name']}: قيمه مقابل واحد على الأقل بـ 2 أو أقل. ما الدليل الذي ينقص؟"
                if ar
                else f"{c['name']}: at least one interviewer rated it 2 or lower. What evidence "
                "is missing?"
            )
    positive = {v["recommendation"] for v in voices} & {"yes", "strong_yes"}
    negative = {v["recommendation"] for v in voices} & {"no", "strong_no"}
    if positive and negative:
        disagreements.append(
            "التوصيات العامة منقسمة بين نعم ولا."
            if ar
            else "The overall recommendations split between yes and no."
        )
    if ar:
        summary = f"أرسل {len(voices)} من المقابلين تقييماتهم. التوصيات: {tally}."
    else:
        summary = f"{len(voices)} interviewers submitted scorecards. Recommendations: {tally}."
    return DebriefDraft(
        summary=summary[:600],
        criteria=notes,
        disagreements=[d[:300] for d in disagreements[:5]],
        open_questions=[q[:300] for q in questions[:5]],
    )


# ------------------------------------------------------------------ the debrief


@dataclass(frozen=True, slots=True)
class Debrief:
    record: AppRecord
    stage_key: str
    stage_name: str
    locale: str
    ready: bool
    expected: int
    submitted: int
    waiting_for: list[str]
    rated: list[Rated]
    voices: list[Voice]
    draft: DebriefDraft | None
    source: str
    prompt_version: str
    from_recording: bool
    row: HireDebrief | None
    candidate_label: str
    identity_hidden: bool


def _hash(rows: list[HireScorecard]) -> str:
    raw = json.dumps(
        sorted(
            [
                {"id": str(r.id), "at": r.submitted_at.isoformat() if r.submitted_at else ""}
                for r in rows
            ],
            key=lambda x: x["id"],
        ),
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _material(
    session: AsyncSession, record: AppRecord, stage_key: str, locale: str
) -> tuple[list[HireScorecard], list[Rated], list[Voice], list[uuid.UUID], int, bool, str]:
    loaded = await plan_for(session, record.job)
    if loaded is None:
        raise NotFoundError("this job has no interview plan")
    stage = loaded.plan.stage(stage_key)
    if stage is None:
        raise NotFoundError("this stage has no interview plan")
    seats = await panel_seats(session, record.app.id, stage_key, record.job)
    rows = await stage_scorecards(session, record.app.id, stage_key)
    submitted_by = {(r.interview_id, r.interviewer_id) for r in rows}
    missing = [user for iv, user in seats if (iv, user) not in submitted_by]
    blind = await application_blind(session, record.app.id)
    hidden = record.identity_hidden(blind)
    redact = (
        await identity_redactor(session, record.app.id, record.identity_contacts())
        if hidden
        else None
    )
    names = {
        p.id: p.name for p in await panel_people(session, list({r.interviewer_id for r in rows}))
    }
    voices: list[Voice] = []
    for n, r in enumerate(rows, start=1):
        label = f"المقابل {n}" if locale == "ar" else f"Interviewer {n}"
        notes = redact(r.notes) if redact is not None else r.notes
        voices.append(
            Voice(label, names.get(r.interviewer_id, label), r.recommendation or "", notes)
        )
    known = criteria_by_id(loaded.rubric)
    order: list[str] = []
    for r in rows:
        for cid in r.criteria:
            if cid not in order:
                order.append(cid)
    if not order:
        order = stage.criteria()
    rated: list[Rated] = []
    for cid in order:
        values: list[tuple[str, str, int]] = []
        for voice, r in zip(voices, rows, strict=True):
            value = r.ratings.get(cid)
            if isinstance(value, int):
                values.append((voice.label, voice.name, value))
        found = known.get(cid)
        rated.append(Rated(cid, found[0].name if found else cid, values))
    return rows, rated, voices, missing, len(seats), hidden, stage_name(stage, locale)


async def build_debrief(
    session: AsyncSession,
    application_id: uuid.UUID,
    stage_key: str,
    locale: str,
    *,
    router: ModelRouter | None,
    actor_id: uuid.UUID | None,
) -> Debrief:
    locale = "ar" if locale == "ar" else "en"
    record = await load_record(session, application_id)
    rows, rated, voices, missing, expected, hidden, sname = await _material(
        session, record, stage_key, locale
    )
    blind = await application_blind(session, record.app.id)
    label = record.label(blind)
    waiting = [p.name for p in await panel_people(session, list(dict.fromkeys(missing)))]
    ready = expected > 0 and not missing and bool(rows)
    empty = Debrief(
        record=record,
        stage_key=stage_key,
        stage_name=sname,
        locale=locale,
        ready=ready,
        expected=expected,
        submitted=len(rows),
        waiting_for=waiting,
        rated=[],
        voices=[],
        draft=None,
        source="none",
        prompt_version="",
        from_recording=False,
        row=None,
        candidate_label=label,
        identity_hidden=hidden,
    )
    if not ready:
        # Scorecards stay independent until everyone has submitted.
        return empty
    digest = _hash(rows)
    existing = await session.scalar(
        select(HireDebrief).where(
            HireDebrief.application_id == record.app.id,
            HireDebrief.stage_key == stage_key,
            HireDebrief.scorecards_hash == digest,
        )
    )
    inputs = debrief_inputs(
        job_title=record.job.title_ar
        if locale == "ar" and record.job.title_ar
        else record.job.title,
        stage=sname,
        locale=locale,
        rated=rated,
        voices=voices,
    )
    if existing is not None:
        stored = existing.content.get(locale)
        try:
            draft: DebriefDraft = (
                DebriefDraft.model_validate(stored) if stored else template_draft(inputs)
            )
        except ValueError:
            stored, draft = None, template_draft(inputs)
        return replace(
            empty,
            rated=rated,
            voices=voices,
            draft=draft,
            source=existing.source if stored else "template",
            prompt_version=existing.prompt_version if stored else TEMPLATE_VERSION,
            from_recording=existing.from_recording if stored else False,
            row=existing,
        )
    draft = template_draft(inputs)
    source, version, call_id, recorded = "template", TEMPLATE_VERSION, None, False
    if router is not None:
        try:
            result = await router.complete(
                task=TASK,
                inputs=inputs,
                schema=DebriefDraft,
                org_id=record.app.org_id,
                context=CallContext(
                    actor_id=str(actor_id) if actor_id else None,
                    workflow=WORKFLOW,
                    entity_type=ENTITY,
                    entity_id=str(record.app.id),
                ),
            )
        except ModelUnavailable:
            result = None
        if result is not None:
            call_id = result.call_id
            if not check_draft(result.value, inputs):
                draft, source, version, recorded = (
                    result.value,
                    "model",
                    PROMPT_VERSION,
                    result.from_recording,
                )
    if source == "template":
        # Nothing from the model: show the numbers' summary, keep nothing, try again later.
        return replace(
            empty,
            rated=rated,
            voices=voices,
            draft=draft,
            source=source,
            prompt_version=version,
            from_recording=False,
            row=None,
        )
    row = HireDebrief(
        id=uuid.uuid4(),
        org_id=record.app.org_id,
        application_id=record.app.id,
        job_id=record.job.id,
        stage_key=stage_key,
        scorecards_hash=digest,
        scorecard_ids=[r.id for r in rows],
        content={locale: draft.model_dump(mode="json")},
        source=source,
        prompt_version=version,
        model_call_id=call_id,
        from_recording=recorded,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
            await append_audit(
                session,
                action="hire.debrief.generated",
                actor_type=ActorType.USER if actor_id else ActorType.SYSTEM,
                actor_id=str(actor_id) if actor_id else "pairly.hire.debrief",
                entity_type=ENTITY,
                entity_id=record.app.id,
                workflow=WORKFLOW,
                payload={
                    "debrief_id": str(row.id),
                    "stage_key": stage_key,
                    "scorecards": [str(r.id) for r in rows],
                    "prompt_version": version,
                    "model_call_id": str(call_id) if call_id else None,
                    "from_recording": recorded,
                },
            )
    except IntegrityError:
        row = await session.scalar(  # type: ignore[assignment]
            select(HireDebrief).where(
                HireDebrief.application_id == record.app.id,
                HireDebrief.stage_key == stage_key,
                HireDebrief.scorecards_hash == digest,
            )
        )
    return replace(
        empty,
        rated=rated,
        voices=voices,
        draft=draft,
        source=source,
        prompt_version=version,
        from_recording=recorded,
        row=row,
    )


async def decide(
    session: AsyncSession,
    application_id: uuid.UUID,
    stage_key: str,
    *,
    decision: str,
    reason: str,
    actor_id: uuid.UUID,
    router: ModelRouter | None,
) -> Debrief:
    """Record the person's decision on a ready debrief, with a reason, as a decision
    record. Once per debrief."""
    if decision not in DECISIONS:
        raise BadRequestError("decide advance, hold or reject")
    text = reason.strip()
    if len(text) < 3:
        raise BadRequestError("give a short reason for the decision")
    debrief = await build_debrief(
        session, application_id, stage_key, "en", router=router, actor_id=actor_id
    )
    if not debrief.ready:
        raise ConflictError("wait for every scorecard before deciding")
    row = debrief.row
    if row is None:
        # The summary came from the numbers only: keep it so the decision has a row.
        rows = await stage_scorecards(session, application_id, stage_key)
        assert debrief.draft is not None  # noqa: S101 (ready)
        row = HireDebrief(
            id=uuid.uuid4(),
            org_id=debrief.record.app.org_id,
            application_id=application_id,
            job_id=debrief.record.job.id,
            stage_key=stage_key,
            scorecards_hash=_hash(rows),
            scorecard_ids=[r.id for r in rows],
            content={"en": debrief.draft.model_dump(mode="json")},
            source="template",
            prompt_version=TEMPLATE_VERSION,
        )
        session.add(row)
        await session.flush()
    locked = await session.scalar(
        select(HireDebrief).where(HireDebrief.id == row.id).with_for_update()
    )
    if locked is None:
        raise NotFoundError("debrief not found")
    if locked.decision is not None:
        raise ConflictError("a decision is already recorded for this debrief")
    now = datetime.now(UTC)
    stored = await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.APPROVAL,
            workflow=WORKFLOW,
            entity_type=ENTITY,
            entity_id=str(application_id),
            actor_type=ActorType.USER,
            actor_id=str(actor_id),
            inputs={
                "debrief_id": str(locked.id),
                "stage_key": stage_key,
                "scorecard_ids": [str(s) for s in locked.scorecard_ids],
            },
            model_call_ids=[locked.model_call_id] if locked.model_call_id else [],
            output={"decision": decision, "stage_key": stage_key},
            evidence={
                "criteria": [
                    {"id": r.criterion_id, "ratings": r.values, "mean": r.mean, "spread": r.spread}
                    for r in debrief.rated
                ],
                "recommendations": [v.recommendation for v in debrief.voices],
            },
            reason=text,
            decided_at=now,
            supersedes=None,
        ),
    )
    locked.decision = decision
    locked.decision_reason = text
    locked.decided_by = actor_id
    locked.decided_at = now
    locked.decision_record_id = stored.id
    await session.flush()
    await append_audit(
        session,
        action="hire.debrief.decided",
        actor_type=ActorType.USER,
        actor_id=str(actor_id),
        entity_type=ENTITY,
        entity_id=application_id,
        workflow=WORKFLOW,
        payload={
            "debrief_id": str(locked.id),
            "stage_key": stage_key,
            "decision": decision,
            "decision_record_id": stored.id,
        },
    )
    return await build_debrief(
        session, application_id, stage_key, "en", router=None, actor_id=actor_id
    )
