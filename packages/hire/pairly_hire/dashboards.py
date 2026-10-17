# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Role dashboards (brief 5.1 items 1 to 7, 5.2 item 1): what needs each person now, and
how things are going, built only from existing services. Nothing new is measured here.

Every dashboard has the same shape (``Dashboard``): ``action`` (what needs me now,
first), ``sections`` (the rest, as lists of records), ``metrics`` (``analytics.metrics``
results, records trimmed) and ``links`` (pages the role may open). Every number is the
size of a list of records, and every record carries what the web needs to link to it.

Sources, per role:

* recruiter: the analytics facts (``analytics.facts.load_facts``) of the jobs they
  recruit for (``recruiters.job_recruiters``; every job when they have none): screenings
  awaiting review, their flags (``metrics.flags``), the anti-ghosting SLA
  (``metrics.sla_breaches``), time to first response and time in stage; the approval
  inbox (``approvals.service.list_inbox``); the waiting list (``messaging.sla.waiting``);
  interviews today and this week; scorecards still missing after an interview.
* hiring manager: their jobs (recruiter, or opened from a requisition they asked for):
  requisitions and their status, candidates in the shortlist stage, interviews this
  week, debriefs ready (every panel seat submitted, ``scorecards.panel_seats``, no
  decision yet), offers awaiting approval, pipeline counts per role and time to hire.
* interviewer: their own panel seats only: upcoming interviews with join links and
  kits, scorecards due, past interviews and whether they submitted, load this week.
* HR or talent lead: the full ``analytics.metrics.compute`` (funnel, time to hire,
  sources, ghosting, interviewer load, AI and human agreement, overrides, flags), all
  jobs and teams, survey totals when allowed, and links to the bias report.
* employee referrer: ``pipeline.referrals.my_referrals`` (the referrer's privacy-safe
  view) and the open jobs to refer people to.

Candidates use the portal and referees their signed link; the web links those.

Blind safety: candidates appear by application reference, or by the label the pipeline
shows (``AppRecord.label`` with ``blind_applications``), never more. No scorecard
ratings, notes, compensation or approval titles are read here.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.models import ApprovalStatus
from pairly_core.approvals.service import list_inbox
from pairly_core.models import User
from pairly_core.rbac import Actor
from pairly_hire.analytics.facts import AppFact, Facts, load_facts
from pairly_hire.analytics.metrics import (
    Metric,
    Settings,
    compute,
    flags,
    is_active,
    sla_breaches,
    stage_set,
    time_in_stage,
    time_to_first_response,
    time_to_hire,
)
from pairly_hire.interviewing.models import HireDebrief, HireScorecard
from pairly_hire.interviewing.scorecards import panel_seats
from pairly_hire.messaging.sla import sla_days, waiting
from pairly_hire.models import HireApplication, HireCandidate, HireJob, JobStatus
from pairly_hire.offers.models import HireOffer, OfferStatus
from pairly_hire.pipeline.hooks import blind_applications
from pairly_hire.pipeline.models import HirePipelineEntry
from pairly_hire.pipeline.records import AppRecord
from pairly_hire.pipeline.referrals import my_referrals
from pairly_hire.pipeline.stages import HIRED, SHORTLIST, StageSet, stages_for_jobs
from pairly_hire.recruiters import job_recruiters
from pairly_hire.requisitions.models import HireRequisition, RequisitionStatus
from pairly_hire.scheduling.emails import meeting_url_of
from pairly_hire.scheduling.models import HireInterview, HireInterviewPanelist
from pairly_hire.scheduling.rules import org_scheduling

ROLES: Final = ("recruiter", "hiring_manager", "interviewer", "talent", "referrer")
MAX_ITEMS: Final = 10
MAX_RECORDS: Final = 10
# Interviews that take scorecards (``interviewing.reminders.SCOREABLE``) and how far back
# a missing one still counts.
SCOREABLE: Final = ("booked", "rescheduled", "completed")
UPCOMING: Final = ("booked", "rescheduled")
SCORECARD_WINDOW: Final = timedelta(days=14)
PAST_WINDOW: Final = timedelta(days=30)
WORKING_JOBS: Final = (JobStatus.OPEN.value, JobStatus.PAUSED.value)

