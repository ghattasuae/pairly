# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The rubric builder: AI draft, a person's edits, and the lock (brief 3.1.4).

* ``save_draft``: store a draft (from the AI or a person) as canonical YAML. A draft is
  never used for scoring.
* ``lock_rubric``: a person with ``hire.rubric.lock`` (second factor) locks the current
  draft. Locking publishes it as an immutable policy version (kind ``hire_rubric``,
  name ``hire.job.<job id hex>``; ``pairly_core.policy_versions`` refuses UPDATE and
  DELETE for every role) and points the job at it. Locking an unchanged draft again is
  a no-op. Locking a changed draft after applications exist creates a new version:
  existing applications keep the version they arrived under (``rubric_policy_id``) and
  are never rescored silently; the result says how many applications stay on earlier
  versions so the UI can say so.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, audit_actor_action
from pairly_core.errors import ConflictError
from pairly_core.policy_versions import (
    PublishedPolicy,
    get_policy,
    list_policy_versions,
    publish_policy_version,
)
from pairly_core.rbac import Actor
from pairly_hire.models import (
    ApplicationStatus,
    HireApplication,
    HireJob,
    HireRubricDraft,
    JobStatus,
)
from pairly_hire.rubric import POLICY_KIND, Rubric, parse_rubric, policy_name, rubric_validator

ENTITY: Final = "hire.job"
WORKFLOW: Final = "hire"
DRAFT_SAVED: Final = "hire.rubric.draft_saved"
LOCKED: Final = "hire.rubric.locked"


async def get_draft(session: AsyncSession, job_id: uuid.UUID) -> HireRubricDraft | None:
    return await session.scalar(select(HireRubricDraft).where(HireRubricDraft.job_id == job_id))


async def save_draft(
    session: AsyncSession,
    actor: Actor,
    job: HireJob,
    rubric: Rubric,
    *,
    source: str,
    model_call_id: uuid.UUID | None = None,
) -> HireRubricDraft:
    if job.status == JobStatus.CLOSED.value:
        raise ConflictError("a closed job cannot be changed")
    content = rubric.to_yaml()
    draft = await get_draft(session, job.id)
    now = datetime.now(UTC)
    if draft is None:
        draft = HireRubricDraft(
            id=uuid.uuid4(),
            org_id=job.org_id,
            job_id=job.id,
            content=content,
            source=source,
            model_call_id=model_call_id,
            updated_by=actor.id,
        )
        session.add(draft)
    else:
        draft.content = content
        draft.source = source
        draft.model_call_id = model_call_id
        draft.updated_by = actor.id
        draft.updated_at = now
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        DRAFT_SAVED,
        entity_type=ENTITY,
        entity_id=job.id,
        workflow=WORKFLOW,
        payload={
            "source": source,
            "model_call_id": str(model_call_id) if model_call_id else None,
        },
    )
    await session.refresh(draft)
    return draft


@dataclass(frozen=True, slots=True)
class LockResult:
    policy: PublishedPolicy
    new_version: bool
    # Submitted applications of this job that stay on an earlier rubric version.
    applications_on_earlier_versions: int


async def lock_rubric(session: AsyncSession, actor: Actor, job: HireJob) -> LockResult:
    """Lock the job's current draft. The caller checked ``hire.rubric.lock``."""
    if job.status == JobStatus.CLOSED.value:
        raise ConflictError("a closed job cannot be changed")
    draft = await get_draft(session, job.id)
    if draft is None:
        raise ConflictError("there is no rubric draft to lock")
    previous = job.rubric_policy_id
    policy = await publish_policy_version(
        session,
        kind=POLICY_KIND,
        name=policy_name(job.id.hex),
        content=draft.content,
        validate=rubric_validator(),
        actor_type=ActorType.USER,
        actor_id=str(actor.id),
    )
    new_version = policy.id != previous
    if new_version:
        job.rubric_policy_id = policy.id
        job.updated_at = datetime.now(UTC)
        await session.flush()
    earlier = int(
        await session.scalar(
            select(func.count())
            .select_from(HireApplication)
            .where(
                HireApplication.job_id == job.id,
                HireApplication.rubric_policy_id != policy.id,
                HireApplication.status != ApplicationStatus.INCOMPLETE.value,
            )
        )
        or 0
    )
    await audit_actor_action(
        session,
        actor,
        LOCKED,
        entity_type=ENTITY,
        entity_id=job.id,
        workflow=WORKFLOW,
        payload={
            "policy_version_id": str(policy.id),
            "version": policy.version,
            "content_hash": policy.content_hash,
            "new_version": new_version,
            "applications_on_earlier_versions": earlier,
        },
    )
    return LockResult(policy, new_version, earlier)


async def locked_rubric(session: AsyncSession, job: HireJob) -> tuple[PublishedPolicy, Rubric]:
    """The job's current locked rubric, or ``ConflictError`` when there is none."""
    if job.rubric_policy_id is None:
        raise ConflictError("the job has no locked rubric")
    policy = await get_policy(session, job.rubric_policy_id)
    if policy is None or policy.kind != POLICY_KIND:  # pragma: no cover - RESTRICT key
        raise ConflictError("the job's rubric version is missing")
    return policy, parse_rubric(policy.content)


async def rubric_versions(session: AsyncSession, job: HireJob) -> list[PublishedPolicy]:
    return await list_policy_versions(session, POLICY_KIND, policy_name(job.id.hex))
