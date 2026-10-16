# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The ``hire_requisition`` graph (version 1): a requested role waits for its approvals.

::

    propose -> wait --approved--> execute -> END
                    --rejected--> declined -> END
                    --changes_requested--> changes -> END
                    --(no decision)--> wait

* ``propose``  creates the ``hire.requisition`` approval. Its payload is the exact
               request (title, team, budget, headcount, location, work model, start
               date), so the approval covers what the approvers saw. The chain comes
               from ``approval_rules.yaml``: HR always, finance too above the budget
               threshold. The requester started the run, so the engine makes them a
               preparer: they can never approve their own request.
* ``wait``     interrupts until the chain is decided (read from the database).
* ``execute``  the ``approve_requisition`` final action, through the executor (it
               re-checks the approval and the payload hash): the requisition becomes
               ``approved`` and a job may be opened from it.
* ``declined`` / ``changes``: the requisition says why; no job can be opened.

There is no edge from ``propose`` to ``execute`` that skips ``wait``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.executor import ExecutorRefusedError
from pairly_core.policy_versions import PolicyError
from pairly_core.workflow.context import WorkflowContext
from pairly_core.workflow.nodes import await_decision, request_approval, run_final_action
from pairly_core.workflow.runtime import node_retry
from pairly_core.workflow.spec import FinalActionCall
from pairly_hire.requisitions.models import HireRequisition, RequisitionStatus
from pairly_hire.requisitions.service import audit_requisition, locked, payload_of

GRAPH: Final = "hire_requisition"
VERSION: Final = 1
STEP_KEY: Final = "approve"
APPROVAL_TYPE: Final = "hire.requisition"
FINAL_ACTION: Final = "approve_requisition"
ENTITY: Final = "hire.requisition"

APPROVAL_RULES_V1: Final = (Path(__file__).resolve().parent / "approval_rules.yaml").read_text(
    encoding="utf-8"
)


class RequisitionState(TypedDict, total=False):
    requisition_id: str
    approval_id: str
    payload: dict[str, Any]
    outcome: str


Ctx = Runtime[WorkflowContext]


async def propose(state: RequisitionState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    async with ctx.transaction() as session:
        row = await locked(session, state["requisition_id"])
        if row.status != RequisitionStatus.PENDING_APPROVAL.value or row.approval_id is not None:
            return Command(goto=END)
        payload = payload_of(row)
        context = {"kind": ENTITY, **payload, "justification": row.justification}
        title = f"New role: {row.title}, {row.team} ({row.reference})"
    try:
        approval_id = await request_approval(
            ctx,
            step_key=STEP_KEY,
            approval_type=APPROVAL_TYPE,
            title=title,
            payload=payload,
            context=context,
            entity_type=ENTITY,
            entity_id=state["requisition_id"],
        )
    except PolicyError as exc:
        # Nobody may approve it: the requisition says so (fail closed, no job).
        async with ctx.transaction() as session:
            row = await locked(session, state["requisition_id"])
            row.status = RequisitionStatus.FAILED.value
            row.status_reason = f"approval_not_possible:{type(exc).__name__}"[:64]
            row.updated_at = datetime.now(UTC)
            await audit_requisition(session, None, "hire.requisition.failed", row)
        return Command(goto=END)
    async with ctx.transaction() as session:
        row = await locked(session, state["requisition_id"])
        row.approval_id = uuid.UUID(approval_id)
        row.run_id = ctx.run_id
        row.updated_at = datetime.now(UTC)
        await audit_requisition(
            session, None, "hire.requisition.awaiting_approval", row, {"approval_id": approval_id}
        )
    return Command(goto="wait", update={"approval_id": approval_id, "payload": payload})


async def wait(state: RequisitionState, runtime: Ctx) -> Command[Any]:
    outcome = await await_decision(runtime.context, state["approval_id"])
    if outcome is None:
        return Command(goto="wait")
    target = {"approved": "execute", "rejected": "declined", "changes_requested": "changes"}
    return Command(goto=target[outcome], update={"outcome": outcome})


async def approve_requisition(session: AsyncSession, call: FinalActionCall) -> Mapping[str, Any]:
    """The final action, inside the executor's transaction after it re-checked the
    approval and the payload hash."""
    row = await session.scalar(
        select(HireRequisition)
        .where(HireRequisition.approval_id == call.approval_id)
        .with_for_update()
    )
    if row is None:
        raise ExecutorRefusedError("requisition_not_found")
    if row.status != RequisitionStatus.PENDING_APPROVAL.value:
        raise ExecutorRefusedError("requisition_not_pending")
    if payload_of(row) != dict(call.payload):
        raise ExecutorRefusedError("requisition_changed")
    now = datetime.now(UTC)
    row.status = RequisitionStatus.APPROVED.value
    row.status_reason = None
    row.decided_at = now
    row.updated_at = now
    await audit_requisition(
        session,
        None,
        "hire.requisition.approved",
        row,
        {
            "approval_id": str(call.approval_id),
            "approvers": [str(a) for a in call.approver_ids],
        },
    )
    return {"requisition_id": str(row.id), "status": row.status}


async def execute(state: RequisitionState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    try:
        await run_final_action(
            ctx,
            action=FINAL_ACTION,
            approval_id=state["approval_id"],
            step_key=STEP_KEY,
            payload=state["payload"],
        )
    except ExecutorRefusedError as exc:
        async with ctx.transaction() as session:
            row = await locked(session, state["requisition_id"])
            if row.status == RequisitionStatus.PENDING_APPROVAL.value:
                row.status = RequisitionStatus.FAILED.value
                row.status_reason = f"refused:{exc.reason}"[:64]
                row.updated_at = datetime.now(UTC)
                await audit_requisition(session, None, "hire.requisition.failed", row)
        raise
    return {}


async def _close(ctx: WorkflowContext, state: RequisitionState, status: RequisitionStatus) -> None:
    async with ctx.transaction() as session:
        row = await locked(session, state["requisition_id"])
        if row.status != RequisitionStatus.PENDING_APPROVAL.value:
            return
        now = datetime.now(UTC)
        row.status = status.value
        row.status_reason = state.get("outcome")
        row.decided_at = now
        row.updated_at = now
        await audit_requisition(
            session,
            None,
            f"hire.requisition.{status.value}",
            row,
            {"approval_id": state.get("approval_id")},
        )


async def declined(state: RequisitionState, runtime: Ctx) -> dict[str, Any]:
    await _close(runtime.context, state, RequisitionStatus.REJECTED)
    return {}


async def changes(state: RequisitionState, runtime: Ctx) -> dict[str, Any]:
    await _close(runtime.context, state, RequisitionStatus.CHANGES_REQUESTED)
    return {}


def build_graph() -> StateGraph[Any, Any, Any, Any]:
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(
        RequisitionState, context_schema=WorkflowContext
    )
    retry = node_retry()
    graph.add_node("propose", propose, retry_policy=retry, timeout=60, destinations=("wait", END))
    graph.add_node("wait", wait, destinations=("wait", "execute", "declined", "changes"))
    graph.add_node("execute", execute, retry_policy=retry, timeout=60)
    graph.add_node("declined", declined, retry_policy=retry, timeout=30)
    graph.add_node("changes", changes, retry_policy=retry, timeout=30)
    graph.add_edge(START, "propose")
    graph.add_edge("execute", END)
    graph.add_edge("declined", END)
    graph.add_edge("changes", END)
    return graph
