# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The ``hire_message`` graph (version 1): a sensitive message waits for a person.

::

    propose -> wait --approved--> execute -> END
                    --rejected--> declined -> END
                    --changes_requested--> changes -> END
                    --(no decision)--> wait

* ``propose``  renders the submitted message from its frozen values and creates the
               ``hire.message`` approval. Its payload is the engine's message payload
               (``sensitive_message_payload``): template, recipient, subject and the
               exact text. The submitter is a preparer, so they can never approve it.
* ``wait``     interrupts until a person decides (read from the database).
* ``execute``  the ``send_candidate_message`` final action, through the executor:
               the executor re-checks the approval and the payload hash, then the
               action queues the email with ``notify(approval_id=...)``, which checks
               the hash of the rendered email against the approval once more. Any
               change to the template or the values after the approval blocks the send.
* ``declined`` / ``changes``: the message is not sent; it says why.

Nothing here sends without a recorded human approval: there is no edge from
``propose`` to ``execute`` that skips ``wait``.

``restart_stuck`` (run by the ``hire_sla_drafts`` job): a message submitted but whose
run never started (the API process stopped right after the response) is started again
as the person who submitted it, if they may still; otherwise it fails with the reason,
so a person sees it (fail closed: nothing is sent).
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.approvals.executor import ExecutorRefusedError
from pairly_core.audit import ActorType, append_audit
from pairly_core.db import set_tenant
from pairly_core.errors import PairlyError
from pairly_core.identity import load_actor
from pairly_core.models import User
from pairly_core.notifications.service import (
    Recipient,
    SensitiveTemplateError,
    message_hash,
    notify,
    sensitive_message_payload,
)
from pairly_core.plugins import discover_plugins
from pairly_core.policy_versions import PolicyError
from pairly_core.rbac import Actor, RoleRegistry, build_registry
from pairly_core.workflow.context import WorkflowContext
from pairly_core.workflow.nodes import await_decision, request_approval, run_final_action
from pairly_core.workflow.runtime import WorkflowRuntime, node_retry
from pairly_core.workflow.spec import FinalActionCall
from pairly_hire.messaging import library
from pairly_hire.messaging.compose import CHANNEL, frozen_values, mark_updated
from pairly_hire.messaging.models import HireMessage, MessageStatus
from pairly_hire.models import ApplicationStatus
from pairly_hire.offers.models import HireOffer
from pairly_hire.permissions import MESSAGE_CREATE
from pairly_hire.pipeline.records import load_record

log = structlog.get_logger("pairly_hire.messaging.graph")

GRAPH: Final = "hire_message"
VERSION: Final = 1
STEP_KEY: Final = "send"
APPROVAL_TYPE: Final = "hire.message"
FINAL_ACTION: Final = "send_candidate_message"
ACTOR_ID: Final = "pairly.hire.message_graph"
WORKFLOW: Final = "hire"
ENTITY: Final = "hire.message"

APPROVAL_RULES_V1: Final = """\
# Who approves a sensitive message to a candidate (a rejection or an offer), version 1.
# One person of the hiring team who did not submit it (segregation of duties is
# enforced by the engine).
schema: pairly.approval_rules/v1
approval_type: hire.message
sla_hours: 24
warn_hours: 4
chains:
  - name: review
    steps:
      - {name: reviewer, mode: any_of, roles: [recruiter, hiring_manager, admin]}
"""


class MessageState(TypedDict, total=False):
    message_id: str
    approval_id: str
    payload: dict[str, Any]
    outcome: str


Ctx = Runtime[WorkflowContext]


@functools.cache
def _registry() -> RoleRegistry:
    """The role registry of this process's plugins (``notify`` re-checks the approvers)."""
    return build_registry(discover_plugins().values())


def _message_id(state: MessageState) -> uuid.UUID:
    return uuid.UUID(state["message_id"])


async def _locked(session: AsyncSession, message_id: uuid.UUID) -> HireMessage:
    message = await session.scalar(
        select(HireMessage).where(HireMessage.id == message_id).with_for_update()
    )
    if message is None:
        raise LookupError("message not found")
    return message


async def _audit(
    session: AsyncSession, action: str, message: HireMessage, payload: dict[str, Any]
) -> None:
    await append_audit(
        session,
        action=action,
        actor_type=ActorType.SYSTEM,
        actor_id=ACTOR_ID,
        entity_type=ENTITY,
        entity_id=message.id,
        workflow=WORKFLOW,
        payload={"application_id": str(message.application_id), "status": message.status} | payload,
    )


