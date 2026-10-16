# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Anti-ghosting (brief 3.5.3): nobody waits more than N days without hearing from us.

A candidate is waiting from the later of the day they applied and the last message we
sent them (``hire_pipeline_entries.last_update_at``, set by every queued message). The
clock only runs while the application is in an active stage (not hired, rejected or
withdrawn). N is per organisation (``hire_messaging_settings.sla_days``, default 7).

``run_sla_job`` (``hire_sla_drafts``, every 15 minutes, worker): for each organisation
and each application waiting N days or more that has no open draft:

1. drafts a status update (``hire.candidate.status_update``, purpose ``sla_update``),
   never sends it: a person reads and sends it from the profile (one click);
2. marks the entry (``sla_drafted_at``) so it is drafted once per wait;
3. alerts the recruiters of the job once a day, in the app and by email (category
   ``sla``, urgent): "N candidates are waiting for an update".

The same job restarts sensitive messages whose approval run never started
(``graph.restart_stuck``).

``waiting_longest`` lists the applications waiting longest, for the Hire home page.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.service import member_permissions, role_members
from pairly_core.audit import ActorType, append_audit, audit_actor_action
from pairly_core.db import set_tenant
from pairly_core.jobs.registry import JobContext
from pairly_core.notifications.service import Extras, Recipient, notify
from pairly_core.rbac import Actor, RoleRegistry
from pairly_hire.links import LinkConfig, links_from_env, pipeline_path
from pairly_hire.messaging import library
from pairly_hire.messaging.compose import Author, create_draft
from pairly_hire.messaging.graph import restart_stuck
from pairly_hire.messaging.models import (
    DEFAULT_SLA_DAYS,
    HireMessage,
    HireMessagingSettings,
    MessagePurpose,
)
from pairly_hire.models import ApplicationStatus, HireApplication, HireCandidate, HireJob, JobStatus
from pairly_hire.permissions import CANDIDATE_DECIDE
from pairly_hire.pipeline.hooks import blind_mode
from pairly_hire.pipeline.models import HirePipelineEntry
from pairly_hire.pipeline.records import AppRecord, ensure_entry
from pairly_hire.pipeline.rediscovery import org_ids
from pairly_hire.pipeline.stages import stages_for_jobs
from pairly_hire.pipeline.views import OPEN_MESSAGE, days_between
from pairly_hire.recruiters import job_recruiters

log = structlog.get_logger("pairly_hire.sla")

WORKFLOW: Final = "hire"
JOB_NAME: Final = "hire_sla_drafts"
SLA_ACTOR: Final = "pairly.hire.sla"
MAX_PER_ORG: Final = 200


async def sla_days(session: AsyncSession) -> int:
    value = await session.scalar(select(HireMessagingSettings.sla_days))
    return int(value) if value is not None else DEFAULT_SLA_DAYS


async def save_sla_days(session: AsyncSession, actor: Actor, days: int) -> int:
    row = await session.scalar(select(HireMessagingSettings).with_for_update())
    now = datetime.now(UTC)
    if row is None:
        row = HireMessagingSettings(id=uuid.uuid4(), org_id=actor.org_id, sla_days=days)
        session.add(row)
    row.sla_days = days
    row.updated_by = actor.id
    row.updated_at = now
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        "hire.settings.changed",
        entity_type="hire.settings",
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={"sla_days": days},
    )
    return days


@dataclass(frozen=True, slots=True)
class Waiting:
    record: AppRecord
    waiting_days: int
    since: datetime
    has_open_draft: bool


async def waiting(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    job_id: uuid.UUID | None = None,
    min_days: int = 0,
) -> list[Waiting]:
    """Active applications of open and paused jobs, longest waiting first."""
    now = now or datetime.now(UTC)
    query = (
        select(HireApplication, HireCandidate, HireJob, HirePipelineEntry)
        .join(HireCandidate, HireCandidate.id == HireApplication.candidate_id)
        .join(HireJob, HireJob.id == HireApplication.job_id)
        .outerjoin(HirePipelineEntry, HirePipelineEntry.application_id == HireApplication.id)
        .where(
            HireApplication.status == ApplicationStatus.RECEIVED.value,
            HireJob.status.in_([JobStatus.OPEN.value, JobStatus.PAUSED.value]),
        )
    )
    if job_id is not None:
        query = query.where(HireApplication.job_id == job_id)
    rows = (await session.execute(query)).all()
    stages = await stages_for_jobs(session, list({r[2].id for r in rows}))
    open_ids = set(
        await session.scalars(
            select(HireMessage.application_id).where(
                HireMessage.application_id.in_([r[0].id for r in rows]),
                HireMessage.status.in_(OPEN_MESSAGE),
            )
        )
    )
    out: list[Waiting] = []
    for app, candidate, job, entry in rows:
        record = AppRecord(app, candidate, job, entry, stages[job.id])
        if record.stage.terminal:
            continue
        since = record.waiting_since()
        days = days_between(since, now)
        if days < min_days:
            continue
        out.append(Waiting(record, days, since, app.id in open_ids))
    out.sort(key=lambda w: (w.since, str(w.record.app.id)))
    return out


