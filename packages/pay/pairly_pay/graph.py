# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The ``pay_bank_change`` graph (version 1): a verified change waits for two approvers.

::

    propose -> wait --approved--> execute -> END
                    --rejected | changes_requested--> rejected -> END
                    --(no decision)--> wait

The run starts only after the out-of-band call confirmed the change
(``pairly_pay.bank_changes.record_call``), started by the verifier. So the engine makes
the verifier a preparer, and ``propose`` adds the requester: neither can ever approve.

* ``propose``   creates the ``pay.bank_change`` approval (two steps, see
                ``policy/bank_change_approval_rules.yaml``). Its payload binds the
                ticket, the proposed account and the keyed digest of the locked
                details. If fewer than two people could approve, the ticket fails, the
                approval is cancelled and the hold stays (fail closed).
* ``wait``      interrupts until the chain is decided (read from the database).
* ``execute``   the ``approve_bank_change`` final action through the executor: it
                re-checks the approval, the payload hash, the details and the
                segregation of duties, then starts the cooling-off period (or activates
                at once with zero hours).
* ``rejected``  the old details stay; the hold lifts per rule.

There is no edge from ``propose`` to ``execute`` that skips ``wait``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Final, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from sqlalchemy import select

from pairly_core.approvals.executor import ExecutorRefusedError
from pairly_core.approvals.models import ApprovalDecision, ApprovalStep
from pairly_core.approvals.service import cancel_approval
from pairly_core.policy_versions import PolicyError
from pairly_core.workflow.context import WorkflowContext
from pairly_core.workflow.nodes import await_decision, request_approval, run_final_action
from pairly_core.workflow.runtime import node_retry
from pairly_pay.bank_changes import (
    FINAL_ACTION,
    approval_payload,
    close_after_decision,
    fail_ticket,
    get_ticket,
    preparers,
    refresh_decisions,
)
from pairly_pay.common import SYSTEM_ACTOR, TICKET_ENTITY, By, audit
from pairly_pay.models import PayVendorBankAccount, TicketStatus
from pairly_pay.vendors import get_vendor, masked_account, risk_profile

GRAPH: Final = "pay_bank_change"
VERSION: Final = 1
STEP_KEY: Final = "dual_approval"
APPROVAL_TYPE: Final = "pay.bank_change"
MIN_APPROVERS: Final = 2


class ChangeState(TypedDict, total=False):
    ticket_id: str
    approval_id: str
    payload: dict[str, Any]
    outcome: str


Ctx = Runtime[WorkflowContext]


async def _fail(ctx: WorkflowContext, ticket_id: uuid.UUID, reason: str) -> None:
    async with ctx.transaction() as session:
        ticket = await get_ticket(session, ticket_id, lock=True)
        if ticket.status == TicketStatus.AWAITING_APPROVAL.value:
            await fail_ticket(session, ticket, reason)