async def _offer_bound(session: AsyncSession, message_id: uuid.UUID) -> bool:
    found = await session.scalar(select(HireOffer.id).where(HireOffer.message_id == message_id))
    return found is not None


def _title(message: HireMessage, job_title: str, reference: str) -> str:
    entry = library.entry(message.template_key or "")
    what = entry.label if entry is not None else "Message"
    return f"{what} to candidate {reference}, {job_title}"


async def propose(state: MessageState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    message_id = _message_id(state)
    async with ctx.transaction() as session:
        message = await _locked(session, message_id)
        if message.status != MessageStatus.SUBMITTED.value or message.template_key is None:
            return Command(goto=END)
        if await _offer_bound(session, message.id):
            # An offer's email is approved by the offer's own chain, never here.
            return Command(goto=END)
        record = await load_record(session, message.application_id)
        payload = await sensitive_message_payload(
            session,
            recipient=Recipient.external(record.email, locale=message.locale),
            template_key=message.template_key,
            variables=frozen_values(message),
            channel=CHANNEL,
        )
        title = _title(message, record.job.title, record.app.reference)
        context = {
            "kind": "hire.message",
            "template_key": message.template_key,
            "application_reference": record.app.reference,
            "job_title": record.job.title,
            "stage": record.stage_key,
            "ai_fields": list(message.ai_fields),
            "reason_source": message.reason_source,
            "subject": payload.get("subject"),
            "text": payload.get("text"),
        }
    try:
        approval_id = await request_approval(
            ctx,
            step_key=STEP_KEY,
            approval_type=APPROVAL_TYPE,
            title=title,
            payload=payload,
            context=context,
            entity_type=ENTITY,
            entity_id=str(message_id),
        )
    except PolicyError as exc:
        # Nobody may approve: the message stays unsent and says so (fail closed).
        async with ctx.transaction() as session:
            message = await _locked(session, message_id)
            message.status = MessageStatus.FAILED.value
            message.status_reason = f"approval_not_possible:{type(exc).__name__}"[:64]
            message.updated_at = datetime.now(UTC)
            await _audit(session, "hire.message.failed", message, {"reason": message.status_reason})
        return Command(goto=END)
    async with ctx.transaction() as session:
        message = await _locked(session, message_id)
        message.approval_id = uuid.UUID(approval_id)
        message.run_id = ctx.run_id
        message.status = MessageStatus.AWAITING_APPROVAL.value
        message.updated_at = datetime.now(UTC)
        message.content_hash = message_hash(payload)
        await _audit(
            session,
            "hire.message.awaiting_approval",
            message,
            {"approval_id": approval_id, "content_hash": message.content_hash},
        )
    return Command(goto="wait", update={"approval_id": approval_id, "payload": payload})


async def wait(state: MessageState, runtime: Ctx) -> Command[Any]:
    outcome = await await_decision(runtime.context, state["approval_id"])
    if outcome is None:
        return Command(goto="wait")
    target = {"approved": "execute", "rejected": "declined", "changes_requested": "changes"}
    return Command(goto=target[outcome], update={"outcome": outcome})


async def send_candidate_message(session: AsyncSession, call: FinalActionCall) -> Mapping[str, Any]:
    """The final action: queue the approved email. Runs inside the executor's transaction,
    after it re-checked the approval; ``notify`` checks the payload hash once more."""
    message = await session.scalar(
        select(HireMessage).where(HireMessage.approval_id == call.approval_id).with_for_update()
    )
    if message is None or message.template_key is None:
        raise ExecutorRefusedError("message_not_found")
    if message.status != MessageStatus.AWAITING_APPROVAL.value:
        raise ExecutorRefusedError("message_not_awaiting_approval")
    record = await load_record(session, message.application_id)
    if record.app.status == ApplicationStatus.WITHDRAWN.value:
        # The candidate withdrew from the portal while it waited: nothing is sent.
        raise ExecutorRefusedError("application_withdrawn")
    try:
        results = await notify(
            session,
            recipient=Recipient.external(record.email, locale=message.locale),
            template_key=message.template_key,
            variables=frozen_values(message),
            dedupe_key=f"hire.msg:{message.id}",
            channels=[CHANNEL],
            approval_id=call.approval_id,
            registry=_registry(),
            workflow=WORKFLOW,
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR_ID,
        )
    except SensitiveTemplateError as exc:
        # The email would differ from what the approver saw (a template or a value
        # changed since): nothing is sent (fail closed).
        raise ExecutorRefusedError(f"sensitive_{exc.reason}") from None
    sent = next((r for r in results if r.channel == CHANNEL), None)
    if sent is None or sent.notification_id is None:
        raise ExecutorRefusedError("message_not_queued")
    now = datetime.now(UTC)
    message.status = MessageStatus.QUEUED.value
    message.notification_id = sent.notification_id
    message.sent_at = now
    message.updated_at = now
    await mark_updated(record, session, now)
    await _audit(
        session,
        "hire.message.sent",
        message,
        {
            "notification_id": str(sent.notification_id),
            "approval_id": str(call.approval_id),
            "approvers": [str(a) for a in call.approver_ids],
        },
    )
    return {"message_id": str(message.id), "notification_id": str(sent.notification_id)}


async def execute(state: MessageState, runtime: Ctx) -> dict[str, Any]:
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
            message = await _locked(session, _message_id(state))
            message.status = MessageStatus.FAILED.value
            message.status_reason = f"refused:{exc.reason}"[:64]
            message.updated_at = datetime.now(UTC)
            await _audit(session, "hire.message.failed", message, {"reason": message.status_reason})
        raise
    return {}


async def _close(ctx: WorkflowContext, state: MessageState, status: MessageStatus) -> None:
    async with ctx.transaction() as session:
        message = await _locked(session, _message_id(state))
        if message.status != MessageStatus.AWAITING_APPROVAL.value:
            return
        message.status = status.value
        message.status_reason = state.get("outcome")
        message.updated_at = datetime.now(UTC)
        await _audit(
            session,
            f"hire.message.{status.value}",
            message,
            {"approval_id": state.get("approval_id")},
        )


async def declined(state: MessageState, runtime: Ctx) -> dict[str, Any]:
    await _close(runtime.context, state, MessageStatus.DECLINED)
    return {}


async def changes(state: MessageState, runtime: Ctx) -> dict[str, Any]:
    await _close(runtime.context, state, MessageStatus.CHANGES_REQUESTED)
    return {}


def build_graph() -> StateGraph[Any, Any, Any, Any]:
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(MessageState, context_schema=WorkflowContext)
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


# ------------------------------------------------------------------ stuck submissions

STUCK_AFTER: Final = timedelta(minutes=2)


async def _submitter(
    sessionmaker: async_sessionmaker[AsyncSession],
    registry: RoleRegistry,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Actor | None:
    session = sessionmaker()
    set_tenant(session, org_id, None)
    async with session, session.begin():
        subject = await session.scalar(select(User.auth_subject).where(User.id == user_id))
    if subject is None:
        return None
    session = sessionmaker()
    async with session:
        try:
            actor = await load_actor(session, registry, subject=subject, org_id=org_id)
        except PairlyError:
            return None
        finally:
            await session.rollback()
    return actor if actor.has(MESSAGE_CREATE) else None


async def restart_stuck(
    sessionmaker: async_sessionmaker[AsyncSession],
    runtime: WorkflowRuntime,
    registry: RoleRegistry,
    org_id: uuid.UUID,
) -> int:
    """Start the run of messages submitted more than ``STUCK_AFTER`` ago without one."""
    session = sessionmaker()
    set_tenant(session, org_id, None)
    async with session, session.begin():
        stuck = list(
            (
                await session.execute(
                    select(HireMessage.id, HireMessage.submitted_by).where(
                        HireMessage.status == MessageStatus.SUBMITTED.value,
                        HireMessage.run_id.is_(None),
                        # An offer's email waits for the offer chain, not for a run here.
                        ~select(HireOffer.id)
                        .where(HireOffer.message_id == HireMessage.id)
                        .exists(),
                        HireMessage.updated_at < datetime.now(UTC) - STUCK_AFTER,
                    )
                )
            ).all()
        )
    started = 0
    for message_id, user_id in stuck:
        actor = await _submitter(sessionmaker, registry, org_id, user_id) if user_id else None
        if actor is None:
            session = sessionmaker()
            set_tenant(session, org_id, None)
            async with session, session.begin():
                message = await _locked(session, message_id)
                if message.status == MessageStatus.SUBMITTED.value and message.run_id is None:
                    message.status = MessageStatus.FAILED.value
                    message.status_reason = "submitter_lost_access"
                    message.updated_at = datetime.now(UTC)
                    await _audit(
                        session, "hire.message.failed", message, {"reason": message.status_reason}
                    )
            continue
        try:
            await runtime.start_run(
                actor,
                workflow=WORKFLOW,
                graph=GRAPH,
                input={"message_id": str(message_id)},
                entity_type=ENTITY,
                entity_id=str(message_id),
            )
            started += 1
        except Exception as exc:
            log.warning(
                "hire_message_restart_failed", message_id=str(message_id), error=type(exc).__name__
            )
    return started
