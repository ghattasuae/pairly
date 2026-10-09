# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Jobs: create, edit, publish, pause, close. Every write records an audit event.

Status (``JobStatus``)::

    draft --publish--> open --pause--> paused --publish--> open
    draft | open | paused --close--> closed   (final)

Publishing needs a locked rubric (``pairly_hire.rubrics``), an English description, and
an application form version (the default form is published on first publish if the job
has none), so every application records the exact rubric and form it arrived under.
Editing an open job's text is allowed (typos happen) and audited; the rubric and form
change only through their own versioned routes.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit, audit_actor_action
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.policy_versions import PublishedPolicy, get_policy, publish_policy_version
from pairly_core.rbac import Actor
from pairly_hire.forms import DEFAULT_FORM, ApplicationForm, form_validator, parse_form
from pairly_hire.forms import POLICY_KIND as FORM_KIND
from pairly_hire.models import (
    DEFAULT_RESPONSE_DAYS,
    DEFAULT_RETENTION_MONTHS,
    HireJob,
    HireOrgSettings,
    JobStatus,
)
from pairly_hire.rubric import policy_name

WORKFLOW: Final = "hire"
ENTITY: Final = "hire.job"
CREATED: Final = "hire.job.created"
UPDATED: Final = "hire.job.updated"
PUBLISHED: Final = "hire.job.published"
PAUSED: Final = "hire.job.paused"
CLOSED: Final = "hire.job.closed"
FORM_SAVED: Final = "hire.form.saved"

# Fields a person may set on a job (create and update).
EDITABLE: Final = (
    "title",
    "title_ar",
    "team",
    "location",
    "country",
    "work_model",
    "employment_type",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "show_salary",
    "description_en",
    "description_ar",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", folded.lower()).strip("-")[:60].strip("-")
    return slug or "job"


async def unique_slug(session: AsyncSession, title: str) -> str:
    base = slugify(title)
    candidate = base
    for _ in range(20):
        taken = await session.scalar(select(HireJob.id).where(HireJob.slug == candidate))
        if taken is None:
            return candidate
        candidate = f"{base}-{secrets.token_hex(2)}"
    raise ConflictError("could not find a free address for this job")  # pragma: no cover


def check_salary(values: dict[str, Any]) -> None:
    low, high = values.get("salary_min"), values.get("salary_max")
    if isinstance(low, Decimal) and isinstance(high, Decimal) and low > high:
        raise BadRequestError("the lowest salary is above the highest")
    if values.get("show_salary") and not all(
        values.get(k) is not None
        for k in ("salary_min", "salary_max", "salary_currency", "salary_period")
    ):
        raise BadRequestError("to show the salary, give a range, a currency and a period")


async def get_job(session: AsyncSession, job_id: uuid.UUID, *, lock: bool = False) -> HireJob:
    query = select(HireJob).where(HireJob.id == job_id)
    if lock:
        query = query.with_for_update()
    job = await session.scalar(query)
    if job is None:
        raise NotFoundError("job not found")
    return job


async def create_job(session: AsyncSession, actor: Actor, values: dict[str, Any]) -> HireJob:
    check_salary(values)
    job = HireJob(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        slug=await unique_slug(session, str(values["title"])),
        status=JobStatus.DRAFT.value,
        created_by=actor.id,
        **{k: values[k] for k in EDITABLE if k in values},
    )
    session.add(job)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        CREATED,
        entity_type=ENTITY,
        entity_id=job.id,
        workflow=WORKFLOW,
        payload={"slug": job.slug},
    )
    await session.refresh(job)
    return job


async def update_job(
    session: AsyncSession, actor: Actor, job: HireJob, changes: dict[str, Any]
) -> HireJob:
    if job.status == JobStatus.CLOSED.value:
        raise ConflictError("a closed job cannot be changed")
    merged = {k: getattr(job, k) for k in EDITABLE} | changes
    check_salary(merged)
    if job.status == JobStatus.OPEN.value and not str(merged.get("description_en") or "").strip():
        raise BadRequestError("an open job needs an English description")
    for key, value in changes.items():
        if key in EDITABLE:
            setattr(job, key, value)
    job.updated_at = datetime.now(UTC)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        UPDATED,
        entity_type=ENTITY,
        entity_id=job.id,
        workflow=WORKFLOW,
        payload={"fields": sorted(k for k in changes if k in EDITABLE)},
    )
    await session.refresh(job)
    return job


# ------------------------------------------------------------------ forms


