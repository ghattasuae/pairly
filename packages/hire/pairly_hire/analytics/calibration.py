# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Calibration mode (brief 3.3.11): people label a sample, the system reports agreement.

* ``sample``: screened applications of a job (newest first) a person has not labelled
  yet. The person scores each criterion 0 to 4 against the rubric's anchors without
  seeing the AI's scores (``save_labels``; labelling again replaces their earlier label).
* Overrides feed it too: a recorded human override (``human_override`` decision) holds
  the person's scores for every criterion, paired with the AI's scores it overrode.
* ``report``: per criterion, the number of pairs, exact agreement, agreement within one
  point, Cohen's kappa where there are at least ``MIN_KAPPA`` pairs (else a plain note),
  the average difference (people minus AI) and the most common disagreement. A
  criterion is low agreement when within-one agreement is under 0.8, exact agreement is
  under 0.5, or kappa is under 0.4 (with at least ``MIN_PAIRS`` pairs).
* Suggestions: for a low-agreement criterion, a deterministic text built from the
  rubric's own anchors (``deterministic_suggestion``) and, on request, a model
  suggestion (prompt ``hire_rubric_clarify/v1``; recorded answers for the demo jobs). A
  suggestion never changes the rubric: a person edits and locks a new version.

Self-identification is never read here.
"""

from __future__ import annotations

import statistics
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.decisions.models import DecisionRecord
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.llm.types import CallContext, TaskSpec
from pairly_core.notifications.templating import has_em_dash
from pairly_core.policy_versions import PolicyError
from pairly_hire.analytics.models import HireCalibrationLabel
from pairly_hire.analytics.stats import cohen_kappa
from pairly_hire.models import HireApplication, HireJob
from pairly_hire.rubric import DEFAULT_ANCHORS, Criterion, Rubric
from pairly_hire.screening.models import HireScreening
from pairly_hire.screening.service import rubric_ref

if TYPE_CHECKING:
    from pairly_core.llm.router import ModelRouter

TASK: Final = TaskSpec(name="hire_rubric_clarify", prompt_version="v1")
ENTITY: Final = "hire.calibration"
OVERRIDE_KIND: Final = "human_override"
APPLICATION_ENTITY: Final = "hire.application"
SCORED: Final = ("awaiting_review", "shortlisted", "not_shortlisted", "returned")
MIN_KAPPA: Final = 10
MIN_PAIRS: Final = 5
LOW_WITHIN_ONE: Final = 0.8
LOW_EXACT: Final = 0.5
LOW_KAPPA: Final = 0.4
MAX_SAMPLE: Final = 50
MAX_SUGGESTION: Final = 600

Source = Literal["label", "override"]
Direction = Literal["people_higher", "people_lower", "mixed"]


@dataclass(frozen=True, slots=True)
class Pair:
    criterion_id: str
    ai: int
    human: int
    source: Source


@dataclass(slots=True)
class CriterionAgreement:
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
    suggestion: str | None = None


@dataclass(slots=True)
class CalibrationReport:
    job_id: str
    rubric_version: int | None
    labels: int
    overrides: int
    criteria: list[CriterionAgreement] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def direction_of(pairs: Sequence[Pair]) -> Direction | None:
    diffs = [p.human - p.ai for p in pairs]
    if not diffs or all(d == 0 for d in diffs):
        return None
    mean = statistics.fmean(diffs)
    if mean >= 0.5:
        return "people_higher"
    if mean <= -0.5:
        return "people_lower"
    return "mixed"


def agreement(criterion_id: str, name: str, pairs: Sequence[Pair]) -> CriterionAgreement:
    """The statistics of one criterion's (AI, person) pairs."""
    n = len(pairs)
    sources: dict[str, int] = dict(Counter(str(p.source) for p in pairs))
    if n == 0:
        return CriterionAgreement(
            criterion_id,
            name,
            0,
            None,
            None,
            None,
            "no pairs yet",
            None,
            sources,
            None,
            False,
            None,
        )
    exact = sum(1 for p in pairs if p.ai == p.human) / n
    within = sum(1 for p in pairs if abs(p.ai - p.human) <= 1) / n
    kappa: float | None = None
    note: str | None = None
    if n >= MIN_KAPPA:
        kappa = cohen_kappa([p.ai for p in pairs], [p.human for p in pairs])
        if kappa is None:
            note = "undefined: every score is the same"
    else:
        note = f"needs at least {MIN_KAPPA} pairs"
    misses = Counter((p.ai, p.human) for p in pairs if p.ai != p.human)
    top = misses.most_common(1)[0] if misses else None
    low = n >= MIN_PAIRS and (
        within < LOW_WITHIN_ONE or exact < LOW_EXACT or (kappa is not None and kappa < LOW_KAPPA)
    )
    return CriterionAgreement(
        criterion_id=criterion_id,
        name=name,
        n=n,
        exact=_round(exact),
        within_one=_round(within),
        kappa=_round(kappa),
        kappa_note=note,
        mean_difference=_round(statistics.fmean(p.human - p.ai for p in pairs), 2),
        sources=sources,
        top_disagreement={"ai": top[0][0], "people": top[0][1], "count": top[1]} if top else None,
        low_agreement=low,
        direction=direction_of(pairs),
    )


