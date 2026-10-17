# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``expense_stuck_note_sweep``: notes that never reach a person's decision.

A note is submitted and its run starts after the response (``api.submit_note``). If the
process stops before the run starts, or the run fails or stalls before it asks for an
approval, the note would stay ``processing`` with nobody told. Every five minutes the
worker runs this job (a plugin job: ``JobSpec.run``) over notes still ``processing``
``PAIRLY_EXPENSE_STUCK_MINUTES`` (default 15) after their last change:

* no run yet, never retried: start the run once more, as the submitter (only if they
  may still submit). The note counts the attempt first, so a crash never loops.
* no run after that retry, the submitter lost access, the run failed, was cancelled,
  completed without asking anyone, or stalled while running: the note moves to
  ``needs_review`` with the reason (fail closed: a person looks, nothing is approved).
* a run waiting for an approval is left alone (the approval is in the inbox).

The engine cannot see this case on its own: before the run starts there is no
``workflow_runs`` row, only the workflow's own record. So the job lives in the plugin,
on the engine's generic plugin job support.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from sqlalchemy import select, text

from pairly_core.audit import ActorType, append_audit
from pairly_core.db import set_tenant
from pairly_core.jobs.registry import JobContext
from pairly_core.workflow.models import RunStatus, WorkflowRun
from pairly_expense.graph import GRAPH
from pairly_expense.models import ExpenseNote, NoteStatus
from pairly_expense.notes import (
    ENTITY_TYPE,
    WORKFLOW,
    add_reasons,
    locked_note,
    submitter_actor,
)

log = structlog.get_logger("pairly_expense.sweeper")

JOB_NAME: Final = "expense_stuck_note_sweep"
ACTOR_ID: Final = "pairly.expense.sweeper"
STUCK_MINUTES: Final = int(os.environ.get("PAIRLY_EXPENSE_STUCK_MINUTES", "15"))
BATCH: Final = 100


async def _org_ids(ctx: JobContext, minutes: int) -> list[uuid.UUID]:
    async with ctx.sessionmaker() as session, session.begin():
        rows = await session.execute(
            text("SELECT org_id FROM expense_stuck_note_org_ids(:m) AS org_id"), {"m": minutes}
        )
        return [uuid.UUID(str(v)) for v in rows.scalars()]


async def _needs_review(
    ctx: JobContext, org_id: uuid.UUID, note_id: uuid.UUID, reason: str
) -> bool:
    session = ctx.sessionmaker()
    set_tenant(session, org_id, None)
    async with session, session.begin():
        note = await locked_note(session, note_id)
        if note.status != NoteStatus.PROCESSING.value:
            return False
        note.status = NoteStatus.NEEDS_REVIEW.value
        note.failure_reason = reason[:200]
        add_reasons(note, [reason])
        note.updated_at = datetime.now(UTC)
        await append_audit(
            session,
            action="expense.note.needs_review",
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR_ID,
            entity_type=ENTITY_TYPE,
            entity_id=note.id,
            workflow=WORKFLOW,
            payload={"reason": reason[:200]},
        )
    log.warning("expense_note_needs_review", note_id=str(note_id), reason=reason)
    return True


async def _restart(ctx: JobContext, org_id: uuid.UUID, note: ExpenseNote) -> str | None:
    """Count the attempt, then start the run once more. Returns ``None`` when the run
    started, ``""`` when the note no longer needs it, else why it could not."""
    session = ctx.sessionmaker()
    set_tenant(session, org_id, None)
    async with session, session.begin():
        row = await locked_note(session, note.id)
        if row.status != NoteStatus.PROCESSING.value or row.run_id is not None:
            return ""
        row.start_attempts += 1
        row.updated_at = datetime.now(UTC)
        await append_audit(
            session,
            action="expense.note.run_restarted",
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR_ID,
            entity_type=ENTITY_TYPE,
            entity_id=row.id,
            workflow=WORKFLOW,
            payload={"attempt": row.start_attempts},
        )
    actor = await submitter_actor(ctx.sessionmaker, ctx.registry, org_id, note.submitted_by)
    if actor is None:
        return "run_not_started:submitter_cannot_submit"
    try:
        view = await ctx.runtime.start_run(
            actor,
            workflow=WORKFLOW,
            graph=GRAPH,
            input={"note_id": str(note.id), "document_id": str(note.document_id)},
            entity_type=ENTITY_TYPE,
            entity_id=str(note.id),
        )
    except Exception as exc:
        return f"run_not_started:{type(exc).__name__}"
    if view.status == RunStatus.FAILED.value:
        return f"run_failed:{view.error or 'unknown'}"
    return None


def _run_reason(run: WorkflowRun | None, stale_before: datetime) -> str | None:
    if run is None:
        return "run_missing"
    if run.status == RunStatus.FAILED.value:
        return f"run_failed:{run.error or 'unknown'}"
    if run.status == RunStatus.CANCELLED.value:
        return "run_cancelled"
    if run.status == RunStatus.COMPLETED.value:
        return "run_completed_without_decision"
    if run.status == RunStatus.RUNNING.value and run.updated_at < stale_before:
        return "run_stalled"
    return None  # waiting for an approval, or still moving


async def sweep_org(ctx: JobContext, org_id: uuid.UUID, minutes: int) -> dict[str, int]:
    counts = {"restarted": 0, "needs_review": 0}
    stale_before = datetime.now(UTC) - timedelta(minutes=minutes)
    session = ctx.sessionmaker()
    set_tenant(session, org_id, None)
    async with session, session.begin():
        notes = list(
            await session.scalars(
                select(ExpenseNote)
                .where(
                    ExpenseNote.status == NoteStatus.PROCESSING.value,
                    ExpenseNote.updated_at < stale_before,
                )
                .order_by(ExpenseNote.updated_at)
                .limit(BATCH)
            )
        )
        runs = {
            r.id: r
            for r in await session.scalars(
                select(WorkflowRun).where(
                    WorkflowRun.id.in_([n.run_id for n in notes if n.run_id is not None])
                )
            )
        }
        for note in notes:
            session.expunge(note)
    for note in notes:
        if note.run_id is None:
            if note.start_attempts == 0:
                problem = await _restart(ctx, org_id, note)
                if problem is None:
                    counts["restarted"] += 1
                if not problem:
                    continue
                reason = problem
            else:
                reason = "run_not_started"
        else:
            found = _run_reason(runs.get(note.run_id), stale_before)
            if found is None:
                continue
            reason = found
        if await _needs_review(ctx, org_id, note.id, reason):
            counts["needs_review"] += 1
    return counts


async def sweep_stuck_notes(ctx: JobContext) -> Mapping[str, Any]:
    """The plugin job. One failing organisation never stops the others."""
    totals = {"restarted": 0, "needs_review": 0}
    errors = 0
    for org_id in await _org_ids(ctx, STUCK_MINUTES):
        try:
            counts = await sweep_org(ctx, org_id, STUCK_MINUTES)
        except Exception as exc:
            errors += 1
            log.error("expense_sweep_failed", org_id=str(org_id), error=type(exc).__name__)
            continue
        for key, value in counts.items():
            totals[key] += value
    return {**totals, "errors": errors}