Detail = dict[str, str | int | float | bool | None]


@dataclass(slots=True)
class StageCount:
    key: str
    label: str
    label_ar: str
    count: int


@dataclass(slots=True)
class Item:
    """One record: ``kind`` tells the web where it lives (``application``,
    ``interview``, ``job``, ``shortlist``, ``debrief``, ``offer``, ``requisition``,
    ``approval``, ``referral``)."""

    kind: str
    id: str
    label: str
    job_id: str | None = None
    application_id: str | None = None
    at: datetime | None = None
    detail: Detail = field(default_factory=dict)
    stages: list[StageCount] = field(default_factory=list)


@dataclass(slots=True)
class Section:
    key: str
    count: int
    items: list[Item] = field(default_factory=list)


@dataclass(slots=True)
class Dashboard:
    role: str
    generated_at: datetime
    timezone: str
    # ``mine``: the jobs the person works on; ``all``: every job (they have none, or the
    # role sees the whole organisation).
    scope: str
    action: list[Section] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


def section(key: str, items: Sequence[Item], *, count: int | None = None) -> Section:
    return Section(key, len(items) if count is None else count, list(items[:MAX_ITEMS]))


def trimmed(metric: Metric) -> Metric:
    """The metric with its first records only (the total stays in ``records_total``)."""
    return replace(metric, records=metric.records[:MAX_RECORDS])


def app_item(app: AppFact, **detail: Any) -> Item:
    return Item(
        "application",
        str(app.id),
        app.reference,
        job_id=str(app.job_id),
        application_id=str(app.id),
        at=app.submitted_at,
        detail={**{"stage": app.stage}, **detail},
    )


def scoped(facts: Facts, job_ids: set[uuid.UUID] | None) -> Facts:
    """``facts`` limited to ``job_ids`` (None: unchanged)."""
    if job_ids is None:
        return facts
    return replace(
        facts,
        apps=tuple(a for a in facts.apps if a.job_id in job_ids),
        interviews=tuple(i for i in facts.interviews if i.job_id in job_ids),
    )


async def _settings(session: AsyncSession) -> Settings:
    days = await sla_days(session)
    return Settings(sla_days=days, ghost_days=days)


async def _window(session: AsyncSession, now: datetime) -> tuple[str, datetime, datetime]:
    """The organisation's time zone, the start of today there and seven days later."""
    org = await org_scheduling(session)
    try:
        zone = ZoneInfo(org.timezone)
    except (KeyError, ValueError):
        zone = ZoneInfo("UTC")
    local = now.astimezone(zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    return org.timezone, start, start + timedelta(days=7)


async def _jobs(session: AsyncSession, statuses: Iterable[str] | None = None) -> list[HireJob]:
    query = select(HireJob).order_by(HireJob.created_at.desc())
    if statuses is not None:
        query = query.where(HireJob.status.in_(list(statuses)))
    return list(await session.scalars(query))


async def _labels(
    session: AsyncSession, application_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """The label the pipeline shows for each application (blind-safe)."""
    ids = list(dict.fromkeys(application_ids))
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(HireApplication, HireCandidate, HireJob, HirePipelineEntry)
            .join(HireCandidate, HireCandidate.id == HireApplication.candidate_id)
            .join(HireJob, HireJob.id == HireApplication.job_id)
            .outerjoin(HirePipelineEntry, HirePipelineEntry.application_id == HireApplication.id)
            .where(HireApplication.id.in_(ids))
        )
    ).all()
    stages = await stages_for_jobs(session, list({r[2].id for r in rows}))
    blind = await blind_applications(session, ids)
    out: dict[uuid.UUID, str] = {}
    for app, candidate, job, entry in rows:
        out[app.id] = AppRecord(app, candidate, job, entry, stages[job.id]).label(app.id in blind)
    return out