async def propose(state: ChangeState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    ticket_id = uuid.UUID(state["ticket_id"])
    async with ctx.transaction() as session:
        ticket = await get_ticket(session, ticket_id, lock=True)
        if ticket.status != TicketStatus.AWAITING_APPROVAL.value:
            return Command(goto=END)
        if ticket.run_id is None:
            ticket.run_id = ctx.run_id
        elif ticket.run_id != ctx.run_id:
            return Command(goto=END)  # another run owns this ticket
        vendor = await get_vendor(session, ticket.vendor_id)
        proposed = await session.scalar(
            select(PayVendorBankAccount).where(
                PayVendorBankAccount.id == ticket.proposed_account_id
            )
        )
        previous = (
            await session.scalar(
                select(PayVendorBankAccount).where(
                    PayVendorBankAccount.id == ticket.previous_account_id
                )
            )
            if ticket.previous_account_id
            else None
        )
        if proposed is None:
            await fail_ticket(session, ticket, "proposed_details_missing")
            return Command(goto=END)
        payload = approval_payload(ticket, vendor, proposed)
        profile = await risk_profile(session, vendor)
        context = {
            "kind": TICKET_ENTITY,
            "ticket_path": f"/pay/bank-changes/{ticket.id}",
            "vendor_reference": vendor.reference,
            "source": ticket.source,
            "old_iban_masked": masked_account(previous) if previous else None,
            "old_bank_name": previous.bank_name if previous else None,
            "new_iban_masked": masked_account(proposed),
            "new_bank_name": proposed.bank_name,
            "new_swift": proposed.swift,
            "phone_recently_changed": ticket.phone_recently_changed,
            "cooling_off_hours": ticket.cooling_off_hours,
            "risk_signals": profile["signals"],
        }
        title = f"Bank details change: {vendor.legal_name} ({ticket.reference})"
        exclude = preparers(ticket)
    try:
        approval_id = await request_approval(
            ctx,
            step_key=STEP_KEY,
            approval_type=APPROVAL_TYPE,
            title=title,
            payload=payload,
            context=context,
            entity_type=TICKET_ENTITY,
            entity_id=str(ticket_id),
            preparer_ids=exclude,
        )
    except PolicyError as exc:
        await _fail(ctx, ticket_id, f"approval_not_possible:{type(exc).__name__}")
        return Command(goto=END)
    async with ctx.transaction() as session:
        steps = list(
            await session.scalars(
                select(ApprovalStep).where(ApprovalStep.approval_id == uuid.UUID(approval_id))
            )
        )
        people = {p for step in steps for p in step.assignee_ids} - set(exclude)
        ticket = await get_ticket(session, ticket_id, lock=True)
        if len(steps) < MIN_APPROVERS or len(people) < MIN_APPROVERS:
            # Two different people must approve and the rules cannot give two: fail closed.
            await cancel_approval(
                session,
                uuid.UUID(approval_id),
                actor_type=By.system().actor_type,
                actor_id=SYSTEM_ACTOR,
            )
            if ticket.status == TicketStatus.AWAITING_APPROVAL.value:
                ticket.approval_id = uuid.UUID(approval_id)
                await fail_ticket(session, ticket, "approval_not_possible:fewer_than_two_approvers")
            return Command(goto=END)
        ticket.approval_id = uuid.UUID(approval_id)
        ticket.updated_at = datetime.now(UTC)
        await audit(
            session,
            By.system(),
            "pay.bank_change.awaiting_approval",
            entity_type=TICKET_ENTITY,
            entity_id=ticket_id,
            payload={
                "approval_id": approval_id,
                "run_id": str(ctx.run_id),
                "approvers_possible": len(people),
            },
        )
        await refresh_decisions(session, ticket)
    return Command(goto="wait", update={"approval_id": approval_id, "payload": payload})


async def wait(state: ChangeState, runtime: Ctx) -> Command[Any]:
    outcome = await await_decision(runtime.context, state["approval_id"])
    if outcome is None:
        return Command(goto="wait")
    target = {"approved": "execute", "rejected": "rejected", "changes_requested": "rejected"}
    return Command(goto=target[outcome], update={"outcome": outcome})


async def execute(state: ChangeState, runtime: Ctx) -> dict[str, Any]:
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
        await _fail(ctx, uuid.UUID(state["ticket_id"]), f"executor_refused:{exc.reason}")
        raise
    async with ctx.transaction() as session:
        await refresh_decisions(session, await get_ticket(session, uuid.UUID(state["ticket_id"])))
    return {}


async def rejected(state: ChangeState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    outcome = (
        "rejected_by_approver"
        if state.get("outcome") == "rejected"
        else "changes_requested_by_approver"
    )
    async with ctx.transaction() as session:
        reason = await session.scalar(
            select(ApprovalDecision.reason)
            .where(ApprovalDecision.approval_id == uuid.UUID(state["approval_id"]))
            .order_by(ApprovalDecision.created_at.desc())
            .limit(1)
        )
        await close_after_decision(session, uuid.UUID(state["ticket_id"]), outcome, reason)
    return {}


def build_graph() -> StateGraph[Any, Any, Any, Any]:
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(ChangeState, context_schema=WorkflowContext)
    retry = node_retry()
    graph.add_node("propose", propose, retry_policy=retry, timeout=60, destinations=("wait", END))
    graph.add_node("wait", wait, destinations=("wait", "execute", "rejected"))
    graph.add_node("execute", execute, retry_policy=retry, timeout=60)
    graph.add_node("rejected", rejected, retry_policy=retry, timeout=30)
    graph.add_edge(START, "propose")
    graph.add_edge("execute", END)
    graph.add_edge("rejected", END)
    return graph
