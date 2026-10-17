# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Recruiter assignment: a job's owner recruiter and optional co-recruiters.

The owner defaults to the job's creator. The owner and the co-recruiters are the people
told about a candidate's replies and uploads (``pairly_hire.portal.service``) and about
candidates waiting too long (``pairly_hire.messaging.sla``). Every assigned person must
be a member of the organisation who can read candidates. Changes are audited.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.service import member_permissions
from pairly_core.audit import audit_actor_action
from pairly_core.errors import BadRequestError, ConflictError
from pairly_core.rbac import Actor, RoleRegistry
from pairly_hire.models import HireJob, JobStatus
from pairly_hire.permissions import CANDIDATE_READ

ENTITY: Final = "hire.job"
WORKFLOW: Final = "hire"
ASSIGNED: Final = "hire.job.recruiters_assigned"
MAX_CO_RECRUITERS: Final = 10


def owner_of(job: HireJob) -> uuid.UUID | None:
    """The owner recruiter: the assigned owner, else the creator."""
    return job.owner_id if job.owner_id is not None else job.created_by


def job_recruiters(job: HireJob) -> set[uuid.UUID]:
    """The owner and the co-recruiters of ``job`` (may be empty for a seeded job)."""
    people = set(job.co_recruiter_ids or ())
    owner = owner_of(job)
    if owner is not None:
        people.add(owner)
    return people


async def assign_recruiters(
    session: AsyncSession,
    actor: Actor,
    registry: RoleRegistry,
    job: HireJob,
    *,
    owner_id: uuid.UUID,
    co_recruiter_ids: list[uuid.UUID],
) -> HireJob:
    if job.status == JobStatus.CLOSED.value:
        raise ConflictError("a closed job cannot be changed")
    co = sorted({u for u in co_recruiter_ids if u != owner_id}, key=str)
    if len(co) > MAX_CO_RECRUITERS:
        raise BadRequestError(f"at most {MAX_CO_RECRUITERS} co-recruiters")
    perms = await member_permissions(session, registry, [owner_id, *co])
    for person in (owner_id, *co):
        if CANDIDATE_READ not in perms.get(person, frozenset()):
            raise BadRequestError("every recruiter must be a member who can read candidates")
    previous = owner_of(job)
    before = {
        "owner_id": str(previous) if previous is not None else None,
        "co_recruiter_ids": [str(u) for u in job.co_recruiter_ids or ()],
    }
    job.owner_id = owner_id
    job.co_recruiter_ids = co
    job.updated_at = datetime.now(UTC)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        ASSIGNED,
        entity_type=ENTITY,
        entity_id=job.id,
        workflow=WORKFLOW,
        payload={
            "before": before,
            "owner_id": str(owner_id),
            "co_recruiter_ids": [str(u) for u in co],
        },
    )
    await session.refresh(job)
    return job