def _stage_counts(apps: Sequence[AppFact], stages: StageSet) -> list[StageCount]:
    counts: dict[str, int] = defaultdict(int)
    for a in apps:
        counts[a.stage] += 1
    return [
        StageCount(s.key, s.label, s.label_ar, counts.get(s.key, 0))
        for s in stages.stages
        if not s.terminal or s.key == HIRED
    ]


async def _job_items(session: AsyncSession, facts: Facts, jobs: Sequence[HireJob]) -> list[Item]:
    """Each job with its pipeline counts per stage (active candidates, and hired)."""
    job_stages = await stages_for_jobs(session, [j.id for j in jobs]) if jobs else {}
    by_job: dict[uuid.UUID, list[AppFact]] = defaultdict(list)
    for a in facts.apps:
        stages = stage_set(facts, a)
        if is_active(a, stages) or a.stage == HIRED:
            by_job[a.job_id].append(a)
    out: list[Item] = []
    for job in jobs:
        apps = by_job.get(job.id, [])
        out.append(
            Item(
                "job",
                str(job.id),
                job.title,
                job_id=str(job.id),
                detail={
                    "status": job.status,
                    "team": job.team,
                    "active": sum(1 for a in apps if a.stage != HIRED),
                    "hired": sum(1 for a in apps if a.stage == HIRED),
                },
                stages=_stage_counts(apps, job_stages[job.id]),
            )
        )
    return out


async def _approvals(session: AsyncSession, actor: Actor) -> list[Item]:
    """Hire approvals waiting for this person (the inbox, assigned to me). The approval's
    title is never shown (it may name a candidate): its type and SLA only."""
    page = await list_inbox(
        session,
        actor,
        workflow="hire",
        status=ApprovalStatus.PENDING,
        assigned_to_me=True,
        limit=100,
    )
    return [
        Item(
            "approval",
            str(i.approval.id),
            i.approval.approval_type,
            at=i.approval.created_at,
            detail={"type": i.approval.approval_type, "sla": i.sla},
        )
        for i in page.items
    ]


async def _interviews(
    session: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    job_ids: set[uuid.UUID] | None = None,
    panelist: uuid.UUID | None = None,
    statuses: Sequence[str] = UPCOMING,
) -> list[HireInterview]:
    query = select(HireInterview).where(
        HireInterview.status.in_(list(statuses)),
        HireInterview.starts_at >= start,
        HireInterview.starts_at < end,
    )
    if job_ids is not None:
        query = query.where(HireInterview.job_id.in_(list(job_ids)))
    if panelist is not None:
        query = query.where(
            HireInterview.id.in_(
                select(HireInterviewPanelist.interview_id).where(
                    HireInterviewPanelist.user_id == panelist,
                    HireInterviewPanelist.removed_at.is_(None),
                )
            )
        )
    return list(await session.scalars(query.order_by(HireInterview.starts_at, HireInterview.id)))


def _interview_item(
    interview: HireInterview, labels: dict[uuid.UUID, str], *, join: bool = False, **detail: Any
) -> Item:
    extra: Detail = {
        "stage": interview.stage_key,
        "status": interview.status,
        "ends_at": interview.ends_at.isoformat() if interview.ends_at else None,
        "meeting_provider": interview.meeting_provider,
    }
    if join:
        extra["meeting_url"] = meeting_url_of(interview)
    return Item(
        "interview",
        str(interview.id),
        labels.get(interview.application_id, ""),
        job_id=str(interview.job_id),
        application_id=str(interview.application_id),
        at=interview.starts_at,
        detail={**extra, **detail},
    )


