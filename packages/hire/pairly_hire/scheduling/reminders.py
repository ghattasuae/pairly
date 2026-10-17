# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Interview reminders (brief 3.6.5): 24 hours and 1 hour before, to both sides.

``run_reminders_job`` (``hire_interview_reminders``, every 5 minutes, worker): per
organisation with applicants, every booked interview starting within 24 hours:

* starting within 1 hour and not reminded at 1 hour: the 1 hour reminder (urgent for
  the panel: an interview starting soon is one of the things that may interrupt,
  brief 5.3), and the 24 hour one is marked done so it is never sent late;
* else not reminded at 24 hours: the 24 hour reminder.

Rows are taken with ``FOR UPDATE SKIP LOCKED`` and marked in the same transaction, and
every email has a dedupe key with the interview's sequence, so a reminder goes once per
time; moving an interview clears both marks (``service._book``) and the new time gets
its own reminders. Audited (``hire.interview.reminded``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType
from pairly_core.db import set_tenant
from pairly_core.errors import NotFoundError
from pairly_core.jobs.registry import JobContext
from pairly_hire.messaging import library
from pairly_hire.messaging.compose import Author
from pairly_hire.pipeline.records import load_record
from pairly_hire.pipeline.rediscovery import org_ids
from pairly_hire.scheduling import emails
from pairly_hire.scheduling import templates as tpl
from pairly_hire.scheduling.emails import CandidateMail, PanelMail, SchedulingEnv
from pairly_hire.scheduling.models import ACTIVE, HireInterview
from pairly_hire.scheduling.service import audit_interview, panel_of, panel_people

log = structlog.get_logger("pairly_hire.scheduling.reminders")

JOB_NAME: Final = "hire_interview_reminders"
REMINDER_ACTOR: Final = "pairly.hire.reminders"
MAX_PER_RUN: Final = 200


async def send_due_reminders(
    session: AsyncSession, env: SchedulingEnv, *, now: datetime | None = None
) -> dict[str, int]:
    """One organisation's pass (the session is bound to it, in a transaction)."""
    now = now or datetime.now(UTC)
    rows = list(
        await session.scalars(
            select(HireInterview)
            .where(
                HireInterview.status.in_(ACTIVE),
                HireInterview.starts_at > now,
                HireInterview.starts_at <= now + timedelta(hours=24),
                or_(
                    HireInterview.reminded_24h_at.is_(None),
                    HireInterview.reminded_1h_at.is_(None),
                ),
            )
            .order_by(HireInterview.starts_at)
            .limit(MAX_PER_RUN)
            .with_for_update(skip_locked=True)
        )
    )
    author = Author(ActorType.SYSTEM, REMINDER_ACTOR)
    counts = {"day": 0, "hour": 0}
    for interview in rows:
        assert interview.starts_at is not None  # noqa: S101 (active)
        soon = interview.starts_at - now <= timedelta(hours=1)
        if soon and interview.reminded_1h_at is None:
            kind = "1h"
        elif not soon and interview.reminded_24h_at is None:
            kind = "24h"
        else:
            continue
        try:
            record = await load_record(session, interview.application_id)
        except NotFoundError:
            continue
        panel = await panel_people(session, await panel_of(session, interview.id))
        key = f"hire.iv:{interview.id}:{interview.ics_sequence}:remind:{kind}"
        await emails.send_to_candidate(
            session,
            env,
            interview,
            record,
            CandidateMail(library.INTERVIEW_REMINDER, {}, None, key),
            author=author,
            panel_size=len(panel),
        )
        await emails.send_to_panel(
            session,
            env,
            interview,
            record,
            panel,
            PanelMail(
                tpl.PANEL_REMINDER,
                None,
                key,
                urgent=kind == "1h",
                channels=("email", "in_app"),
            ),
            author=author,
        )
        if kind == "1h":
            interview.reminded_1h_at = now
            interview.reminded_24h_at = interview.reminded_24h_at or now
            counts["hour"] += 1
        else:
            interview.reminded_24h_at = now
            counts["day"] += 1
        await session.flush()
        await audit_interview(session, author, "hire.interview.reminded", interview, {"kind": kind})
    return counts


async def run_reminders_job(ctx: JobContext) -> dict[str, Any]:
    """``hire_interview_reminders``: every 5 minutes, per organisation with applicants."""
    env = SchedulingEnv.from_environment()
    totals: dict[str, Any] = {"orgs": 0, "day": 0, "hour": 0, "failed": 0}
    for org_id in await org_ids(ctx):
        session = ctx.sessionmaker()
        set_tenant(session, org_id, None)
        try:
            async with session, session.begin():
                counts = await send_due_reminders(session, env)
        except Exception as exc:
            log.warning("interview_reminders_failed", org_id=str(org_id), error=type(exc).__name__)
            totals["failed"] += 1
            continue
        totals["orgs"] += 1
        totals["day"] += counts["day"]
        totals["hour"] += counts["hour"]
    return totals