def deterministic_suggestion(criterion: Criterion, stats: CriterionAgreement) -> str:
    """Plain text from the rubric's own anchors: where people and the AI part ways."""
    anchors = criterion.scale_anchors
    parts: list[str] = []
    if stats.top_disagreement is not None:
        ai, people = stats.top_disagreement["ai"], stats.top_disagreement["people"]
        parts.append(
            f"Most disagreements on {criterion.name} are the AI giving {ai} where people "
            f'give {people}. Make the line between {ai} ("{anchors[ai]}") and {people} '
            f'("{anchors[people]}") concrete for this job: name the evidence that moves '
            f"an application from one to the other."
        )
    if stats.direction == "people_higher":
        parts.append(
            "People score it higher than the AI on average: check whether the evidence "
            "description leaves out kinds of evidence people accept."
        )
    elif stats.direction == "people_lower":
        parts.append(
            "People score it lower than the AI on average: check whether the anchors credit "
            "a mention that people do not count as evidence."
        )
    if criterion.anchors == () or tuple(criterion.anchors) == DEFAULT_ANCHORS:
        parts.append(
            "This criterion uses the general anchors. Writing anchors for this job usually "
            "helps people and the AI score it the same way."
        )
    return " ".join(parts)


# ------------------------------------------------------------------ data


async def scored_screening(session: AsyncSession, application_id: uuid.UUID) -> HireScreening:
    row = await session.scalar(
        select(HireScreening)
        .where(HireScreening.application_id == application_id)
        .order_by(HireScreening.created_at.desc())
        .limit(1)
    )
    if row is None or row.status not in SCORED or row.score_decision_id is None:
        raise NotFoundError("this application has no scored screening")
    return row


async def job_rubric(
    session: AsyncSession, job_id: uuid.UUID
) -> tuple[HireJob, Rubric | None, int | None]:
    job = await session.scalar(select(HireJob).where(HireJob.id == job_id))
    if job is None:
        raise NotFoundError("job not found")
    if job.rubric_policy_id is None:
        return job, None, None
    try:
        ref, rubric = await rubric_ref(session, job.rubric_policy_id)
    except PolicyError:
        return job, None, None
    return job, rubric, ref.version


@dataclass(frozen=True, slots=True)
class SampleItem:
    application_id: uuid.UUID
    screening_id: uuid.UUID
    reference: str
    labelled: bool


async def sample(
    session: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID, size: int
) -> list[SampleItem]:
    """Newest scored applications of the job; the ones this person labelled come last."""
    size = max(1, min(size, MAX_SAMPLE))
    rows = (
        await session.execute(
            select(HireScreening.id, HireScreening.application_id, HireApplication.reference)
            .join(HireApplication, HireApplication.id == HireScreening.application_id)
            .where(
                HireScreening.job_id == job_id,
                HireScreening.status.in_(SCORED),
                HireScreening.score_decision_id.is_not(None),
            )
            .order_by(HireScreening.created_at.desc())
            .limit(size * 3)
        )
    ).all()
    mine = set(
        await session.scalars(
            select(HireCalibrationLabel.screening_id).where(
                HireCalibrationLabel.job_id == job_id, HireCalibrationLabel.labelled_by == user_id
            )
        )
    )
    seen: set[uuid.UUID] = set()
    items: list[SampleItem] = []
    for screening_id, application_id, reference in rows:
        if application_id in seen:
            continue  # the newest screening of each application only
        seen.add(application_id)
        items.append(SampleItem(application_id, screening_id, reference, screening_id in mine))
    items.sort(key=lambda i: i.labelled)
    return items[:size]


