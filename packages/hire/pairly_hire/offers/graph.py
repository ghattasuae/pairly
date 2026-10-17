# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The ``hire_offer`` graph (version 1): an offer waits for its approval chain.

::

    propose -> wait --approved--> execute -> END
                    --rejected--> declined -> END
                    --changes_requested--> changes -> END
                    --(no decision)--> wait

* ``propose``  creates the ``hire.offer`` approval. Its payload binds the exact offer
               (``service.payload_of``: title, start date, annual total and hashes of
               the compensation and conditions) and the hash of the exact offer email
               frozen at submission (``service.approval_payload``), so this one chain
               authorises that email too (one approval per offer). The approvers see
               the email's subject and text in the context. The chain comes from
               ``approval_rules.yaml``: the hiring manager, then HR, and finance above
               the threshold. The submitter started the run, so the engine makes them a
               preparer: they can never approve their own offer.
* ``wait``     interrupts until the chain is decided (read from the database).
* ``execute``  the ``approve_offer`` final action, through the executor (it re-checks
               the approval and the payload hash, and recomputes the offer's payload,
               email included): the offer becomes ``approved`` and the hiring team may
               send that email once (``service.send_offer``).
* ``declined`` / ``changes``: the offer says why; its frozen email is cancelled and
               nothing reaches the candidate.

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
from pairly_core.audit import ActorType
from pairly_core.policy_versions import PolicyError
from pairly_core.workflow.context import WorkflowContext
from pairly_core.workflow.nodes import await_decision, request_approval, run_final_action
from pairly_core.workflow.runtime import node_retry
from pairly_core.workflow.spec import FinalActionCall
from pairly_hire.messaging.compose import Author
from pairly_hire.messaging.models import MessageStatus
from pairly_hire.offers.models import HireOffer, OfferStatus
from pairly_hire.offers.service import (
    SYSTEM_ACTOR,
    approval_payload,
    audit_offer,
    bound_message,
    cancel_bound_message,
    locked,
    message_payload_of,
)
from pairly_hire.pipeline.records import load_record

GRAPH: Final = "hire_offer"
VERSION: Final = 1
STEP_KEY: Final = "approve"
APPROVAL_TYPE: Final = "hire.offer"
FINAL_ACTION: Final = "approve_offer"
ENTITY: Final = "hire.offer"

APPROVAL_RULES_V1: Final = (Path(__file__).resolve().parent / "approval_rules.yaml").read_text(
    encoding="utf-8"
)


class OfferState(TypedDict, total=False):
    offer_id: str
    approval_id: str
    payload: dict[str, Any]
    outcome: str


Ctx = Runtime[WorkflowContext]


async def _audit(
    session: AsyncSession, row: HireOffer, action: str, extra: dict[str, Any] | None = None
) -> None:
    await audit_offer(
        session, row, action, actor_type=ActorType.SYSTEM, actor_id=SYSTEM_ACTOR, extra=extra
    )