async def _recipients(
    session: AsyncSession, registry: RoleRegistry, job: HireJob
) -> list[uuid.UUID]:
    people = job_recruiters(job)
    if not people:
        people = await role_members(session, ["recruiter"], WORKFLOW)
    perms = await member_permissions(session, registry, people)
    return sorted((p for p in people if CANDIDATE_DECIDE in perms.get(p, frozenset())), key=str)


async def draft_overdue(
    session: AsyncSession,
    registry: RoleRegistry,
    *,
    links: LinkConfig,
    now: datetime | None = None,
) -> dict[str, int]:
    """One organisation's SLA pass (see the module docstring). Runs in the caller's
    transaction, which must be bound to the organisation."""
    now = now or datetime.now(UTC)
    days = await sla_days(session)
    due = [
        w
        for w in await waiting(session, now=now, min_days=days)
        if not w.has_open_draft
        and (w.record.entry is None or w.record.entry.sla_drafted_at is None)
    ][:MAX_PER_ORG]
    author = Author(ActorType.SYSTEM, SLA_ACTOR)
    per_job: dict[uuid.UUID, list[Waiting]] = defaultdict(list)
    for item in due:
        record = item.record
        await create_draft(
            session,
            record,
            author,
            template_key=library.STATUS_UPDATE,
            purpose=MessagePurpose.SLA_UPDATE,
            links=links,
        )
        entry = await ensure_entry(session, record)
        entry.sla_drafted_at = now
        entry.updated_at = now
        per_job[record.job.id].append(item)
    await session.flush()
    alerts = 0
    blind = await blind_mode(session)
    for items in per_job.values():
        job = items[0].record.job
        recipients = await _recipients(session, registry, job)
        url = f"{links.web_url}/en/hire/jobs/{job.id}/pipeline?sla=1" if links.web_url else None
        await append_audit(
            session,
            action="hire.sla.alerted",
            actor_type=ActorType.SYSTEM,
            actor_id=SLA_ACTOR,
            entity_type="hire.job",
            entity_id=job.id,
            workflow=WORKFLOW,
            payload={
                "drafted": len(items),
                "sla_days": days,
                "recipients": [str(r) for r in recipients],
                "blind_mode": blind,
            },
        )
        for user_id in recipients:
            variables: dict[str, Any] = {"job_title": job.title, "count": len(items), "days": days}
            channels = ["in_app"]
            if url is not None:
                variables["pipeline_url"] = url
                channels.append("email")
            await notify(
                session,
                recipient=Recipient.user(user_id),
                template_key=library.SLA_WAITING,
                variables=variables,
                dedupe_key=f"hire.sla:{job.id}:{user_id}:{now.date().isoformat()}",
                channels=channels,
                extras=Extras(link=pipeline_path(job.id, sla=True)),
                workflow=WORKFLOW,
                actor_type=ActorType.SYSTEM,
                actor_id=SLA_ACTOR,
            )
            alerts += 1
    return {"drafted": len(due), "jobs": len(per_job), "alerts": alerts}


async def run_sla_job(ctx: JobContext) -> dict[str, Any]:
    """``hire_sla_drafts``: every 15 minutes, per organisation with applicants."""
    links = links_from_env()
    totals = {"orgs": 0, "drafted": 0, "alerts": 0, "failed": 0}
    for org_id in await org_ids(ctx):
        session = ctx.sessionmaker()
        set_tenant(session, org_id, None)
        try:
            async with session, session.begin():
                result = await draft_overdue(session, ctx.registry, links=links)
        except Exception as exc:
            log.warning("sla_drafts_failed", org_id=str(org_id), error=type(exc).__name__)
            totals["failed"] += 1
            continue
        totals["orgs"] += 1
        totals["drafted"] += result["drafted"]
        totals["alerts"] += result["alerts"]
        try:
            totals["restarted"] = totals.get("restarted", 0) + await restart_stuck(
                ctx.sessionmaker, ctx.runtime, ctx.registry, org_id
            )
        except Exception as exc:
            log.warning("message_restart_failed", org_id=str(org_id), error=type(exc).__name__)
    return totals
