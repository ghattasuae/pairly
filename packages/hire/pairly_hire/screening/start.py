# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Starting a screening: a ``hire_screenings`` row, then a ``hire_screening`` run.

Runs always start as the screening agent (``agent_actor``: the ``agent`` role, which
holds ``hire.screening.propose`` and nothing that decides). Three ways in:

* the submit route, after the response, when this process can drive the CV through
  the scan and the parse itself or the CV is already parsed (``start_after_submit``);
* the worker job ``hire_screening_start`` every minute, for submitted applications
  that have no screening yet (the API process could not start it, or stopped first).
  It finds the organisations through ``hire_screening_pending_org_ids(minutes)``, a
  SECURITY DEFINER function that returns organisation ids only (migration hire_0002),
  then works in each organisation under Row Level Security;
* a person asking for a new screening (``POST .../screening/run``), for example after
  "changes requested" or a failed run.

An application has at most one screening that is processing or awaiting review: a
start while one is active returns that one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.db import set_tenant
from pairly_core.ingestion.models import Document, DocumentStatus
from pairly_core.jobs.registry import JobContext, JobSpec
from pairly_core.rbac import agent_actor
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_hire.models import ApplicationStatus, HireApplication
from pairly_hire.screening.graph import ACTOR_ID, GRAPH
from pairly_hire.screening.models import HireScreening, ScreeningStatus
from pairly_hire.screening.service import SCREENING_ENTITY, WORKFLOW, screening_settings

log = structlog.get_logger("pairly_hire.screening.start")

# The screening agent's fixed id (an agent, never a person).
AGENT_ID: Final = uuid.UUID("6a0d5c2e-7d4f-5b1a-9e3c-2f8b1d4a6c01")
JOB_NAME: Final = "hire_screening_start"
ACTIVE: Final = frozenset({ScreeningStatus.PROCESSING.value, ScreeningStatus.AWAITING_REVIEW.value})
# Documents whose scan and parse are over (parsed or not): the run can go on at once.
SETTLED: Final = frozenset(
    {
        DocumentStatus.PARSED.value,
        DocumentStatus.PARSE_FAILED.value,
        DocumentStatus.INFECTED.value,
        DocumentStatus.REJECTED.value,
    }
)
# The worker starts a screening anyway (to fail closed in the run) after this long.
PATIENCE: Final = timedelta(minutes=10)
BATCH: Final = 50


class StartRefusedError(ValueError):
    """The application cannot be screened (not submitted, no CV)."""


async def active_screening(
    session: AsyncSession, application_id: uuid.UUID
) -> HireScreening | None:
    return await session.scalar(
        select(HireScreening)
        .where(
            HireScreening.application_id == application_id,
            HireScreening.status.in_(list(ACTIVE)),
        )
        .order_by(HireScreening.created_at.desc())
        .limit(1)
    )


async def create_screening(
    session: AsyncSession, application_id: uuid.UUID, *, requested_by: tuple[ActorType, str]
) -> tuple[HireScreening, bool]:
    """The application's active screening, or a new one (``True`` when created)."""
    app = await session.scalar(
        select(HireApplication).where(HireApplication.id == application_id).with_for_update()
    )
    if app is None or app.cv_document_id is None or app.submitted_at is None:
        raise StartRefusedError("the application is not submitted")
    if app.status != ApplicationStatus.RECEIVED.value:
        raise StartRefusedError("the application is closed")
    existing = await active_screening(session, application_id)
    if existing is not None:
        return existing, False
    config = await screening_settings(session)
    row = HireScreening(
        id=uuid.uuid4(),
        org_id=app.org_id,
        application_id=app.id,
        job_id=app.job_id,
        document_id=app.cv_document_id,
        rubric_policy_id=app.rubric_policy_id,
        status=ScreeningStatus.PROCESSING.value,
        blind=config.blind.enabled,
        blind_config=config.blind.as_dict(),
        review_reasons=[],
        removed_spans=[],
        blind_spans=[],
        model_info={},
        flag_codes=[],
        decision_ids=[],
    )
    session.add(row)
    await session.flush()
    kind, actor = requested_by
    await append_audit(
        session,
        action="hire.screening.created",
        actor_type=kind,
        actor_id=actor,
        entity_type=SCREENING_ENTITY,
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={"application_id": str(app.id), "rubric_policy_id": str(app.rubric_policy_id)},
    )
    return row, True


