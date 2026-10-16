# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire's sections of the shared dashboards (``pairly_core.dashboards``).

* Admin: open candidate data requests (action needed) and the calendar and meeting
  integrations (Google, Microsoft, Zoom), from the same function as the settings page
  (``GET /v1/hire/settings/scheduling/integrations``).
* AI governance: the guardrail findings of the last 30 days' screenings, the latest
  fairness runs (the counterfactual runner) and the calibration agreement of the jobs
  people labelled.

Counts and ids only: no candidate's name, CV or answer is shown here. The guardrail and
calibration sections need ``hire.governance.read`` (admins, recruiters and auditors),
not ``hire.analytics.read``: an auditor sees the numbers without the analytics pages.
Without ``hire.analytics.read`` a calibration row names the job by its id (an unopened
job's title can be confidential) and links nowhere.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Final

from sqlalchemy import func, select

from pairly_core.dashboards.model import (
    Board,
    DashboardSectionSpec,
    Item,
    Section,
    SectionContext,
    Stat,
    Unit,
    may,
    section,
)
from pairly_hire.analytics import calibration
from pairly_hire.analytics.models import HireCalibrationLabel, HireFairnessRun
from pairly_hire.models import HireJob
from pairly_hire.permissions import ANALYTICS_READ, BIAS_READ, GOVERNANCE_READ, SETTINGS_MANAGE
from pairly_hire.portal.models import DataRequestStatus, HireDataRequest
from pairly_hire.portal.notices import SLA_DAYS
from pairly_hire.screening.models import HireScreening

GUARDRAIL_DAYS: Final = 30
FLAGGED_PREFIX: Final = "guardrails_flagged:"
CALIBRATION_JOBS: Final = 5


async def data_requests(ctx: SectionContext) -> Section:
    rows = list(
        await ctx.session.scalars(
            select(HireDataRequest)
            .where(HireDataRequest.status == DataRequestStatus.OPEN.value)
            .order_by(HireDataRequest.created_at)
        )
    )
    items = []
    overdue = 0
    for r in rows:
        due = r.created_at + timedelta(days=SLA_DAYS.get(r.kind, 30))
        late = due < ctx.now
        overdue += int(late)
        items.append(
            Item(
                kind="data_request",
                id=str(r.id),
                label=r.kind,
                href="/hire/data-requests",
                at=due,
                status="overdue" if late else "open",
                detail={"erasure_pending": r.erasure_started_at is not None},
            )
        )
    out = section("data_requests", items, href="/hire/data-requests")
    out.stats = [Stat("data_requests_overdue", float(overdue), href="/hire/data-requests")]
    return out


async def integrations(ctx: SectionContext) -> Section:
    # The settings page's own function (imported here: the API routes load lazily).
    from pairly_hire.api.scheduling import org_integrations

    status = await org_integrations(ctx.actor, ctx.session)
    items = [
        Item(
            kind="integration",
            id=p.provider,
            label=p.provider,
            href="/settings/calendar",
            status="connected" if p.state == "connected" else "needs_setup",
            at=p.verified_at,
            detail={
                "configured": p.configured,
                "flag_enabled": p.flag_enabled,
                "connected_people": p.connected_people,
                "missing": ", ".join(p.missing) or None,
            },
        )
        for p in status.providers
    ]
    needing = sum(1 for i in items if i.status != "connected")
    return Section(
        key="calendar_integrations",
        count=needing,
        href="/settings/calendar",
        items=items,
        stats=[
            Stat(
                "operations_needing_review",
                float(status.operations_needing_review),
                href="/settings/calendar",
            )
        ],
    )


async def guardrails(ctx: SectionContext) -> Section:
    since = ctx.now - timedelta(days=GUARDRAIL_DAYS)
    rows = list(
        await ctx.session.scalars(
            select(HireScreening.review_reasons).where(HireScreening.created_at >= since)
        )
    )
    by_type: Counter[str] = Counter()
    flagged = errors = 0
    for reasons in rows:
        hit = False
        for reason in reasons or []:
            if isinstance(reason, str) and reason.startswith(FLAGGED_PREFIX):
                hit = True
                by_type.update(t for t in reason[len(FLAGGED_PREFIX) :].split(",") if t)
            elif reason == "guardrails_error":
                errors += 1
        flagged += int(hit)
    return Section(
        key="guardrails_hire",
        count=flagged,
        href="/hire",
        stats=[
            Stat("screenings", float(len(rows))),
            Stat("guardrail_errors", float(errors)),
            *(Stat(f"finding_{kind}", float(n)) for kind, n in sorted(by_type.items())),
        ],
        source="hire_screening_review_reasons",
        note=None if rows else "no_data",
    )


async def fairness(ctx: SectionContext) -> Section:
    runs = list(
        await ctx.session.scalars(
            select(HireFairnessRun).order_by(HireFairnessRun.started_at.desc()).limit(5)
        )
    )
    return section(
        "fairness_runs",
        [
            Item(
                kind="fairness_run",
                id=str(r.id),
                label=r.trigger,
                href="/hire/bias",
                at=r.finished_at or r.started_at,
                status=r.status,
                detail={
                    "mode": r.mode,
                    "applications": r.applications,
                    "copies": r.copies,
                    "flagged": r.flagged,
                    "threshold": float(r.threshold),
                },
            )
            for r in runs
        ],
        href="/hire/bias",
        source="hire_fairness_runs",
        note=None if runs else "no_data",
    )


async def calibration_agreement(ctx: SectionContext) -> Section:
    jobs = (
        await ctx.session.execute(
            select(HireCalibrationLabel.job_id, func.max(HireCalibrationLabel.updated_at))
            .group_by(HireCalibrationLabel.job_id)
            .order_by(func.max(HireCalibrationLabel.updated_at).desc())
            .limit(CALIBRATION_JOBS)
        )
    ).all()
    analytics = may(ctx.actor, ANALYTICS_READ)
    titles = (
        {
            j.id: j.title
            for j in await ctx.session.scalars(
                select(HireJob).where(HireJob.id.in_([job_id for job_id, _ in jobs]))
            )
        }
        if jobs and analytics
        else {}
    )
    items = []
    for job_id, labelled_at in jobs:
        report = await calibration.report(ctx.session, job_id)
        exact = [c.exact for c in report.criteria if c.exact is not None]
        items.append(
            Item(
                kind="calibration",
                id=str(job_id),
                label=titles.get(job_id, str(job_id)),
                href=f"/hire/calibration?job_id={job_id}" if analytics else None,
                at=labelled_at,
                detail={
                    "labels": report.labels,
                    "overrides": report.overrides,
                    "criteria": len(report.criteria),
                    "low_agreement": sum(1 for c in report.criteria if c.low_agreement),
                    "mean_exact_agreement": round(sum(exact) / len(exact), 4) if exact else None,
                },
            )
        )
    link = "/hire/calibration" if analytics else None
    return Section(
        key="calibration",
        count=len(items),
        href=link,
        items=items,
        stats=[
            Stat(
                "low_agreement_criteria",
                float(sum(int(i.detail["low_agreement"] or 0) for i in items)),
                Unit.COUNT,
                href=link,
            )
        ],
        source="hire_calibration_labels",
        note=None if items else "no_data",
    )


DASHBOARD_SECTIONS: Final[tuple[DashboardSectionSpec, ...]] = (
    DashboardSectionSpec(Board.ADMIN, "data_requests", SETTINGS_MANAGE, data_requests, action=True),
    DashboardSectionSpec(Board.ADMIN, "calendar_integrations", SETTINGS_MANAGE, integrations),
    # Counts only, so auditors see them too (hire.governance.read).
    DashboardSectionSpec(Board.AI_GOVERNANCE, "guardrails_hire", GOVERNANCE_READ, guardrails),
    # The fairness runs page also opens to the bias report's readers (auditors).
    DashboardSectionSpec(Board.AI_GOVERNANCE, "fairness_runs", BIAS_READ, fairness),
    DashboardSectionSpec(
        Board.AI_GOVERNANCE, "calibration", GOVERNANCE_READ, calibration_agreement
    ),
)