async def my_labels(
    session: AsyncSession, screening_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, int]:
    rows = await session.scalars(
        select(HireCalibrationLabel).where(
            HireCalibrationLabel.screening_id == screening_id,
            HireCalibrationLabel.labelled_by == user_id,
        )
    )
    return {r.criterion_id: int(r.score) for r in rows}


async def save_labels(
    session: AsyncSession,
    user_id: uuid.UUID,
    application_id: uuid.UUID,
    scores: Mapping[str, int],
) -> dict[str, int]:
    """Store this person's scores for the application's newest scored screening."""
    row = await scored_screening(session, application_id)
    _, rubric = await rubric_ref(session, row.rubric_policy_id)
    known = {c.id for c in rubric.criteria}
    if not scores:
        raise BadRequestError("score at least one criterion")
    for criterion_id, score in scores.items():
        if criterion_id not in known:
            raise BadRequestError(f"{criterion_id}: not a criterion of this rubric")
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 4:
            raise BadRequestError(f"{criterion_id}: a score from 0 to 4")
    existing = {
        r.criterion_id: r
        for r in await session.scalars(
            select(HireCalibrationLabel)
            .where(
                HireCalibrationLabel.screening_id == row.id,
                HireCalibrationLabel.labelled_by == user_id,
            )
            .with_for_update()
        )
    }
    now = datetime.now(UTC)
    for criterion_id, score in scores.items():
        label = existing.get(criterion_id)
        if label is None:
            session.add(
                HireCalibrationLabel(
                    id=uuid.uuid4(),
                    org_id=row.org_id,
                    application_id=row.application_id,
                    screening_id=row.id,
                    job_id=row.job_id,
                    rubric_policy_id=row.rubric_policy_id,
                    criterion_id=criterion_id,
                    score=score,
                    labelled_by=user_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            label.score = score
            label.updated_at = now
    await session.flush()
    await append_audit(
        session,
        action="hire.calibration.labelled",
        actor_type=ActorType.USER,
        actor_id=str(user_id),
        entity_type=ENTITY,
        entity_id=row.id,
        workflow="hire",
        payload={
            "application_id": str(row.application_id),
            "job_id": str(row.job_id),
            "criteria": sorted(scores),
        },
    )
    return await my_labels(session, row.id, user_id)


def _scores(output: Mapping[str, Any] | None) -> dict[str, int]:
    raw = (output or {}).get("scores")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(k): int(v) for k, v in raw.items() if isinstance(v, int) and not isinstance(v, bool)
    }


async def collect_pairs(session: AsyncSession, job_id: uuid.UUID) -> tuple[list[Pair], int, int]:
    """(pairs, labels, overrides) for the job: people's labels and recorded overrides,
    each against the AI's scores of the same screening."""
    screenings = list(
        await session.scalars(select(HireScreening).where(HireScreening.job_id == job_id))
    )
    by_id = {s.id: s for s in screenings}
    decision_ids = {s.score_decision_id for s in screenings if s.score_decision_id}
    app_ids = [str(s.application_id) for s in screenings]
    overrides = (
        list(
            await session.scalars(
                select(DecisionRecord).where(
                    DecisionRecord.entity_type == APPLICATION_ENTITY,
                    DecisionRecord.kind == OVERRIDE_KIND,
                    DecisionRecord.entity_id.in_(app_ids),
                )
            )
        )
        if app_ids
        else []
    )
    referenced = decision_ids | {
        str(o.inputs.get("override_of")) for o in overrides if o.inputs.get("override_of")
    }
    ai_scores: dict[str, dict[str, int]] = {}
    if referenced:
        for record in await session.scalars(
            select(DecisionRecord).where(DecisionRecord.id.in_(list(referenced)))
        ):
            ai_scores[record.id] = _scores(record.output)
    pairs: list[Pair] = []
    labels = list(
        await session.scalars(
            select(HireCalibrationLabel).where(HireCalibrationLabel.job_id == job_id)
        )
    )
    for label in labels:
        screening = by_id.get(label.screening_id)
        if screening is None or screening.score_decision_id is None:
            continue
        ai = ai_scores.get(screening.score_decision_id, {}).get(label.criterion_id)
        if ai is not None:
            pairs.append(Pair(label.criterion_id, ai, int(label.score), "label"))
    for record in overrides:
        base = ai_scores.get(str(record.inputs.get("override_of")), {})
        for criterion_id, human in _scores(record.output).items():
            if criterion_id in base:
                pairs.append(Pair(criterion_id, base[criterion_id], human, "override"))
    return pairs, len(labels), len(overrides)


async def report(session: AsyncSession, job_id: uuid.UUID) -> CalibrationReport:
    _, rubric, version = await job_rubric(session, job_id)
    pairs, labels, overrides = await collect_pairs(session, job_id)
    grouped: dict[str, list[Pair]] = defaultdict(list)
    for p in pairs:
        grouped[p.criterion_id].append(p)
    criteria = list(rubric.criteria) if rubric is not None else []
    out = CalibrationReport(str(job_id), version, labels, overrides)
    seen: set[str] = set()
    for criterion in criteria:
        stats = agreement(criterion.id, criterion.name, grouped.get(criterion.id, []))
        if stats.low_agreement:
            stats.suggestion = deterministic_suggestion(criterion, stats)
        out.criteria.append(stats)
        seen.add(criterion.id)
    for criterion_id in sorted(set(grouped) - seen):  # criteria of an older rubric version
        out.criteria.append(agreement(criterion_id, criterion_id, grouped[criterion_id]))
    return out


# ------------------------------------------------------------------ model suggestion

_Suggestion = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_SUGGESTION)]
_PROTECTED: Final = (
    "age", "gender", "male", "female", "nationality", "religion", "married", "pregnan",
    "disab", "health", "race", "ethnic", "photo", "appearance",
)  # fmt: skip


