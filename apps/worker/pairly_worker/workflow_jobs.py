# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Workflow and approval jobs (WP1.8).

* ``escalate_overdue_approvals`` (cron, every 5 minutes): escalates overdue approval
  steps (``pairly_core.approvals.sla``).
* ``resume_decided_runs`` (cron, every minute): reconciliation. Resumes runs that wait
  for an approval which already has its human decision (the API resumes right after a
  decision; this catches a process that stopped in between). Resuming still goes through
  ``WorkflowRuntime.resume_run`` and all its checks.

Organisations are found through the SECURITY DEFINER functions of migration 0006, which
return only organisation ids; each organisation is then processed in its own
tenant-bound session as the application role. One failing organisation never stops the
others.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.approvals.hooks import ApprovalHooks
from pairly_core.approvals.sla import escalate_overdue
from pairly_core.db import set_tenant
from pairly_core.rbac import RoleRegistry
from pairly_core.workflow.runtime import WorkflowRuntime

log = structlog.get_logger("pairly_worker.workflow_jobs")


async def _org_ids(
    sessionmaker: async_sessionmaker[AsyncSession], function: str
) -> list[uuid.UUID]:
    if function not in {"approval_overdue_org_ids", "workflow_resumable_org_ids"}:
        raise ValueError("unknown org listing function")
    async with sessionmaker() as session, session.begin():
        rows = await session.execute(text(f"SELECT org_id FROM {function}() AS org_id"))  # noqa: S608  # nosec B608 (fixed allowlist above)
        return [uuid.UUID(str(value)) for value in rows.scalars()]


async def escalate_overdue_approvals(ctx: dict[str, Any]) -> dict[str, Any]:
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    registry: RoleRegistry = ctx["registry"]
    hooks: ApprovalHooks = ctx["runtime"].hooks
    escalated = 0
    errors: list[str] = []
    for org_id in await _org_ids(sessionmaker, "approval_overdue_org_ids"):
        try:
            async with sessionmaker() as session:
                set_tenant(session, org_id, None)
                async with session.begin():
                    escalated += len(await escalate_overdue(session, registry, hooks=hooks))
        except Exception as exc:
            errors.append(str(org_id))
            log.error("approval_escalation_failed", org_id=str(org_id), error=type(exc).__name__)
    summary = {"escalated": escalated, "errors": errors}
    log.info("approval_escalation_finished", **summary)
    return summary


async def resume_decided_runs(ctx: dict[str, Any]) -> dict[str, Any]:
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    runtime: WorkflowRuntime = ctx["runtime"]
    resumed = 0
    errors: list[str] = []
    for org_id in await _org_ids(sessionmaker, "workflow_resumable_org_ids"):
        try:
            resumed += await runtime.resume_decided_runs(org_id)
        except Exception as exc:
            errors.append(str(org_id))
            log.error("workflow_resume_failed", org_id=str(org_id), error=type(exc).__name__)
    summary = {"resumed": resumed, "errors": errors}
    if resumed or errors:
        log.info("workflow_resume_finished", **summary)
    return summary
