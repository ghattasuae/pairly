# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The ``pay_invoice_approval`` graph (version 1): an invoice waits for its approvers.

::

    propose -> wait --approved--> execute -> END
                    --rejected | changes_requested--> decided -> END
                    --(no decision)--> wait

Started by the person who sent the invoice for approval
(``pairly_pay.approvals.submit_for_approval``), so the engine makes them a preparer.

* ``propose``  checks the gates again, builds the payload from the database and asks
               for a ``pay.invoice`` approval; everyone who processed the invoice is a
               preparer. If no chain or no approver can be found, the invoice goes back
               to a person (fail closed).
* ``wait``     interrupts until the chain is decided (read from the database).
* ``execute``  the ``approve_invoice`` final action through the executor. A refusal
               sends the invoice back to a person.
* ``decided``  a rejection closes the invoice; a change request sends it back.

There is no edge from ``propose`` to ``execute`` that skips ``wait``.
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
from pairly_pay.approvals import (
    APPROVAL_TYPE,
    FINAL_ACTION,
    STEP_KEY,
    NotApprovableError,
    approval_context,
    back_to_person,
    close_after_decision,
    prepare,
    preparers,
)
from pairly_pay.common import By, audit
from pairly_pay.invoices import INVOICE_ENTITY, get_invoice, refresh_decisions
from pairly_pay.models import InvoiceStatus

__all__ = ["build_graph"]


class ApprovalState(TypedDict, total=False):
    invoice_id: str
    approval_id: str
    payload: dict[str, Any]
    outcome: str


Ctx = Runtime[WorkflowContext]


async def _back(ctx: WorkflowContext, invoice_id: uuid.UUID, reason: str) -> None:
    async with ctx.transaction() as session:
        await back_to_person(session, invoice_id, reason, run_id=ctx.run_id)


async def propose(state: ApprovalState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    invoice_id = uuid.UUID(state["invoice_id"])
    async with ctx.transaction() as session:
        invoice = await get_invoice(session, invoice_id, lock=True)
        if invoice.status != InvoiceStatus.AWAITING_APPROVAL.value:
            return Command(goto=END)
        if invoice.approval_run_id is None:
            invoice.approval_run_id = ctx.run_id
        elif invoice.approval_run_id != ctx.run_id:
            return Command(goto=END)  # another run owns this invoice
        try:
            prepared = await prepare(session, invoice)
        except NotApprovableError as exc:
            await back_to_person(
                session, invoice_id, f"approval_blocked:{exc.reasons[0]}", run_id=ctx.run_id
            )
            return Command(goto=END)
        payload = prepared.payload
        context = approval_context(
            invoice, prepared.vendor, prepared.bank, prepared.profile, prepared.vendor_risk
        )
        exclude = await preparers(session, invoice)
        amount = f"{payload['amount']} {payload['currency']}"
        title = f"Invoice {invoice.reference}: {prepared.vendor.legal_name}, {amount}"
    try:
        approval_id = await request_approval(
            ctx,
            step_key=STEP_KEY,
            approval_type=APPROVAL_TYPE,
            title=title,
            payload=payload,
            context=context,
            entity_type=INVOICE_ENTITY,
            entity_id=str(invoice_id),
            preparer_ids=exclude,
        )
    except PolicyError as exc:
        await _back(ctx, invoice_id, f"approval_not_possible:{type(exc).__name__}")
        return Command(goto=END)
    async with ctx.transaction() as session:
        invoice = await get_invoice(session, invoice_id, lock=True)
        invoice.approval_id = uuid.UUID(approval_id)
        invoice.updated_at = datetime.now(UTC)
        await audit(
            session,
            By.system(),
            "pay.invoice.awaiting_approval",
            entity_type=INVOICE_ENTITY,
            entity_id=invoice_id,
            payload={"approval_id": approval_id, "run_id": str(ctx.run_id)},
        )
        await refresh_decisions(session, invoice)
    return Command(goto="wait", update={"approval_id": approval_id, "payload": payload})


async def wait(state: ApprovalState, runtime: Ctx) -> Command[Any]:
    outcome = await await_decision(runtime.context, state["approval_id"])
    if outcome is None:
        return Command(goto="wait")
    target = {"approved": "execute", "rejected": "decided", "changes_requested": "decided"}
    return Command(goto=target[outcome], update={"outcome": outcome})


async def execute(state: ApprovalState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    invoice_id = uuid.UUID(state["invoice_id"])
    try:
        await run_final_action(
            ctx,
            action=FINAL_ACTION,
            approval_id=state["approval_id"],
            step_key=STEP_KEY,
            payload=state["payload"],
        )
    except ExecutorRefusedError as exc:
        await _back(ctx, invoice_id, f"approval_refused:{exc.reason}")
        raise
    async with ctx.transaction() as session:
        await refresh_decisions(session, await get_invoice(session, invoice_id))
    return {}


async def decided(state: ApprovalState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    async with ctx.transaction() as session:
        await close_after_decision(
            session,
            uuid.UUID(state["invoice_id"]),
            uuid.UUID(state["approval_id"]),
            state.get("outcome") or "rejected",
        )
    return {}


def build_graph() -> StateGraph[Any, Any, Any, Any]:
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(
        ApprovalState, context_schema=WorkflowContext
    )
    retry = node_retry()
    graph.add_node("propose", propose, retry_policy=retry, timeout=60, destinations=("wait", END))
    graph.add_node("wait", wait, destinations=("wait", "execute", "decided"))
    graph.add_node("execute", execute, retry_policy=retry, timeout=60)
    graph.add_node("decided", decided, retry_policy=retry, timeout=30)
    graph.add_edge(START, "propose")
    graph.add_edge("execute", END)
    graph.add_edge("decided", END)
    return graph