async def current_form(session: AsyncSession, job: HireJob) -> tuple[ApplicationForm, int | None]:
    """The job's form and its version (``None``: the default, not published yet)."""
    if job.form_policy_id is None:
        return DEFAULT_FORM, None
    policy = await get_policy(session, job.form_policy_id)
    if policy is None or policy.kind != FORM_KIND:  # pragma: no cover - RESTRICT foreign key
        raise ConflictError("the job's form version is missing")
    return parse_form(policy.content), policy.version


async def save_form(
    session: AsyncSession, actor: Actor, job: HireJob, form: ApplicationForm
) -> PublishedPolicy:
    """Publish ``form`` as the job's next form version (no new version if unchanged)."""
    if job.status == JobStatus.CLOSED.value:
        raise ConflictError("a closed job cannot be changed")
    policy = await publish_policy_version(
        session,
        kind=FORM_KIND,
        name=policy_name(job.id.hex),
        content=form.to_yaml(),
        validate=form_validator(),
        actor_type=ActorType.USER,
        actor_id=str(actor.id),
    )
    if job.form_policy_id != policy.id:
        job.form_policy_id = policy.id
        job.updated_at = datetime.now(UTC)
        await session.flush()
    await audit_actor_action(
        session,
        actor,
        FORM_SAVED,
        entity_type=ENTITY,
        entity_id=job.id,
        workflow=WORKFLOW,
        payload={"policy_version_id": str(policy.id), "version": policy.version},
    )
    return policy


# ------------------------------------------------------------------ status


@dataclass(frozen=True, slots=True)
class Transition:
    action: str
    allowed_from: frozenset[str]
    to: str


PUBLISH: Final = Transition(
    PUBLISHED, frozenset({JobStatus.DRAFT.value, JobStatus.PAUSED.value}), JobStatus.OPEN.value
)
PAUSE: Final = Transition(PAUSED, frozenset({JobStatus.OPEN.value}), JobStatus.PAUSED.value)
CLOSE: Final = Transition(
    CLOSED,
    frozenset({JobStatus.DRAFT.value, JobStatus.OPEN.value, JobStatus.PAUSED.value}),
    JobStatus.CLOSED.value,
)


async def change_status(
    session: AsyncSession, actor: Actor, job: HireJob, transition: Transition
) -> HireJob:
    if job.status not in transition.allowed_from:
        raise ConflictError(f"a {job.status} job cannot be {transition.to}")
    if transition is PUBLISH:
        if job.rubric_policy_id is None:
            raise ConflictError("lock the rubric before you publish the job")
        if not job.description_en.strip():
            raise ConflictError("write the English description before you publish the job")
        if job.form_policy_id is None:
            await save_form(session, actor, job, DEFAULT_FORM)
    now = datetime.now(UTC)
    job.status = transition.to
    job.updated_at = now
    if transition is PUBLISH and job.published_at is None:
        job.published_at = now
    if transition is CLOSE:
        job.closed_at = now
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        transition.action,
        entity_type=ENTITY,
        entity_id=job.id,
        workflow=WORKFLOW,
        payload={"status": job.status},
    )
    await session.refresh(job)
    return job


# ------------------------------------------------------------------ settings


@dataclass(frozen=True, slots=True)
class Settings:
    careers_enabled: bool
    rejected_retention_months: int
    response_days: int


async def org_settings(session: AsyncSession) -> Settings:
    row = await session.scalar(select(HireOrgSettings))
    if row is None:
        return Settings(True, DEFAULT_RETENTION_MONTHS, DEFAULT_RESPONSE_DAYS)
    return Settings(row.careers_enabled, row.rejected_retention_months, row.response_days)


async def save_settings(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_type: ActorType,
    actor_id: str,
    user_id: uuid.UUID | None,
    changes: dict[str, Any],
) -> Settings:
    row = await session.scalar(select(HireOrgSettings).with_for_update())
    if row is None:
        row = HireOrgSettings(id=uuid.uuid4(), org_id=org_id)
        session.add(row)
    for key in ("careers_enabled", "rejected_retention_months", "response_days"):
        if key in changes and changes[key] is not None:
            setattr(row, key, changes[key])
    row.updated_by = user_id
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await append_audit(
        session,
        action="hire.settings.changed",
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="hire.settings",
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={k: changes[k] for k in sorted(changes) if changes[k] is not None},
    )
    return Settings(row.careers_enabled, row.rejected_retention_months, row.response_days)
