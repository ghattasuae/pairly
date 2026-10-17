# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Employee referrals (brief 3.4.4): signed links, tracking, and a privacy-safe view.

* ``referral_link``: one link per employee and open job (``hire.referral.create``,
  employees included). The link is the job's careers page with ``?ref=<token>``; the
  token is signed (``pairly_hire.links``, ``rf1``) and names the link row, which names
  the employee and the job. Revoking the row ends the link.
* ``attach_referral``: the apply flow passes the token on; a valid token for this
  organisation and this job records ``hire_referrals`` (application, link, referrer).
  An invalid, revoked or foreign token is ignored: the application goes on without a
  referral (the candidate never sees an error for it). The referrer can never be the
  candidate's own contact, since the link only names an employee.
* ``my_referrals`` (``hire.referral.read``): the employee's own referrals with the job
  and the stage only. Never scores, notes, messages, flags or contact details; the
  candidate is named only once the stage reveals identity when blind mode is on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit, audit_actor_action
from pairly_core.errors import ConflictError, ForbiddenError, ServiceUnavailableError
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_hire.jobs import get_job
from pairly_hire.links import LinkConfig, read_referral_token, referral_token
from pairly_hire.models import HireApplication, HireCandidate, HireJob, JobStatus
from pairly_hire.permissions import REFERRAL_CREATE, REFERRAL_READ
from pairly_hire.pipeline.hooks import blind_applications
from pairly_hire.pipeline.models import HirePipelineEntry, HireReferral, HireReferralLink
from pairly_hire.pipeline.records import AppRecord
from pairly_hire.pipeline.stages import stages_for_jobs

WORKFLOW: Final = "hire"
REFERRAL_ACTOR: Final = "pairly.hire.apply"


@dataclass(frozen=True, slots=True)
class Link:
    id: uuid.UUID
    job_id: uuid.UUID
    url: str


def _url(config: LinkConfig, org_slug: str, job: HireJob, token: str, locale: str) -> str:
    return f"{config.web_url}/{locale}/careers/{org_slug}/{job.slug}?ref={token}"


async def referral_link(
    session: AsyncSession,
    actor: Actor,
    job_id: uuid.UUID,
    *,
    config: LinkConfig,
    org_slug: str,
    locale: str = "en",
) -> Link:
    """The actor's link for an open job (created on first use). Audited when created."""
    authorize(actor, REFERRAL_CREATE)
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can refer someone")
    if not config.ready:
        raise ServiceUnavailableError("referral links are not configured here")
    assert config.secret is not None  # noqa: S101 (ready)
    job = await get_job(session, job_id)
    if job.status != JobStatus.OPEN.value:
        raise ConflictError("you can only refer people to open jobs")
    link = await session.scalar(
        select(HireReferralLink).where(
            HireReferralLink.job_id == job.id,
            HireReferralLink.referrer_id == actor.id,
            HireReferralLink.revoked_at.is_(None),
        )
    )
    if link is None:
        link = HireReferralLink(
            id=uuid.uuid4(),
            org_id=actor.org_id,
            job_id=job.id,
            referrer_id=actor.id,
            created_at=datetime.now(UTC),
        )
        session.add(link)
        await session.flush()
        await audit_actor_action(
            session,
            actor,
            "hire.referral.link_created",
            entity_type="hire.job",
            entity_id=job.id,
            workflow=WORKFLOW,
            payload={"link_id": str(link.id)},
        )
    token = referral_token(config.secret, actor.org_id, link.id)
    return Link(link.id, job.id, _url(config, org_slug, job, token, locale))


async def attach_referral(
    session: AsyncSession,
    application: HireApplication,
    token: str | None,
    secret: str | None,
) -> HireReferral | None:
    """Record the referral of a new application, if ``token`` is valid for it."""
    if not token or not secret:
        return None
    claim = read_referral_token(secret, token)
    if claim is None or claim.org_id != application.org_id:
        return None
    link = await session.scalar(
        select(HireReferralLink).where(
            HireReferralLink.id == claim.link_id, HireReferralLink.revoked_at.is_(None)
        )
    )
    if link is None or link.job_id != application.job_id:
        return None
    existing = await session.scalar(
        select(HireReferral).where(HireReferral.application_id == application.id)
    )
    if existing is not None:
        return existing
    referral = HireReferral(
        id=uuid.uuid4(),
        org_id=application.org_id,
        application_id=application.id,
        job_id=application.job_id,
        link_id=link.id,
        referrer_id=link.referrer_id,
        created_at=datetime.now(UTC),
    )
    session.add(referral)
    await session.flush()
    await append_audit(
        session,
        action="hire.referral.recorded",
        actor_type=ActorType.SYSTEM,
        actor_id=REFERRAL_ACTOR,
        entity_type="hire.application",
        entity_id=application.id,
        workflow=WORKFLOW,
        payload={"link_id": str(link.id), "referrer_id": str(link.referrer_id)},
    )
    return referral


@dataclass(frozen=True, slots=True)
class ReferralView:
    """All a referrer may see about a person they referred."""

    referral_id: uuid.UUID
    job_title: str
    candidate: str
    stage: str
    stage_label: str
    referred_at: datetime


async def my_referrals(session: AsyncSession, actor: Actor) -> list[ReferralView]:
    authorize(actor, REFERRAL_READ)
    rows = (
        await session.execute(
            select(HireReferral, HireApplication, HireCandidate, HireJob, HirePipelineEntry)
            .join(HireApplication, HireApplication.id == HireReferral.application_id)
            .join(HireCandidate, HireCandidate.id == HireApplication.candidate_id)
            .join(HireJob, HireJob.id == HireReferral.job_id)
            .outerjoin(HirePipelineEntry, HirePipelineEntry.application_id == HireApplication.id)
            .where(HireReferral.referrer_id == actor.id)
            .order_by(HireReferral.created_at.desc())
            .limit(200)
        )
    ).all()
    stages = await stages_for_jobs(session, list({r[3].id for r in rows}))
    blind = await blind_applications(session, [r[1].id for r in rows])
    out: list[ReferralView] = []
    for referral, app, candidate, job, entry in rows:
        record = AppRecord(app, candidate, job, entry, stages[job.id])
        stage = record.stage
        # Outcomes the referrer sees: in progress, hired, or "not selected" (never why).
        label = (
            stage.label
            if stage.kind in {"active", "hired"}
            else ("Not selected" if stage.kind == "rejected" else "Withdrawn")
        )
        out.append(
            ReferralView(
                referral_id=referral.id,
                job_title=job.title,
                candidate=record.label(app.id in blind),
                stage=stage.kind if stage.terminal else "in_progress",
                stage_label=label,
                referred_at=referral.created_at,
            )
        )
    return out