async def _seats(
    session: AsyncSession, interviews: Sequence[HireInterview]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    ids = [i.id for i in interviews]
    if not ids:
        return {}
    out: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for interview_id, user_id in (
        await session.execute(
            select(HireInterviewPanelist.interview_id, HireInterviewPanelist.user_id).where(
                HireInterviewPanelist.interview_id.in_(ids),
                HireInterviewPanelist.removed_at.is_(None),
            )
        )
    ).all():
        out[interview_id].append(user_id)
    return out


async def _submitted(
    session: AsyncSession, interviews: Sequence[HireInterview]
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    ids = [i.id for i in interviews]
    if not ids:
        return set()
    rows = await session.execute(
        select(HireScorecard.interview_id, HireScorecard.interviewer_id).where(
            HireScorecard.interview_id.in_(ids), HireScorecard.status == "submitted"
        )
    )
    return {(r.interview_id, r.interviewer_id) for r in rows.all()}


async def _missing_scorecards(
    session: AsyncSession,
    now: datetime,
    *,
    job_ids: set[uuid.UUID] | None = None,
    panelist: uuid.UUID | None = None,
) -> list[tuple[HireInterview, uuid.UUID]]:
    """(interview, person) for each seat of an interview that ended in the last two weeks
    without a submitted scorecard."""
    query = select(HireInterview).where(
        HireInterview.status.in_(list(SCOREABLE)),
        HireInterview.ends_at <= now,
        HireInterview.ends_at > now - SCORECARD_WINDOW,
    )
    if job_ids is not None:
        query = query.where(HireInterview.job_id.in_(list(job_ids)))
    interviews = list(await session.scalars(query.order_by(HireInterview.ends_at)))
    seats = await _seats(session, interviews)
    done = await _submitted(session, interviews)
    out: list[tuple[HireInterview, uuid.UUID]] = []
    for interview in interviews:
        for person in seats.get(interview.id, []):
            if panelist is not None and person != panelist:
                continue
            if (interview.id, person) not in done:
                out.append((interview, person))
    return out


async def _people(session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    wanted = list(set(ids))
    if not wanted:
        return {}
    rows = await session.execute(
        select(User.id, User.display_name, User.email).where(User.id.in_(wanted))
    )
    return {r.id: str(r.display_name or r.email or "") for r in rows.all()}


# ------------------------------------------------------------------ recruiter


def recruiting_jobs(jobs: Sequence[HireJob], actor: Actor) -> set[uuid.UUID]:
    return {j.id for j in jobs if actor.id is not None and actor.id in job_recruiters(j)}


async def recruiter(session: AsyncSession, actor: Actor, now: datetime) -> Dashboard:
    tz, today, week_end = await _window(session, now)
    settings = await _settings(session)
    jobs = await _jobs(session)
    mine = recruiting_jobs(jobs, actor)
    scope = mine or None
    facts = scoped(await load_facts(session), scope)

    awaiting = [
        a
        for a in facts.apps
        if is_active(a, stage_set(facts, a))
        and a.screening is not None
        and a.screening.status == "awaiting_review"
    ]
    flagged: dict[uuid.UUID, list[str]] = defaultdict(list)
    for metric in flags(awaiting):
        kind = metric.key.split(".", 1)[1]
        for record in metric.records:
            flagged[uuid.UUID(record.id)].append(kind)
    by_id = {a.id: a for a in facts.apps}
    flag_items = [app_item(by_id[i], flags=",".join(k)) for i, k in flagged.items() if i in by_id]

    waits = [
        w for w in await waiting(session, now=now) if scope is None or w.record.job.id in scope
    ]
    blind = await blind_applications(session, [w.record.app.id for w in waits[:MAX_ITEMS]])
    wait_items = [
        Item(
            "application",
            str(w.record.app.id),
            w.record.label(w.record.app.id in blind),
            job_id=str(w.record.job.id),
            application_id=str(w.record.app.id),
            at=w.since,
            detail={
                "stage": w.record.stage_key,
                "waiting_days": w.waiting_days,
                "sla_overdue": w.waiting_days >= settings.sla_days,
                "has_open_draft": w.has_open_draft,
            },
        )
        for w in waits[:MAX_ITEMS]
    ]

    missing = await _missing_scorecards(session, now, job_ids=scope)
    week = await _interviews(session, start=today, end=week_end, job_ids=scope)
    labels = await _labels(
        session,
        [i.application_id for i in week] + [i.application_id for i, _ in missing[:MAX_ITEMS]],
    )
    names = await _people(session, [p for _, p in missing[:MAX_ITEMS]])
    missing_items = [
        _interview_item(i, labels, interviewer=names.get(p, ""), interviewer_id=str(p))
        for i, p in missing
    ]
    today_end = today + timedelta(days=1)
    todays = [i for i in week if i.starts_at is not None and i.starts_at < today_end]

    sla = sla_breaches(facts, now, settings)
    ghosting = next(m for m in sla if m.key == "sla.anti_ghosting")
    late_items = [
        app_item(by_id[uuid.UUID(r.id)]) for r in ghosting.records if uuid.UUID(r.id) in by_id
    ]

    working = [j for j in jobs if j.status in WORKING_JOBS and (scope is None or j.id in scope)]
    return Dashboard(
        role="recruiter",
        generated_at=now,
        timezone=tz,
        scope="mine" if scope else "all",
        action=[
            section(
                "to_review", [app_item(a) for a in sorted(awaiting, key=lambda a: a.submitted_at)]
            ),
            section("flags", flag_items),
            section("approvals", await _approvals(session, actor)),
            section("waiting", wait_items, count=len(waits)),
            section("missing_scorecards", missing_items),
            section("sla", late_items),
        ],
        sections=[
            section("interviews_today", [_interview_item(i, labels) for i in todays]),
            section("interviews_week", [_interview_item(i, labels) for i in week]),
            section("jobs", await _job_items(session, facts, working)),
        ],
        metrics=[
            trimmed(time_to_first_response(list(facts.apps))),
            *map(trimmed, time_in_stage(facts)),
        ],
        links=["analytics", "inbox"],
    )


# ------------------------------------------------------------------ hiring manager


async def hiring_manager(session: AsyncSession, actor: Actor, now: datetime) -> Dashboard:
    tz, today, week_end = await _window(session, now)
    jobs = await _jobs(session)
    requisitions = list(
        await session.scalars(
            select(HireRequisition)
            .where(HireRequisition.requested_by == actor.id)
            .order_by(HireRequisition.created_at.desc())
            .limit(50)
        )
    )
    asked = {r.id for r in requisitions}
    mine = recruiting_jobs(jobs, actor) | {
        j.id for j in jobs if j.requisition_id is not None and j.requisition_id in asked
    }
    scope = mine or None
    facts = scoped(await load_facts(session), scope)
    working = [j for j in jobs if j.status in WORKING_JOBS and (scope is None or j.id in scope)]

    shortlists: list[Item] = []
    by_job: dict[uuid.UUID, int] = defaultdict(int)
    for a in facts.apps:
        if a.stage == SHORTLIST and is_active(a, stage_set(facts, a)):
            by_job[a.job_id] += 1
    for job in working:
        if by_job.get(job.id):
            shortlists.append(
                Item(
                    "shortlist",
                    str(job.id),
                    job.title,
                    job_id=str(job.id),
                    detail={"count": by_job[job.id]},
                )
            )

    ready = await _debriefs_ready(session, facts, now)
    offers = list(
        await session.scalars(
            select(HireOffer)
            .where(HireOffer.status == OfferStatus.PENDING_APPROVAL.value)
            .where(
                HireOffer.job_id.in_(list(scope))
                if scope is not None
                else HireOffer.job_id.is_not(None)
            )
            .order_by(HireOffer.created_at)
        )
    )
    week = await _interviews(session, start=today, end=week_end, job_ids=scope)
    labels = await _labels(
        session, [i.application_id for i in week] + [o.application_id for o in offers[:MAX_ITEMS]]
    )
    offer_items = [
        Item(
            "offer",
            str(o.id),
            labels.get(o.application_id, ""),
            job_id=str(o.job_id),
            application_id=str(o.application_id),
            at=o.created_at,
            detail={"reference": o.reference, "job_title": o.job_title},
        )
        for o in offers
    ]
    open_statuses = {
        RequisitionStatus.PENDING_APPROVAL.value,
        RequisitionStatus.CHANGES_REQUESTED.value,
    }
    requisition_items = [
        Item(
            "requisition",
            str(r.id),
            r.title,
            at=r.created_at,
            detail={"reference": r.reference, "status": r.status, "team": r.team},
        )
        for r in requisitions
        if r.status != RequisitionStatus.CANCELLED.value
    ]
    approvals = await _approvals(session, actor)
    return Dashboard(
        role="hiring_manager",
        generated_at=now,
        timezone=tz,
        scope="mine" if scope else "all",
        action=[
            section("shortlists", shortlists, count=sum(by_job.get(j.id, 0) for j in working)),
            section("debriefs", ready),
            section("offers", offer_items),
            section("approvals", approvals),
            section(
                "requisitions_open",
                [i for i in requisition_items if i.detail.get("status") in open_statuses],
            ),
        ],
        sections=[
            section("interviews_week", [_interview_item(i, labels) for i in week]),
            section("requisitions", requisition_items),
            section("jobs", await _job_items(session, facts, working)),
        ],
        metrics=[trimmed(time_to_hire(list(facts.apps)))],
        links=["requisitions", "inbox"],
    )


async def _debriefs_ready(session: AsyncSession, facts: Facts, now: datetime) -> list[Item]:
    """Applications and stages where every panel seat has submitted its scorecard and
    nobody has decided on the debrief yet (``interviewing.debrief``: ready)."""
    active = {a.id: a for a in facts.apps if is_active(a, stage_set(facts, a))}
    if not active:
        return []
    rows = (
        await session.execute(
            select(HireScorecard.application_id, HireScorecard.stage_key)
            .where(
                HireScorecard.status == "submitted",
                HireScorecard.application_id.in_(list(active)),
                HireScorecard.submitted_at > now - timedelta(days=60),
            )
            .distinct()
        )
    ).all()
    decided = {
        (r.application_id, r.stage_key)
        for r in (
            await session.execute(
                select(HireDebrief.application_id, HireDebrief.stage_key).where(
                    HireDebrief.application_id.in_(list(active)), HireDebrief.decision.is_not(None)
                )
            )
        ).all()
    }
    jobs = {
        j.id: j
        for j in await session.scalars(
            select(HireJob).where(HireJob.id.in_({a.job_id for a in active.values()}))
        )
    }
    out: list[Item] = []
    for application_id, stage_key in rows:
        if (application_id, stage_key) in decided or len(out) >= MAX_ITEMS * 3:
            continue
        app = active[application_id]
        seats = await panel_seats(session, application_id, stage_key, jobs[app.job_id])
        if not seats:
            continue
        submitted = {
            (r.interview_id, r.interviewer_id)
            for r in (
                await session.execute(
                    select(HireScorecard.interview_id, HireScorecard.interviewer_id).where(
                        HireScorecard.application_id == application_id,
                        HireScorecard.stage_key == stage_key,
                        HireScorecard.status == "submitted",
                    )
                )
            ).all()
        }
        if all(seat in submitted for seat in seats):
            out.append(
                Item(
                    "debrief",
                    f"{application_id}:{stage_key}",
                    app.reference,
                    job_id=str(app.job_id),
                    application_id=str(application_id),
                    detail={"stage": stage_key, "scorecards": len(seats)},
                )
            )
    return out


# ------------------------------------------------------------------ interviewer


async def interviewer(session: AsyncSession, actor: Actor, now: datetime) -> Dashboard:
    tz, today, week_end = await _window(session, now)
    assert actor.id is not None  # noqa: S101 (the route requires a person)
    upcoming = await _interviews(
        session, start=now - timedelta(hours=1), end=now + timedelta(days=14), panelist=actor.id
    )
    week = [i for i in upcoming if i.starts_at is not None and today <= i.starts_at < week_end]
    due = await _missing_scorecards(session, now, panelist=actor.id)
    past = await _interviews(
        session, start=now - PAST_WINDOW, end=now, panelist=actor.id, statuses=SCOREABLE
    )
    done = await _submitted(session, past)
    labels = await _labels(
        session, {i.application_id for i in [*upcoming, *past, *(d for d, _ in due)]}
    )
    return Dashboard(
        role="interviewer",
        generated_at=now,
        timezone=tz,
        scope="mine",
        action=[
            section("scorecards_due", [_interview_item(i, labels) for i, _ in due]),
            section(
                "interviews_next",
                [
                    _interview_item(i, labels, join=True)
                    for i in upcoming
                    if i.starts_at and i.starts_at < today + timedelta(days=2)
                ],
            ),
        ],
        sections=[
            section(
                "interviews_upcoming", [_interview_item(i, labels, join=True) for i in upcoming]
            ),
            section(
                "interviews_past",
                [
                    _interview_item(i, labels, submitted=(i.id, actor.id) in done)
                    for i in sorted(past, key=lambda i: i.starts_at or now, reverse=True)
                ],
            ),
            section("load_week", [_interview_item(i, labels) for i in week]),
        ],
    )


# ------------------------------------------------------------------ HR or talent lead


async def talent(
    session: AsyncSession, actor: Actor, now: datetime, *, survey: bool, bias: bool
) -> Dashboard:
    tz, _today, _end = await _window(session, now)
    settings = await _settings(session)
    facts = await load_facts(session)
    result = compute(facts, now=now, settings=settings)
    by_id = {a.id: a for a in facts.apps}

    def records(metrics: Iterable[Metric]) -> list[Item]:
        seen: dict[str, Item] = {}
        for m in metrics:
            for r in m.records:
                app = by_id.get(uuid.UUID(r.id)) if r.kind == "application" else None
                if app is not None and r.id not in seen:
                    seen[r.id] = app_item(app, metric=m.key)
        return list(seen.values())

    flagged = records(result.flags)
    late = records(m for m in result.sla if m.key == "sla.anti_ghosting")
    breached = next(m for m in result.sla if m.key == "sla.approvals")
    jobs = await _jobs(session, (*WORKING_JOBS, JobStatus.DRAFT.value))
    links = ["analytics", "calibration"]
    if bias:
        links.append("bias")
    if survey:
        links.append("surveys")
    return Dashboard(
        role="talent",
        generated_at=now,
        timezone=tz,
        scope="all",
        action=[
            section("flags", flagged),
            section("sla", late),
            section(
                "approvals_late",
                [Item("approval", r.id, r.label) for r in breached.records],
                count=int(breached.value or 0),
            ),
        ],
        sections=[section("jobs", await _job_items(session, facts, jobs), count=len(jobs))],
        metrics=[
            trimmed(m)
            for m in (
                *result.stage_conversion,
                result.time_to_hire,
                *result.sources,
                result.ghosting_rate,
                *result.interviewer_load[:MAX_ITEMS],
                *result.agreement,
                result.override_rate,
                *result.flags,
            )
        ],
        links=links,
    )


# ------------------------------------------------------------------ employee referrer


async def referrer(session: AsyncSession, actor: Actor, now: datetime) -> Dashboard:
    tz, _today, _end = await _window(session, now)
    mine = await my_referrals(session, actor)
    jobs = await _jobs(session, (JobStatus.OPEN.value,))
    referred = [
        Item(
            "referral",
            str(r.referral_id),
            r.candidate,
            at=r.referred_at,
            detail={"job_title": r.job_title, "stage": r.stage, "stage_label": r.stage_label},
        )
        for r in mine
    ]
    return Dashboard(
        role="referrer",
        generated_at=now,
        timezone=tz,
        scope="mine",
        action=[
            section(
                "referrals_active", [r for r in referred if r.detail.get("stage") == "in_progress"]
            )
        ],
        sections=[
            section("referrals", referred),
            section(
                "open_jobs",
                [
                    Item("job", str(j.id), j.title, job_id=str(j.id), detail={"team": j.team})
                    for j in jobs
                ],
            ),
        ],
        links=["referrals"],
    )


__all__ = [
    "ROLES",
    "Dashboard",
    "Item",
    "Section",
    "StageCount",
    "hiring_manager",
    "interviewer",
    "recruiter",
    "referrer",
    "talent",
]
