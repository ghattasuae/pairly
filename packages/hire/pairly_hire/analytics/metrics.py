# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Hiring analytics as pure functions over ``facts.Facts`` (brief 3.10).

Every metric is a ``Metric``: a value, its unit, the sample size ``n`` it was computed
from, a few plain details (numerator, denominator, median and mean) and the records it
counts (``RecordLink``: an application, an approval or an interview), so every number
on the page links to the rows behind it. A metric with no data has ``value`` None, never
a made-up zero. Methods, stated plainly:

* time to first response: hours from submission to the first message a person sent
  the candidate (queued), median and mean; applications still without one are counted
  as ``pending``.
* time in stage: days spent in each stage, from entering it (submission for
  ``applied``) to the next effective move; stays still open are not counted.
* time to hire: days from submission to the first move into ``hired``.
* stage conversion: the share of applications that reached a stage that also reached
  the next active stage (reaching a stage means reaching it or any later one in the
  job's board order; hired counts as past every active stage).
* source effectiveness: per application source, how many reached the shortlist (or
  later) and how many were hired.
* SLA breaches: active applications waiting at least the organisation's anti-ghosting
  SLA days since their last update (or submission), now; and Hire approval steps past
  their due time (decided late, or still open after it).
* ghosting rate: active applications without any update for ``ghost_days`` or more,
  over all active applications.
* interviewer load: interviews per person on the panel (cancelled ones left out), with
  how many are still to come.
* scheduling time saved (an estimate): interviews the candidate booked through the
  self-scheduling link, times an assumed number of minutes of back and forth each
  booking saves (``minutes_per_booking``, configurable). Labelled as an estimate.
* AI and human agreement: among screenings a person decided (shortlisted or not), the
  share where the AI's overall score was on the same side of ``agreement_threshold``
  (score at or above it reads as "shortlist").
* override rate: screened applications with at least one human override of the AI's
  scores, over screened applications.
* flags caught: screenings whose guardrails flagged injection or hidden content, and
  claim consistency and AI-generated signals (information only).
* cost per screened application: the model cost of the screenings' calls over the
  screened applications. In recorded mode the calls come from recordings and cost 0;
  the metric says so (``recorded_calls``).
"""

from __future__ import annotations

import itertools
import statistics
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from pairly_hire.analytics.facts import AppFact, ApprovalStepFact, Facts, InterviewFact
from pairly_hire.pipeline.stages import DEFAULT_STAGES, HIRED, SHORTLIST, StageSet

RecordKind = Literal["application", "approval", "interview"]

CLAIM_CODES: Final = frozenset(
    {
        "end_before_start",
        "future_date",
        "overlapping_roles",
        "unexplained_gap",
        "title_inflation",
        "unsupported_skills",
    }
)
AI_SIGNAL_CODES: Final = frozenset({"boilerplate", "mass_application"})
INJECTION_REASON: Final = "guardrails_flagged"
DECIDED: Final = {"shortlisted": True, "not_shortlisted": False}
TERMINAL_STATUSES: Final = frozenset({"rejected", "withdrawn"})
# The page lists at most this many records per number (the total is always given).
MAX_RECORDS: Final = 500
HOUR: Final = 3600.0
DAY: Final = 86400.0


@dataclass(frozen=True, slots=True)
class Settings:
    sla_days: int = 7
    ghost_days: int = 7
    agreement_threshold: float = 60.0
    minutes_per_booking: int = 30


@dataclass(frozen=True, slots=True)
class RecordLink:
    kind: RecordKind
    id: str
    job_id: str | None
    label: str


@dataclass(slots=True)
class Metric:
    key: str
    value: float | None
    unit: str
    n: int
    records: list[RecordLink] = field(default_factory=list)
    records_total: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def app_link(app: AppFact) -> RecordLink:
    return RecordLink("application", str(app.id), str(app.job_id), app.reference)


def _metric(
    key: str,
    value: float | None,
    unit: str,
    n: int,
    records: Sequence[RecordLink],
    **detail: Any,
) -> Metric:
    return Metric(
        key=key,
        value=value,
        unit=unit,
        n=n,
        records=list(records[:MAX_RECORDS]),
        records_total=len(records),
        detail=detail,
    )


def _summary(values: Sequence[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return round(statistics.median(values), 2), round(statistics.fmean(values), 2)


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def stage_set(facts: Facts, app: AppFact) -> StageSet:
    return facts.stages.get(app.job_id, DEFAULT_STAGES)


def is_active(app: AppFact, stages: StageSet) -> bool:
    stage = stages.get(app.stage)
    terminal = stage.terminal if stage is not None else app.stage in {HIRED, *TERMINAL_STATUSES}
    return not terminal and app.status not in TERMINAL_STATUSES


def furthest(app: AppFact, stages: StageSet) -> int:
    """Index (in ``active_keys(stages) + [hired]``) of the furthest stage reached."""
    order = [*active_keys(stages), HIRED]
    best = 0
    for key in (app.stage, *(m.to_stage for m in app.moves)):
        if key in order:
            best = max(best, order.index(key))
    return best


def active_keys(stages: StageSet) -> list[str]:
    return [s.key for s in stages.stages if not s.terminal]


def reached(app: AppFact, stages: StageSet, key: str) -> bool:
    order = [*active_keys(stages), HIRED]
    return key in order and furthest(app, stages) >= order.index(key)


# ------------------------------------------------------------------ time


def time_to_first_response(apps: Sequence[AppFact]) -> Metric:
    hours: list[float] = []
    measured: list[RecordLink] = []
    pending = 0
    for a in apps:
        if a.first_response_at is None:
            pending += 1
            continue
        hours.append(max(0.0, (a.first_response_at - a.submitted_at).total_seconds() / HOUR))
        measured.append(app_link(a))
    median, mean = _summary(hours)
    return _metric(
        "time_to_first_response", median, "hours", len(hours), measured, mean=mean, pending=pending
    )


def stays(app: AppFact) -> list[tuple[str, float]]:
    """(stage, days) for every closed stay, in order."""
    out: list[tuple[str, float]] = []
    current, since = "applied", app.submitted_at
    for move in app.moves:
        if move.from_stage == current:
            out.append((current, max(0.0, (move.at - since).total_seconds() / DAY)))
        current, since = move.to_stage, move.at
    return out


def time_in_stage(facts: Facts) -> list[Metric]:
    days: dict[str, list[float]] = defaultdict(list)
    links: dict[str, list[RecordLink]] = defaultdict(list)
    for a in facts.apps:
        for stage, d in stays(a):
            days[stage].append(d)
            links[stage].append(app_link(a))
    order = _display_order(facts)
    out: list[Metric] = []
    for key in order:
        if key not in days:
            continue
        median, mean = _summary(days[key])
        out.append(
            _metric(
                f"time_in_stage.{key}",
                median,
                "days",
                len(days[key]),
                links[key],
                stage=key,
                mean=mean,
            )
        )
    return out


def time_to_hire(apps: Sequence[AppFact]) -> Metric:
    days: list[float] = []
    links: list[RecordLink] = []
    for a in apps:
        hired = next((m.at for m in a.moves if m.to_stage == HIRED), None)
        if hired is None:
            continue
        days.append(max(0.0, (hired - a.submitted_at).total_seconds() / DAY))
        links.append(app_link(a))
    median, mean = _summary(days)
    return _metric("time_to_hire", median, "days", len(days), links, mean=mean)


# ------------------------------------------------------------------ funnel


def _display_order(facts: Facts) -> list[str]:
    """Active stage keys in board order (the default order first, then custom ones)."""
    order = active_keys(DEFAULT_STAGES)
    for stages in facts.stages.values():
        for key in active_keys(stages):
            if key not in order:
                order.append(key)
    return order


def stage_conversion(facts: Facts) -> list[Metric]:
    order = [*_display_order(facts), HIRED]
    reached_by: dict[str, list[AppFact]] = {key: [] for key in order}
    for a in facts.apps:
        stages = stage_set(facts, a)
        for key in order:
            if reached(a, stages, key):
                reached_by[key].append(a)
    out: list[Metric] = []
    for here, there in itertools.pairwise(order):
        base = reached_by[here]
        next_ids = {a.id for a in reached_by[there]}
        moved_on = [a for a in base if a.id in next_ids]
        out.append(
            _metric(
                f"stage_conversion.{here}",
                _rate(len(moved_on), len(base)),
                "rate",
                len(base),
                [app_link(a) for a in moved_on],
                stage=here,
                next_stage=there,
                reached=len(base),
                moved_on=len(moved_on),
            )
        )
    return out


def source_effectiveness(facts: Facts) -> list[Metric]:
    by_source: dict[str, list[AppFact]] = defaultdict(list)
    for a in facts.apps:
        by_source[a.source].append(a)
    out: list[Metric] = []
    for source in sorted(by_source):
        apps = by_source[source]
        shortlisted = [a for a in apps if reached(a, stage_set(facts, a), SHORTLIST)]
        hired = [a for a in apps if reached(a, stage_set(facts, a), HIRED)]
        out.append(
            _metric(
                f"source.{source}",
                _rate(len(hired), len(apps)),
                "rate",
                len(apps),
                [app_link(a) for a in apps],
                source=source,
                applications=len(apps),
                shortlisted=len(shortlisted),
                hired=len(hired),
                shortlist_rate=_rate(len(shortlisted), len(apps)),
            )
        )
    return out


# ------------------------------------------------------------------ waiting


def waiting_days(app: AppFact, now: datetime) -> float:
    since = max(app.submitted_at, app.last_update_at or app.submitted_at)
    return max(0.0, (now - since).total_seconds() / DAY)


def _active(facts: Facts) -> list[AppFact]:
    return [a for a in facts.apps if is_active(a, stage_set(facts, a))]


def sla_breaches(facts: Facts, now: datetime, settings: Settings) -> list[Metric]:
    active = _active(facts)
    late = [a for a in active if waiting_days(a, now) >= settings.sla_days]
    steps = [s for s in facts.steps if step_breached(s, now)]
    approvals = sorted({(s.approval_id, s.approval_type) for s in steps}, key=lambda x: str(x[0]))
    return [
        _metric(
            "sla.anti_ghosting",
            float(len(late)),
            "count",
            len(active),
            [app_link(a) for a in late],
            sla_days=settings.sla_days,
        ),
        _metric(
            "sla.approvals",
            float(len(steps)),
            "count",
            len([s for s in facts.steps if s.due_at is not None]),
            [RecordLink("approval", str(a), None, t) for a, t in approvals],
        ),
    ]


def step_breached(step: ApprovalStepFact, now: datetime) -> bool:
    if step.due_at is None:
        return False
    if step.completed_at is not None:
        return step.completed_at > step.due_at
    return step.status == "active" and now > step.due_at


def ghosting_rate(facts: Facts, now: datetime, settings: Settings) -> Metric:
    active = _active(facts)
    ghosted = [a for a in active if waiting_days(a, now) >= settings.ghost_days]
    return _metric(
        "ghosting_rate",
        _rate(len(ghosted), len(active)),
        "rate",
        len(active),
        [app_link(a) for a in ghosted],
        ghost_days=settings.ghost_days,
        ghosted=len(ghosted),
    )


# ------------------------------------------------------------------ interviews


def _interview_link(i: InterviewFact, apps: dict[uuid.UUID, AppFact]) -> RecordLink:
    app = apps.get(i.application_id)
    label = app.reference if app is not None else str(i.application_id)
    return RecordLink("application", str(i.application_id), str(i.job_id), label)


def interviewer_load(facts: Facts, now: datetime) -> list[Metric]:
    apps = {a.id: a for a in facts.apps}
    per: dict[uuid.UUID, list[InterviewFact]] = defaultdict(list)
    for i in facts.interviews:
        if i.status == "cancelled":
            continue
        for person in i.panel:
            per[person].append(i)
    out: list[Metric] = []
    for person, items in sorted(per.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
        upcoming = [i for i in items if i.starts_at is not None and i.starts_at >= now]
        out.append(
            _metric(
                f"interviewer_load.{person}",
                float(len(items)),
                "count",
                len(items),
                [_interview_link(i, apps) for i in items],
                user_id=str(person),
                name=facts.people.get(person, ""),
                upcoming=len(upcoming),
            )
        )
    return out


def scheduling_time_saved(facts: Facts, settings: Settings) -> Metric:
    apps = {a.id: a for a in facts.apps}
    booked = [i for i in facts.interviews if i.self_scheduled]
    return _metric(
        "scheduling_time_saved",
        float(len(booked) * settings.minutes_per_booking),
        "minutes",
        len(booked),
        [_interview_link(i, apps) for i in booked],
        estimate=True,
        minutes_per_booking=settings.minutes_per_booking,
        self_scheduled_bookings=len(booked),
    )


# ------------------------------------------------------------------ AI and people


def ai_human_agreement(apps: Sequence[AppFact], settings: Settings) -> list[Metric]:
    decided = [
        a
        for a in apps
        if a.screening is not None
        and a.screening.overall is not None
        and a.screening.status in DECIDED
    ]
    agree: list[AppFact] = []
    disagree: list[AppFact] = []
    for a in decided:
        screening = a.screening
        if screening is None or screening.overall is None:
            continue
        ai_yes = screening.overall >= settings.agreement_threshold
        (agree if ai_yes == DECIDED[screening.status] else disagree).append(a)
    return [
        _metric(
            "ai_human_agreement",
            _rate(len(agree), len(decided)),
            "rate",
            len(decided),
            [app_link(a) for a in decided],
            threshold=settings.agreement_threshold,
            agreed=len(agree),
            disagreed=len(disagree),
        ),
        _metric(
            "ai_human_disagreement",
            float(len(disagree)),
            "count",
            len(decided),
            [app_link(a) for a in disagree],
            threshold=settings.agreement_threshold,
        ),
    ]


def screened(apps: Iterable[AppFact]) -> list[AppFact]:
    return [a for a in apps if a.screening is not None and a.screening.overall is not None]


def override_rate(apps: Sequence[AppFact]) -> Metric:
    done = screened(apps)
    overridden = [a for a in done if a.overrides > 0]
    return _metric(
        "override_rate",
        _rate(len(overridden), len(done)),
        "rate",
        len(done),
        [app_link(a) for a in overridden],
        overridden=len(overridden),
        overrides=sum(a.overrides for a in overridden),
    )


def flags(apps: Sequence[AppFact]) -> list[Metric]:
    with_screening = [a for a in apps if a.screening is not None]
    injection = [
        a
        for a in with_screening
        if a.screening is not None
        and any(r.startswith(INJECTION_REASON) for r in a.screening.review_reasons)
    ]
    claims = [
        a
        for a in with_screening
        if a.screening is not None and set(a.screening.flag_codes) & CLAIM_CODES
    ]
    signals = [
        a
        for a in with_screening
        if a.screening is not None and set(a.screening.flag_codes) & AI_SIGNAL_CODES
    ]
    n = len(with_screening)

    def counted(key: str, found: list[AppFact]) -> Metric:
        links = [app_link(a) for a in found]
        return _metric(key, float(len(found)), "count", n, links, rate=_rate(len(found), n))

    return [
        counted("flags.injection", injection),
        counted("flags.claims", claims),
        counted("flags.ai_signals", signals),
    ]


def cost_per_screened(apps: Sequence[AppFact]) -> Metric:
    done = screened(apps)
    total = sum(a.cost_micros for a in done)
    calls = sum(a.model_calls for a in done)
    recorded = sum(a.recorded_calls for a in done)
    value = round(total / len(done) / 1_000_000, 6) if done else None
    return _metric(
        "cost_per_screened_application",
        value,
        "usd",
        len(done),
        [app_link(a) for a in done if a.model_calls],
        total_usd=round(total / 1_000_000, 6),
        model_calls=calls,
        recorded_calls=recorded,
        recorded_mode=bool(calls) and recorded == calls,
    )


@dataclass(slots=True)
class Analytics:
    applications: int
    settings: Settings
    time_to_first_response: Metric
    time_in_stage: list[Metric]
    time_to_hire: Metric
    stage_conversion: list[Metric]
    sources: list[Metric]
    sla: list[Metric]
    ghosting_rate: Metric
    interviewer_load: list[Metric]
    scheduling_time_saved: Metric
    agreement: list[Metric]
    override_rate: Metric
    flags: list[Metric]
    cost_per_screened_application: Metric
    jobs: dict[str, str]


def compute(facts: Facts, *, now: datetime, settings: Settings) -> Analytics:
    apps = list(facts.apps)
    return Analytics(
        applications=len(apps),
        settings=settings,
        time_to_first_response=time_to_first_response(apps),
        time_in_stage=time_in_stage(facts),
        time_to_hire=time_to_hire(apps),
        stage_conversion=stage_conversion(facts),
        sources=source_effectiveness(facts),
        sla=sla_breaches(facts, now, settings),
        ghosting_rate=ghosting_rate(facts, now, settings),
        interviewer_load=interviewer_load(facts, now),
        scheduling_time_saved=scheduling_time_saved(facts, settings),
        agreement=ai_human_agreement(apps, settings),
        override_rate=override_rate(apps),
        flags=flags(apps),
        cost_per_screened_application=cost_per_screened(apps),
        jobs={str(k): v for k, v in facts.jobs.items()},
    )