async def propose(state: OfferState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    async with ctx.transaction() as session:
        row = await locked(session, state["offer_id"])
        if row.status != OfferStatus.PENDING_APPROVAL.value or row.approval_id is not None:
            return Command(goto=END)
        message = await bound_message(session, row)
        if message is None or message.status != MessageStatus.SUBMITTED.value:
            # No frozen offer email: nothing to approve as one piece (fail closed).
            row.status = OfferStatus.FAILED.value
            row.status_reason = "offer_email_missing"
            row.updated_at = datetime.now(UTC)
            await _audit(session, row, "hire.offer.failed", {"reason": row.status_reason})
            return Command(goto=END)
        record = await load_record(session, row.application_id)
        payload = await approval_payload(session, row, record, message)
        email = await message_payload_of(session, message, record)
        context = {
            "kind": ENTITY,
            **payload,
            "email_subject": email.get("subject"),
            "email_text": email.get("text"),
        }
        title = f"Offer: {row.job_title} ({row.reference})"
    try:
        approval_id = await request_approval(
            ctx,
            step_key=STEP_KEY,
            approval_type=APPROVAL_TYPE,
            title=title,
            payload=payload,
            context=context,
            entity_type=ENTITY,
            entity_id=state["offer_id"],
        )
    except PolicyError as exc:
        # Nobody may approve it: the offer says so (fail closed, nothing is sent).
        async with ctx.transaction() as session:
            row = await locked(session, state["offer_id"])
            row.status = OfferStatus.FAILED.value
            row.status_reason = f"approval_not_possible:{type(exc).__name__}"[:64]
            row.updated_at = datetime.now(UTC)
            await cancel_bound_message(
                session, row, Author(ActorType.SYSTEM, SYSTEM_ACTOR), "offer_failed"
            )
            await _audit(session, row, "hire.offer.failed")
        return Command(goto=END)
    async with ctx.transaction() as session:
        row = await locked(session, state["offer_id"])
        row.approval_id = uuid.UUID(approval_id)
        row.run_id = ctx.run_id
        row.updated_at = datetime.now(UTC)
        message = await bound_message(session, row)
        if message is not None and message.status == MessageStatus.SUBMITTED.value:
            message.status = MessageStatus.AWAITING_APPROVAL.value
            message.approval_id = row.approval_id
            message.run_id = ctx.run_id
            message.content_hash = str(payload["message_sha256"])
            message.updated_at = row.updated_at
        await _audit(
            session,
            row,
            "hire.offer.awaiting_approval",
            {"approval_id": approval_id, "message_sha256": payload["message_sha256"]},
        )
    return Command(goto="wait", update={"approval_id": approval_id, "payload": payload})


async def wait(state: OfferState, runtime: Ctx) -> Command[Any]:
    outcome = await await_decision(runtime.context, state["approval_id"])
    if outcome is None:
        return Command(goto="wait")
    target = {"approved": "execute", "rejected": "declined", "changes_requested": "changes"}
    return Command(goto=target[outcome], update={"outcome": outcome})


async def approve_offer(session: AsyncSession, call: FinalActionCall) -> Mapping[str, Any]:
    """The final action, inside the executor's transaction after it re-checked the
    approval and the payload hash."""
    row = await session.scalar(
        select(HireOffer).where(HireOffer.approval_id == call.approval_id).with_for_update()
    )
    if row is None:
        raise ExecutorRefusedError("offer_not_found")
    if row.status != OfferStatus.PENDING_APPROVAL.value:
        raise ExecutorRefusedError("offer_not_pending")
    message = await bound_message(session, row)
    if message is None or message.approval_id != call.approval_id:
        raise ExecutorRefusedError("offer_email_missing")
    record = await load_record(session, row.application_id)
    if await approval_payload(session, row, record, message) != dict(call.payload):
        raise ExecutorRefusedError("offer_changed")
    now = datetime.now(UTC)
    row.status = OfferStatus.APPROVED.value
    row.status_reason = None
    row.approved_at = now
    row.updated_at = now
    await _audit(
        session,
        row,
        "hire.offer.approved",
        {
            "approval_id": str(call.approval_id),
            "approvers": [str(a) for a in call.approver_ids],
        },
    )
    return {"offer_id": str(row.id), "status": row.status}


async def execute(state: OfferState, runtime: Ctx) -> dict[str, Any]:
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
            row = await locked(session, state["offer_id"])
            if row.status == OfferStatus.PENDING_APPROVAL.value:
                row.status = OfferStatus.FAILED.value
                row.status_reason = f"refused:{exc.reason}"[:64]
                row.updated_at = datetime.now(UTC)
                await cancel_bound_message(
                    session, row, Author(ActorType.SYSTEM, SYSTEM_ACTOR), "offer_failed"
                )
                await _audit(session, row, "hire.offer.failed")
        raise
    return {}


async def _close(ctx: WorkflowContext, state: OfferState, status: OfferStatus) -> None:
    async with ctx.transaction() as session:
        row = await locked(session, state["offer_id"])
        if row.status != OfferStatus.PENDING_APPROVAL.value:
            return
        now = datetime.now(UTC)
        row.status = status.value
        row.status_reason = state.get("outcome")
        row.updated_at = now
        await cancel_bound_message(
            session, row, Author(ActorType.SYSTEM, SYSTEM_ACTOR), f"offer_{status.value}"
        )
        await _audit(
            session, row, f"hire.offer.{status.value}", {"approval_id": state.get("approval_id")}
        )


async def declined(state: OfferState, runtime: Ctx) -> dict[str, Any]:
    await _close(runtime.context, state, OfferStatus.REJECTED)
    return {}


async def changes(state: OfferState, runtime: Ctx) -> dict[str, Any]:
    await _close(runtime.context, state, OfferStatus.CHANGES_REQUESTED)
    return {}


def build_graph() -> StateGraph[Any, Any, Any, Any]:
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(OfferState, context_schema=WorkflowContext)
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
