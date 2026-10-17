# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Building blocks for graph nodes that need a human decision.

The pattern (see ``tests`` for a complete graph)::

    async def propose(state, runtime):            # 1. create the approval row
        approval_id = await request_approval(runtime.context, step_key="pay", ...)
        return {"approval_id": approval_id}

    async def wait(state, runtime):               # 2. interrupt until decided
        outcome = await await_decision(runtime.context, state["approval_id"])
        if outcome is None:                       # resumed without a decision: wait again
            return Command(goto="wait")
        return Command(goto={"approved": "execute", ...}[outcome])

    async def execute(state, runtime):            # 3. the final action, via the executor
        await run_final_action(runtime.context, action="pay", ...)

Creating the approval and waiting for it are separate nodes on purpose: LangGraph runs a
node again from the start when it is resumed, and the approval must be created once
(``create_approval`` is idempotent per run, step key and revision as well).

``await_decision`` ignores the resume value: it reads the approval from the database.
So even code that resumes the graph directly with a forged value gets ``None`` back and
the run goes on waiting.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from langgraph.types import interrupt

from pairly_core.approvals.executor import (
    ExecutorRefusedError,
    execute_final_action,
    record_refusal,
)
from pairly_core.approvals.models import DECIDED_STATUSES, Approval
from pairly_core.approvals.service import ApprovalRequest, create_approval
from pairly_core.audit import ActorType
from pairly_core.workflow.context import WorkflowContext
from pairly_core.workflow.spec import SpecError

INTERRUPT_KIND = "pairly.approval"


async def request_approval(
    ctx: WorkflowContext,
    *,
    step_key: str,
    approval_type: str,
    title: str,
    payload: Mapping[str, Any],
    context: Mapping[str, Any] | None = None,
    revision: int = 1,
    entity_type: str | None = None,
    entity_id: str | None = None,
    preparer_ids: Sequence[uuid.UUID] = (),
) -> str:
    """Create (once) the approval for this run step and return its id as a string.

    The run's starter is the submitter. When the starter is a person, they are also a
    preparer, so they can never approve their own proposal.
    """
    kind = ctx.graph.approval_type(approval_type)
    if kind is None:
        raise SpecError(f"graph {ctx.graph.name!r} does not declare {approval_type!r}")
    preparers = list(preparer_ids)
    if ctx.started_by_type is ActorType.USER:
        preparers.append(uuid.UUID(ctx.started_by_id))
    async with ctx.transaction() as session:
        approval = await create_approval(
            session,
            ctx.registry,
            ApprovalRequest(
                run_id=ctx.run_id,
                step_key=step_key,
                approval_type=kind,
                title=title,
                payload=payload,
                context=context or {},
                revision=revision,
                entity_type=entity_type,
                entity_id=entity_id,
                submitted_by_type=ctx.started_by_type,
                submitted_by_id=ctx.started_by_id,
                preparer_ids=preparers,
            ),
            hooks=ctx.hooks,
        )
        return str(approval.id)


async def await_decision(ctx: WorkflowContext, approval_id: str) -> str | None:
    """Interrupt until resumed, then return the approval's decided status from the
    database (``approved``, ``rejected``, ``changes_requested``), or ``None`` when it
    is not decided (a resume without a decision)."""
    interrupt({"kind": INTERRUPT_KIND, "approval_id": approval_id})
    async with ctx.transaction() as session:
        approval = await session.get(Approval, uuid.UUID(approval_id))
        if approval is None or approval.run_id != ctx.run_id:
            return None
        return approval.status if approval.status in DECIDED_STATUSES else None


async def run_final_action(
    ctx: WorkflowContext,
    *,
    action: str,
    approval_id: str,
    step_key: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Run a final action through the executor (re-checks everything; fail closed)."""
    approval_uuid = uuid.UUID(approval_id)
    try:
        async with ctx.transaction() as session:
            return await execute_final_action(
                session,
                ctx.registry,
                ctx.graph,
                action=action,
                org_id=ctx.org_id,
                run_id=ctx.run_id,
                step_key=step_key,
                approval_id=approval_uuid,
                payload=payload,
                hooks=ctx.hooks,
            )
    except ExecutorRefusedError as exc:
        async with ctx.transaction() as session:
            await record_refusal(
                session,
                workflow=ctx.workflow,
                run_id=ctx.run_id,
                approval_id=approval_uuid,
                action=action,
                reason=exc.reason,
            )
        raise
