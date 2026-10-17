# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The ``pay_payment_run`` graph (version 1): a payment run waits for a finance
controller.

::

    propose -> wait --approved--> execute -> END
                    --rejected | changes_requested--> decided -> END
                    --(no decision)--> wait

Started by the person who proposed the run (``pay.payment_run.create``), so the engine
makes them a preparer: they never approve their own run.

* ``propose``  checks every item again and asks for a ``pay.payment_run`` approval whose
               payload binds the digest of every item (invoice, amount, bank account,
               keyed details digest, check digest). Anything wrong fails the run and
               releases its invoices (fail closed).
* ``wait``     interrupts until decided (read from the database).
* ``execute``  the ``approve_payment_run`` final action through the executor. A refusal
               fails the run and releases its invoices.
* ``decided``  a rejection or a change request releases the invoices.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command

from pairly_core.approvals.executor import ExecutorRefusedError
from pairly_core.policy_versions import PolicyError
from pairly_core.workflow.context import WorkflowContext
from pairly_core.workflow.nodes import await_decision, request_approval, run_final_action
from pairly_core.workflow.runtime import node_retry
from pairly_pay.common import By, audit
from pairly_pay.models import PaymentRunStatus
from pairly_pay.payment_runs import (
    APPROVAL_TYPE,
    FINAL_ACTION,
    RUN_ENTITY,
    STEP_KEY,
    approval_context,
    approval_payload,
    close_after_decision,
    fail_run,
    get_run,
    item_problems,
    items_of,
    refresh_run_decisions,
)

__all__ = ["build_graph"]


class RunState(TypedDict, total=False):
    payment_run_id: str
    approval_id: str
    payload: dict[str, Any]
    outcome: str


Ctx = Runtime[WorkflowContext]


async def _fail(ctx: WorkflowContext, run_id: uuid.UUID, reason: str) -> None:
    async with ctx.transaction() as session:
        await fail_run(session, run_id, reason)


async def propose(state: RunState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    run_id = uuid.UUID(state["payment_run_id"])
    async with ctx.transaction() as session:
        run = await get_run(session, run_id, lock=True)
        if run.status != PaymentRunStatus.AWAITING_APPROVAL.value:
            return Command(goto=END)
        if run.run_id is None:
            run.run_id = ctx.run_id
        elif run.run_id != ctx.run_id:
            return Command(goto=END)
        items = await items_of(session, run.id, active_only=True)
        for item in items:
            problems = await item_problems(session, run, item)
            if problems:
                await fail_run(session, run_id, f"item_blocked:{problems[0]}")
                return Command(goto=END)
        payload = approval_payload(run, items)
        context = await approval_context(session, run, items)
        amount = f"{payload['total']} {run.currency}"
        title = f"Payment run {run.reference}: {payload['items']} invoices, {amount}"
        proposer = run.proposed_by
    try:
        approval_id = await request_approval(
            ctx,
            step_key=STEP_KEY,
            approval_type=APPROVAL_TYPE,
            title=title,
            payload=payload,
            context=context,
            entity_type=RUN_ENTITY,
            entity_id=str(run_id),
            preparer_ids=[proposer],
        )
    except PolicyError as exc:
        await _fail(ctx, run_id, f"approval_not_possible:{type(exc).__name__}")
        return Command(goto=END)
    async with ctx.transaction() as session:
        run = await get_run(session, run_id, lock=True)
        run.approval_id = uuid.UUID(approval_id)
        run.updated_at = datetime.now(UTC)
        await audit(
            session,
            By.system(),
            "pay.payment_run.awaiting_approval",
            entity_type=RUN_ENTITY,
            entity_id=run_id,
            payload={"approval_id": approval_id, "run_id": str(ctx.run_id)},
        )
        await refresh_run_decisions(session, run)
    return Command(goto="wait", update={"approval_id": approval_id, "payload": payload})


async def wait(state: RunState, runtime: Ctx) -> Command[Any]:
    outcome = await await_decision(runtime.context, state["approval_id"])
    if outcome is None:
        return Command(goto="wait")
    target = {"approved": "execute", "rejected": "decided", "changes_requested": "decided"}
    return Command(goto=target[outcome], update={"outcome": outcome})


async def execute(state: RunState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    run_id = uuid.UUID(state["payment_run_id"])
    try:
        await run_final_action(
            ctx,
            action=FINAL_ACTION,
            approval_id=state["approval_id"],
            step_key=STEP_KEY,
            payload=state["payload"],
        )
    except ExecutorRefusedError as exc:
        await _fail(ctx, run_id, f"approval_refused:{exc.reason}")
        raise
    async with ctx.transaction() as session:
        await refresh_run_decisions(session, await get_run(session, run_id))
    return {}


async def decided(state: RunState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    async with ctx.transaction() as session:
        await close_after_decision(
            session,
            uuid.UUID(state["payment_run_id"]),
            uuid.UUID(state["approval_id"]),
            state.get("outcome") or "rejected",
        )
    return {}


def build_graph() -> StateGraph[Any, Any, Any, Any]:
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(RunState, context_schema=WorkflowContext)
    retry = node_retry()
    graph.add_node("propose", propose, retry_policy=retry, timeout=60, destinations=("wait", END))
    graph.add_node("wait", wait, destinations=("wait", "execute", "decided"))
    graph.add_node("execute", execute, retry_policy=retry, timeout=60)
    graph.add_node("decided", decided, retry_policy=retry, timeout=30)
    graph.add_edge(START, "propose")
    graph.add_edge("execute", END)
    graph.add_edge("decided", END)
    return graph