async def start_run(runtime: WorkflowRuntime, row: HireScreening) -> str | None:
    """Start the run for a new screening. Returns the run status, ``None`` on failure
    (the screening stays ``processing`` with no run, visible to reviewers)."""
    actor = agent_actor(runtime.registry, row.org_id, AGENT_ID)
    try:
        view = await runtime.start_run(
            actor,
            workflow=WORKFLOW,
            graph=GRAPH,
            input={
                "screening_id": str(row.id),
                "application_id": str(row.application_id),
                "document_id": str(row.document_id),
            },
            entity_type=SCREENING_ENTITY,
            entity_id=str(row.id),
        )
    except Exception as exc:
        log.error("hire_screening_not_started", screening_id=str(row.id), error=type(exc).__name__)
        return None
    return view.status


async def start_screening(
    runtime: WorkflowRuntime,
    org_id: uuid.UUID,
    application_id: uuid.UUID,
    *,
    requested_by: tuple[ActorType, str] = (ActorType.SYSTEM, ACTOR_ID),
) -> HireScreening | None:
    """Create (or find) the screening and start its run. Never raises."""
    session = runtime.sessionmaker()
    set_tenant(session, org_id, None)
    try:
        async with session, session.begin():
            row, created = await create_screening(
                session, application_id, requested_by=requested_by
            )
            session.expunge(row)
    except StartRefusedError:
        return None
    except Exception as exc:
        log.error("hire_screening_not_created", error=type(exc).__name__)
        return None
    if created:
        await start_run(runtime, row)
    return row


async def start_after_submit(
    runtime: WorkflowRuntime, org_id: uuid.UUID, application_id: uuid.UUID
) -> None:
    """The submit route's background task. Starts only when the run will not sit
    waiting for a worker: this process scans and parses, or the CV is settled.
    Otherwise the worker job starts it once the CV is parsed."""
    services = runtime.services
    can_drive = (
        services.store is not None
        and services.scanner is not None
        and services.parse_config is not None
    )
    if not can_drive:
        session = runtime.sessionmaker()
        set_tenant(session, org_id, None)
        async with session, session.begin():
            status = await session.scalar(
                select(Document.status)
                .join(HireApplication, HireApplication.cv_document_id == Document.id)
                .where(HireApplication.id == application_id)
            )
        if status not in SETTLED:
            return
    await start_screening(runtime, org_id, application_id)


# ------------------------------------------------------------------ the worker job


async def _org_ids(ctx: JobContext) -> list[uuid.UUID]:
    async with ctx.sessionmaker() as session, session.begin():
        rows = await session.execute(
            text("SELECT org_id FROM hire_screening_pending_org_ids() AS org_id")
        )
        return [uuid.UUID(str(v)) for v in rows.scalars()]


async def start_pending_org(ctx: JobContext, org_id: uuid.UUID) -> int:
    patience = datetime.now(UTC) - PATIENCE
    session = ctx.sessionmaker()
    set_tenant(session, org_id, None)
    async with session, session.begin():
        rows = await session.execute(
            select(HireApplication.id, HireApplication.submitted_at, Document.status)
            .join(Document, Document.id == HireApplication.cv_document_id)
            .where(
                HireApplication.status == ApplicationStatus.RECEIVED.value,
                HireApplication.submitted_at.is_not(None),
                ~select(HireScreening.id)
                .where(HireScreening.application_id == HireApplication.id)
                .exists(),
            )
            .order_by(HireApplication.submitted_at)
            .limit(BATCH)
        )
        pending = [
            r.id
            for r in rows
            if r.status in SETTLED or (r.submitted_at is not None and r.submitted_at < patience)
        ]
    started = 0
    for application_id in pending:
        if await start_screening(ctx.runtime, org_id, application_id) is not None:
            started += 1
    return started


async def start_pending(ctx: JobContext) -> dict[str, int]:
    """The plugin job. One failing organisation never stops the others."""
    started = errors = 0
    for org_id in await _org_ids(ctx):
        try:
            started += await start_pending_org(ctx, org_id)
        except Exception as exc:
            errors += 1
            log.error("hire_screening_start_failed", org_id=str(org_id), error=type(exc).__name__)
    return {"started": started, "errors": errors}


SCHEDULED_JOBS: Final[tuple[JobSpec, ...]] = (
    JobSpec(
        JOB_NAME,
        "Start the screening of submitted applications whose CV is ready",
        "every minute",
        run=start_pending,
        every_minutes=1,
    ),
)