class RubricClarification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestion: _Suggestion


def clarify_inputs(criterion: Criterion, direction: Direction) -> dict[str, Any]:
    """What the model reads: the criterion as written and which way people differ.
    No application, no candidate, no score of anyone."""
    return {
        "criterion": criterion.name,
        "description": criterion.description,
        "evidence": criterion.evidence,
        "anchors": list(criterion.scale_anchors),
        "direction": direction,
        "language": "en",
    }


def check_suggestion(text: str) -> list[str]:
    problems: list[str] = []
    if not text.strip():
        problems.append("empty")
    if has_em_dash(text):
        problems.append("an em dash")
    words = text.casefold().replace(",", " ").replace(".", " ").split()
    for stem in _PROTECTED:
        if any(w == stem or (len(stem) > 4 and w.startswith(stem)) for w in words):
            problems.append(f"mentions a protected characteristic ({stem})")
    return problems


class SuggestionRefusedError(ConflictError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("the suggestion was refused: " + "; ".join(problems))
        self.problems = problems


async def model_suggestion(
    router: ModelRouter,
    org_id: uuid.UUID,
    criterion: Criterion,
    direction: Direction,
    *,
    actor_id: uuid.UUID,
    job_id: uuid.UUID,
) -> tuple[str, bool]:
    """(suggestion, from_recording). Raises ``ModelUnavailable`` (fail closed) or
    ``SuggestionRefusedError``."""
    result = await router.complete(
        task=TASK,
        inputs=clarify_inputs(criterion, direction),
        schema=RubricClarification,
        org_id=org_id,
        context=CallContext(
            actor_id=str(actor_id), workflow="hire", entity_type="hire.job", entity_id=str(job_id)
        ),
    )
    problems = check_suggestion(result.value.suggestion)
    if problems:
        raise SuggestionRefusedError(problems)
    return result.value.suggestion, result.from_recording
