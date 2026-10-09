# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``pairly-admin seed hire-demo --org <slug>``: an organisation ready for the Hire demo.

Idempotent, in one transaction (the CLI's), as the owner database role:

1. Hire settings: careers page visible (left alone if the organisation has settings).
2. The demo jobs of ``pairly_hire.demo`` (by slug; a job that exists is left alone):
   * Senior Backend Engineer and Customer Support Lead: open, with their rubric locked
     and the default application form, so the careers page and the apply flow work.
     The seed stands in for the recruiter who locked them; the policy versions and the
     audit events name the seed actor, never a person.
   * Accounts Payable Specialist: a draft with an AI rubric draft that is not locked,
     so a person can show the edit, lock and publish steps.
3. Invitations for a recruiter, a hiring manager and an interviewer, each limited to
   the Hire workflow.
4. The recorded model responses of ``data/recordings.json``, so drafting works offline.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from importlib.resources import files
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.invitations import invite_as_system
from pairly_core.llm.bundle import import_recordings, parse_bundle
from pairly_core.plugins import SeedContext, SeededInvitation, SeedResult
from pairly_core.policy_versions import publish_policy_version
from pairly_hire.demo import AP_SPECIALIST, DEMO_JOBS, DemoJob
from pairly_hire.forms import DEFAULT_FORM, form_validator
from pairly_hire.forms import POLICY_KIND as FORM_KIND
from pairly_hire.models import HireJob, HireOrgSettings, HireRubricDraft, JobStatus
from pairly_hire.rubric import POLICY_KIND as RUBRIC_KIND
from pairly_hire.rubric import policy_name, rubric_validator

SEED_ACTOR: Final = "pairly-admin.seed.hire-demo"
WORKFLOW: Final = "hire"
DEMO_PEOPLE: Final = (
    ("recruiter", "recruiter"),
    ("manager", "hiring_manager"),
    ("interviewer", "interviewer"),
)


def recordings_text() -> str:
    return files("pairly_hire").joinpath("data", "recordings.json").read_text(encoding="utf-8")


def demo_email(slug: str, who: str) -> str:
    return f"{slug}-hire-{who}@example.test"


async def _seed_job(session: AsyncSession, org_id: uuid.UUID, demo: DemoJob) -> str:
    existing = await session.scalar(select(HireJob.id).where(HireJob.slug == demo.slug))
    if existing is not None:
        return f"job {demo.slug}: already there, left as it is"
    now = datetime.now(UTC)
    job = HireJob(
        id=uuid.uuid4(),
        org_id=org_id,
        slug=demo.slug,
        title=demo.draft.title,
        title_ar=demo.draft.title_ar,
        team=demo.team,
        location=demo.location,
        country=demo.country,
        work_model=demo.work_model,
        employment_type=demo.employment_type,
        salary_min=demo.salary_min,
        salary_max=demo.salary_max,
        salary_currency=demo.salary_currency,
        salary_period=demo.salary_period,
        show_salary=demo.show_salary,
        description_en=demo.draft.description_en,
        description_ar=demo.draft.description_ar,
        status=JobStatus.DRAFT.value,
        created_by=None,
    )
    session.add(job)
    await session.flush()
    session.add(
        HireRubricDraft(
            id=uuid.uuid4(),
            org_id=org_id,
            job_id=job.id,
            content=demo.rubric.to_yaml(),
            source="ai",
        )
    )
    await session.flush()
    await append_audit(
        session,
        action="hire.job.created",
        actor_type=ActorType.SYSTEM,
        actor_id=SEED_ACTOR,
        entity_type="hire.job",
        entity_id=job.id,
        workflow=WORKFLOW,
        payload={"slug": job.slug, "seed": True},
    )
    if demo is AP_SPECIALIST:
        return f"job {demo.slug}: draft with an unlocked AI rubric draft"
    name = policy_name(job.id.hex)
    rubric = await publish_policy_version(
        session,
        kind=RUBRIC_KIND,
        name=name,
        content=demo.rubric.to_yaml(),
        validate=rubric_validator(),
        actor_type=ActorType.SYSTEM,
        actor_id=SEED_ACTOR,
    )
    form = await publish_policy_version(
        session,
        kind=FORM_KIND,
        name=name,
        content=DEFAULT_FORM.to_yaml(),
        validate=form_validator(),
        actor_type=ActorType.SYSTEM,
        actor_id=SEED_ACTOR,
    )
    job.rubric_policy_id = rubric.id
    job.form_policy_id = form.id
    job.status = JobStatus.OPEN.value
    job.published_at = now
    await session.flush()
    await append_audit(
        session,
        action="hire.job.published",
        actor_type=ActorType.SYSTEM,
        actor_id=SEED_ACTOR,
        entity_type="hire.job",
        entity_id=job.id,
        workflow=WORKFLOW,
        payload={"status": job.status, "rubric_version": rubric.version, "seed": True},
    )
    return f"job {demo.slug}: open, rubric version {rubric.version} locked by the seed"


async def seed_hire_demo(session: AsyncSession, ctx: SeedContext) -> SeedResult:
    lines: list[str] = []
    if await session.scalar(select(HireOrgSettings.id)) is None:
        session.add(HireOrgSettings(id=uuid.uuid4(), org_id=ctx.org_id, careers_enabled=True))
        await session.flush()
        await append_audit(
            session,
            action="hire.settings.changed",
            actor_type=ActorType.SYSTEM,
            actor_id=SEED_ACTOR,
            entity_type="hire.settings",
            workflow=WORKFLOW,
            payload={"careers_enabled": True, "seed": True},
        )
        lines.append("settings: careers page visible")
    for demo in DEMO_JOBS:
        lines.append(await _seed_job(session, ctx.org_id, demo))

    invitations: list[SeededInvitation] = []
    for who, role in DEMO_PEOPLE:
        email = demo_email(ctx.org_slug, who)
        created = await invite_as_system(
            session, ctx.registry, email=email, role=role, workflow=WORKFLOW, actor_id=SEED_ACTOR
        )
        if created is None:
            lines.append(f"{email}: already invited or a member")
            continue
        invitations.append(SeededInvitation(email, role, WORKFLOW, created.token))

    report = await import_recordings(session, ctx.org_id, parse_bundle(recordings_text()))
    lines.append(
        f"recorded model responses: {report.imported} imported, {report.skipped} already there"
    )
    lines.append(f"careers page: {ctx.web_url.rstrip('/')}/en/careers/{ctx.org_slug}")
    return SeedResult(lines=tuple(lines), invitations=tuple(invitations))
