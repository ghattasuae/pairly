# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Feedback reminders (brief 3.7.3, 5.3): 2 hours and 24 hours after the interview.

``run_scorecard_reminders_job`` (``hire_scorecard_reminders``, every 5 minutes, worker):
per organisation with applicants, every interview that ended between 7 days and 2 hours
ago and still takes scorecards; for each person on its panel without a submitted
scorecard:

* snoozed ("remind me later") and the snooze is over: one more reminder, then the snooze
  is cleared; still snoozed: nothing;
* else, not reminded at 2 hours: the 2 hour reminder;
* else, 24 hours after the end and not reminded at 24 hours: the 24 hour reminder
  (marked overdue when past the plan's SLA).

Each reminder carries a fresh one-click link to the scorecard and a "remind me later"
link (``links``). Scorecard rows are taken ``FOR UPDATE SKIP LOCKED`` and marked in the
same transaction, and every email has a dedupe key, so a reminder goes once. Audited
(``hire.scorecard.reminded``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.db import set_tenant
from pairly_core.errors import PairlyError
from pairly_core.jobs.registry import JobContext
from pairly_core.notifications.service import Extras, Recipient, notify
from pairly_hire.interviewing.links import issue_link
from pairly_hire.interviewing.models import HireScorecard
from pairly_hire.interviewing.plan import interview_stage
from pairly_hire.interviewing.templates import SCORECARD_REMINDER
from pairly_hire.links import kit_path
from pairly_hire.pipeline.hooks import application_blind
from pairly_hire.pipeline.records import load_record
from pairly_hire.pipeline.rediscovery import org_ids
from pairly_hire.scheduling.emails import SchedulingEnv
from pairly_hire.scheduling.models import HireInterview, HireInterviewPanelist

log = structlog.get_logger("pairly_hire.interviewing.reminders")

JOB_NAME: Final = "hire_scorecard_reminders"
ACTOR: Final = "pairly.hire.scorecard_reminders"
FIRST_AFTER: Final = timedelta(hours=2)
SECOND_AFTER: Final = timedelta(hours=24)
WINDOW: Final = timedelta(days=7)
MAX_PER_RUN: Final = 200
SCOREABLE: Final = ("booked", "rescheduled", "completed")


def scorecard_url(env: SchedulingEnv, token: str) -> str | None:
    base = env.links.web_url
    return f"{base}/en/scorecard#{token}" if base else None


def due_kind(row: HireScorecard, ends_at: datetime, now: datetime) -> str | None:
    """Which reminder is due now for this seat, if any."""
    if row.status == "submitted":
        return None
    if row.snoozed_until is not None:
        return "later" if now >= row.snoozed_until else None
    if row.reminded_2h_at is None and now >= ends_at + FIRST_AFTER:
        if now >= ends_at + SECOND_AFTER:
            return "24h"
        return "2h"
    if row.reminded_24h_at is None and now >= ends_at + SECOND_AFTER:
        return "24h"
    return None


async def _seat(
    session: AsyncSession,
    interview: HireInterview,
    user_id: uuid.UUID,
    stage_key: str,
    criteria: list[str],
) -> HireScorecard | None:
    seat = (HireScorecard.interview_id == interview.id, HireScorecard.interviewer_id == user_id)
    existing = await session.scalar(select(HireScorecard.id).where(*seat))
    if existing is not None:
        # Someone is writing it right now (skip locked): next run.
        return await session.scalar(
            select(HireScorecard).where(*seat).with_for_update(skip_locked=True)
        )
    now = datetime.now(UTC)
    row = HireScorecard(
        id=uuid.uuid4(),
        org_id=interview.org_id,
        interview_id=interview.id,
        application_id=interview.application_id,
        job_id=interview.job_id,
        stage_key=stage_key,
        interviewer_id=user_id,
        criteria=criteria,
        ratings={},
        notes="",
        status="draft",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def send_due_reminders(
    session: AsyncSession, env: SchedulingEnv, *, now: datetime | None = None
) -> dict[str, int]:
    """One organisation's pass (the session is bound to it, in a transaction)."""
    now = now or datetime.now(UTC)
    interviews = list(
        await session.scalars(
            select(HireInterview)
            .where(
                HireInterview.status.in_(SCOREABLE),
                HireInterview.ends_at.is_not(None),
                HireInterview.ends_at <= now - FIRST_AFTER,
                HireInterview.ends_at >= now - WINDOW,
            )
            .order_by(HireInterview.ends_at)
            .limit(MAX_PER_RUN)
        )
    )
    counts = {"2h": 0, "24h": 0, "later": 0}
    for interview in interviews:
        assert interview.ends_at is not None  # noqa: S101 (filtered)
        try:
            record = await load_record(session, interview.application_id)
        except PairlyError:
            continue
        found = await interview_stage(session, record.job, interview.stage_key)
        if found is None:
            continue
        stage, loaded = found
        due_at = interview.ends_at + timedelta(hours=loaded.plan.sla_hours)
        blind = await application_blind(session, record.app.id)
        label = record.label(blind)
        panel = list(
            await session.scalars(
                select(HireInterviewPanelist.user_id).where(
                    HireInterviewPanelist.interview_id == interview.id,
                    HireInterviewPanelist.removed_at.is_(None),
                )
            )
        )
        for user_id in panel:
            row = await _seat(session, interview, user_id, stage.key, stage.criteria())
            if row is None:
                continue
            kind = due_kind(row, interview.ends_at, now)
            if kind is None:
                continue
            open_url = scorecard_url(env, await issue_link(session, interview, user_id, now=now))
            later_url = scorecard_url(
                env, await issue_link(session, interview, user_id, purpose="later", now=now)
            )
            values: dict[str, Any] = {
                "candidate_label": label,
                "job_title": record.job.title,
                "starts_at": interview.starts_at,
                "due_at": due_at,
                "overdue": now >= due_at,
            }
            if open_url:
                values["scorecard_url"] = open_url
            if later_url:
                values["later_url"] = later_url
            await notify(
                session,
                recipient=Recipient.user(user_id),
                template_key=SCORECARD_REMINDER,
                variables=values,
                dedupe_key=f"hire.sc:{row.id}:{kind}:{(row.snoozed_until or now).isoformat()}",
                channels=["email"],
                extras=Extras(link=kit_path(record.app.id)),
                workflow="hire",
                actor_type=ActorType.SYSTEM,
                actor_id=ACTOR,
            )
            if kind == "later":
                row.snoozed_until = None
            elif kind == "2h":
                row.reminded_2h_at = now
            else:
                row.reminded_2h_at = row.reminded_2h_at or now
                row.reminded_24h_at = now
            row.updated_at = now
            counts[kind] += 1
            await session.flush()
            await append_audit(
                session,
                action="hire.scorecard.reminded",
                actor_type=ActorType.SYSTEM,
                actor_id=ACTOR,
                entity_type="hire.scorecard",
                entity_id=row.id,
                workflow="hire",
                payload={"kind": kind, "interview_id": str(interview.id), "user_id": str(user_id)},
            )
    return counts


async def run_scorecard_reminders_job(ctx: JobContext) -> dict[str, Any]:
    """``hire_scorecard_reminders``: every 5 minutes, per organisation with applicants."""
    env = SchedulingEnv.from_environment()
    totals: dict[str, Any] = {"orgs": 0, "2h": 0, "24h": 0, "later": 0, "failed": 0}
    for org_id in await org_ids(ctx):
        session = ctx.sessionmaker()
        set_tenant(session, org_id, None)
        try:
            async with session, session.begin():
                counts = await send_due_reminders(session, env)
        except Exception as exc:
            log.warning("scorecard_reminders_failed", org_id=str(org_id), error=type(exc).__name__)
            totals["failed"] += 1
            continue
        totals["orgs"] += 1
        for key in ("2h", "24h", "later"):
            totals[key] += counts[key]
    return totals
