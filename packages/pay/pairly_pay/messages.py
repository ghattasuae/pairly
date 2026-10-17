# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Messages about invoice exceptions (brief 4.5.4), sent only after approval.

A person (``pay.message.create``) saves a drafted message (``pairly_pay.exceptions``),
possibly edited, for one recipient:

* the **vendor**: only an email address of a contact in the vendor master (never an
  address typed in, so a message can never be steered to a look-alike address);
* the **requester**: only a member of the organisation, found by their email address.

The ``pay_message`` graph (version 1) then waits for another person::

    propose -> wait --approved--> execute -> END
                    --rejected--> declined -> END
                    --changes_requested--> changes -> END
                    --(no decision)--> wait

* ``propose``  renders the message through the engine's sensitive template
               ``pay.vendor.message`` and creates the ``pay.message`` approval whose
               payload is the exact rendered message (``sensitive_message_payload``).
               The submitter is a preparer, so they never approve it.
* ``execute``  the ``send_pay_message`` final action, through the executor: the
               executor re-checks the approval and its payload hash, then ``notify``
               queues the email with ``approval_id`` and checks the hash of the rendered
               email against the approval once more. Any change after the approval
               blocks the send (fail closed).

Nothing is sent without a recorded human approval: there is no edge from ``propose``
to ``execute`` that skips ``wait``. A run that cannot start or that nobody may approve
leaves the message ``failed`` with the reason. Every step is audited.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any, Final, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.executor import ExecutorRefusedError
from pairly_core.audit import ActorType
from pairly_core.crypto.types import seal
from pairly_core.errors import BadRequestError, NotFoundError
from pairly_core.models import Membership, Organisation, User
from pairly_core.notifications.links import absolute_url, public_web_url
from pairly_core.notifications.service import (
    Recipient,
    SensitiveTemplateError,
    message_hash,
    notify,
    sensitive_message_payload,
)
from pairly_core.notifications.templates import resolve_template
from pairly_core.plugins import discover_plugins
from pairly_core.policy_versions import PolicyError
from pairly_core.rbac import Actor, RoleRegistry, build_registry
from pairly_core.workflow.context import WorkflowContext
from pairly_core.workflow.nodes import await_decision, request_approval, run_final_action
from pairly_core.workflow.runtime import node_retry
from pairly_core.workflow.spec import FinalActionCall
from pairly_pay.common import WORKFLOW, By, audit, new_reference, opened
from pairly_pay.exceptions import categorise, check_message, template_for
from pairly_pay.invoices import InvoiceStateError, get_invoice
from pairly_pay.models import (
    InvoiceStatus,
    MessageChannel,
    MessageDirection,
    MessageRecipient,
    MessageStatus,
    PayInvoice,
    PayMessage,
    PayVendor,
)
from pairly_pay.portal.models import ContactStatus, PayPortalContact
from pairly_pay.vendors import contacts_of

log = structlog.get_logger("pairly_pay.messages")

GRAPH: Final = "pay_message"
VERSION: Final = 1
STEP_KEY: Final = "send"
APPROVAL_TYPE: Final = "pay.message"
FINAL_ACTION: Final = "send_pay_message"
TEMPLATE_KEY: Final = "pay.vendor.message"
# An AP reply in the vendor portal (``pairly_pay.portal.templates``).
PORTAL_REPLY: Final = "pay.vendor_portal.reply"
PORTAL_PATH: Final = "/vendor-portal"
CHANNEL: Final = "email"
ENTITY: Final = "pay.message"
ACTOR_ID: Final = "pairly.pay.message_graph"
REFERENCE_PREFIX: Final = "MSG"
MESSAGE_RULES_V1: Final = (
    files("pairly_pay")
    .joinpath("policy", "message_approval_rules.yaml")
    .read_text(encoding="utf-8")
)
OPEN_FOR_MESSAGES: Final = frozenset({InvoiceStatus.NEEDS_REVIEW.value, InvoiceStatus.READY.value})


class MessageNotFoundError(NotFoundError):
    def __init__(self) -> None:
        super().__init__("message not found")


class RecipientNotAllowedError(BadRequestError):
    code = "recipient_not_allowed"
    title = "Recipient not allowed"


@dataclass(frozen=True, slots=True)
class MessageInput:
    category: str
    template_id: str
    locale: str
    text: str
    # Vendor: an email address of a contact on file. Requester: a member's email address.
    recipient_email: str
    ai_note: bool = False
    model_call_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Contact:
    name: str
    email: str


def vendor_contacts(vendor: PayVendor) -> list[Contact]:
    """The vendor's contacts that have an email address (the only vendor recipients)."""
    out: list[Contact] = []
    for item in contacts_of(vendor):
        email = str(item.get("email") or "").strip()
        if email and "@" in email:
            out.append(Contact(str(item.get("name") or email)[:120], email[:254]))
    return out


async def _member(session: AsyncSession, email: str) -> User | None:
    """A member of this organisation with that email address (memberships are org bound)."""
    return await session.scalar(
        select(User)
        .join(Membership, Membership.user_id == User.id)
        .where(func.lower(User.email) == email.strip().lower())
        .limit(1)
    )


def _seal(message: PayMessage, column: str, value: str | None) -> Any:
    if value is None:
        return None
    return seal(PayMessage, column, value, org_id=message.org_id, row_id=message.id)


async def create_message(
    session: AsyncSession, actor: Actor, invoice_id: uuid.UUID, data: MessageInput
) -> PayMessage:
    """Save a message for approval (the caller starts the run after the commit)."""
    invoice = await get_invoice(session, invoice_id, lock=True)
    if invoice.status not in OPEN_FOR_MESSAGES:
        raise InvoiceStateError(f"the invoice is {invoice.status}")
    if data.category not in {c.key for c in categorise(invoice)}:
        raise BadRequestError("the invoice has no exception of this category")
    template = template_for(data.category, data.template_id)
    if template is None:
        raise BadRequestError("no message template of this category")
    if data.locale not in {"en", "ar"}:
        raise BadRequestError("the language must be en or ar")
    problems = check_message(data.text)
    if problems:
        raise BadRequestError("the message cannot be sent: " + ", ".join(problems))
    user_id: uuid.UUID | None = None
    email: str | None = None
    if template.recipient is MessageRecipient.VENDOR:
        vendor = (
            await session.scalar(select(PayVendor).where(PayVendor.id == invoice.vendor_id))
            if invoice.vendor_id
            else None
        )
        if vendor is None:
            raise RecipientNotAllowedError("link the invoice to its vendor first")
        wanted = data.recipient_email.strip().lower()
        contact = next((c for c in vendor_contacts(vendor) if c.email.lower() == wanted), None)
        if contact is None:
            raise RecipientNotAllowedError("only a contact on file of the vendor can be written to")
        name, email = contact.name, contact.email
    else:
        member = await _member(session, data.recipient_email)
        if member is None:
            raise RecipientNotAllowedError("the requester must be a member of the organisation")
        name, user_id = (member.display_name or member.email)[:120], member.id
    now = datetime.now(UTC)
    message = PayMessage(
        id=uuid.uuid4(),
        org_id=invoice.org_id,
        reference=new_reference(REFERENCE_PREFIX),
        invoice_id=invoice.id,
        category=data.category,
        template_id=template.id,
        recipient_kind=template.recipient.value,
        recipient_user_id=user_id,
        locale=data.locale,
        ai_note=data.ai_note,
        model_call_id=data.model_call_id,
        status=MessageStatus.SUBMITTED.value,
        submitted_by=actor.id,
        created_at=now,
        updated_at=now,
    )
    message.recipient_name = _seal(message, "recipient_name", name)
    message.recipient_email = _seal(message, "recipient_email", email)
    message.message = _seal(message, "message", data.text.strip())
    session.add(message)
    await session.flush()
    await audit(
        session,
        actor,
        "pay.message.submitted",
        entity_type=ENTITY,
        entity_id=message.id,
        payload={
            "invoice_id": str(invoice.id),
            "category": message.category,
            "template_id": message.template_id,
            "recipient_kind": message.recipient_kind,
            "locale": message.locale,
            "ai_note": message.ai_note,
        },
    )
    return message


async def messages_of(session: AsyncSession, invoice_id: uuid.UUID) -> list[PayMessage]:
    rows = await session.scalars(
        select(PayMessage)
        .where(
            PayMessage.invoice_id == invoice_id,
            PayMessage.channel == MessageChannel.EMAIL.value,
        )
        .order_by(PayMessage.created_at.desc(), PayMessage.id)
        .limit(100)
    )
    return list(rows)


async def fail_message(session: AsyncSession, message_id: uuid.UUID, reason: str) -> None:
    """The message is not sent, with the reason (a run that never started)."""
    message = await _locked(session, message_id)
    if message.status != MessageStatus.SUBMITTED.value:
        return
    message.status = MessageStatus.FAILED.value
    message.status_reason = reason[:64]
    message.updated_at = datetime.now(UTC)
    await _audit(session, "pay.message.failed", message, {"reason": message.status_reason})


# ------------------------------------------------------------------ vendor portal


async def thread_of(session: AsyncSession, invoice_id: uuid.UUID) -> list[PayMessage]:
    """The vendor portal thread of an invoice for AP: the vendor's messages and every
    portal reply (oldest first)."""
    rows = await session.scalars(
        select(PayMessage)
        .where(
            PayMessage.invoice_id == invoice_id,
            PayMessage.channel == MessageChannel.PORTAL.value,
        )
        .order_by(PayMessage.created_at, PayMessage.id)
        .limit(200)
    )
    return list(rows)


async def _portal_recipient(session: AsyncSession, invoice: PayInvoice) -> PayPortalContact | None:
    """Who an AP reply goes to: the contact who wrote last about this invoice while
    still active, else the vendor's first active portal contact."""
    if invoice.vendor_id is None:
        return None
    last = await session.scalar(
        select(PayMessage.portal_contact_id)
        .where(
            PayMessage.invoice_id == invoice.id,
            PayMessage.direction == MessageDirection.INBOUND.value,
        )
        .order_by(PayMessage.created_at.desc())
        .limit(1)
    )
    active = select(PayPortalContact).where(
        PayPortalContact.vendor_id == invoice.vendor_id,
        PayPortalContact.status == ContactStatus.ACTIVE.value,
    )
    if last is not None:
        found = await session.scalar(active.where(PayPortalContact.id == last))
        if found is not None:
            return found
    return await session.scalar(active.order_by(PayPortalContact.created_at).limit(1))


@dataclass(frozen=True, slots=True)
class PortalReply:
    message: PayMessage
    # The template in force needs an approval first: the caller starts the run.
    needs_approval: bool


async def post_portal_reply(
    session: AsyncSession, actor: Actor, invoice_id: uuid.UUID, text: str
) -> PortalReply:
    """AP answers in the vendor portal thread of an invoice. Not sensitive by default:
    shown in the portal at once and emailed to the contact. When the organisation's
    version of ``pay.vendor_portal.reply`` requires an approval, it waits for one in the
    ``pay_message`` graph like an exception message (the writer never approves it)."""
    invoice = await get_invoice(session, invoice_id, lock=True)
    problems = check_message(text)
    if problems:
        raise BadRequestError("the message cannot be sent: " + ", ".join(problems))
    contact = await _portal_recipient(session, invoice)
    if contact is None:
        raise RecipientNotAllowedError("the vendor has no active portal contact")
    template = await resolve_template(
        session, key=PORTAL_REPLY, channel=CHANNEL, locale=contact.locale
    )
    if template is None:
        raise BadRequestError("no template for portal replies")
    now = datetime.now(UTC)
    message = PayMessage(
        id=uuid.uuid4(),
        org_id=invoice.org_id,
        reference=new_reference(REFERENCE_PREFIX),
        invoice_id=invoice.id,
        category="portal",
        template_id="portal_reply",
        recipient_kind=MessageRecipient.VENDOR.value,
        recipient_user_id=None,
        locale=contact.locale,
        ai_note=False,
        status=MessageStatus.SUBMITTED.value,
        submitted_by=actor.id,
        channel=MessageChannel.PORTAL.value,
        direction=MessageDirection.OUTBOUND.value,
        vendor_id=invoice.vendor_id,
        portal_contact_id=contact.id,
        created_at=now,
        updated_at=now,
    )
    message.recipient_name = _seal(message, "recipient_name", str(opened(contact, "name")))
    message.recipient_email = _seal(message, "recipient_email", str(opened(contact, "email")))
    message.message = _seal(message, "message", text.strip())
    session.add(message)
    await session.flush()
    await audit(
        session,
        actor,
        "pay.message.submitted",
        entity_type=ENTITY,
        entity_id=message.id,
        payload={
            "invoice_id": str(invoice.id),
            "channel": message.channel,
            "recipient_kind": message.recipient_kind,
            "portal_contact_id": str(contact.id),
            "needs_approval": template.requires_approval,
        },
    )
    if template.requires_approval:
        return PortalReply(message, needs_approval=True)
    results = await notify(
        session,
        recipient=await _recipient(session, message),
        template_key=PORTAL_REPLY,
        variables=await _variables(session, message),
        dedupe_key=f"pay.msg:{message.id}",
        channels=[CHANNEL],
        workflow=WORKFLOW,
        actor_type=ActorType.USER,
        actor_id=str(actor.id),
    )
    sent = next((r for r in results if r.channel == CHANNEL), None)
    message.status = MessageStatus.POSTED.value
    message.notification_id = sent.notification_id if sent is not None else None
    message.sent_at = message.updated_at = datetime.now(UTC)
    await _audit(
        session,
        "pay.message.posted",
        message,
        {"notification_id": str(message.notification_id) if message.notification_id else None},
    )
    return PortalReply(message, needs_approval=False)


# ------------------------------------------------------------------ the graph


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


async def _locked(session: AsyncSession, message_id: uuid.UUID) -> PayMessage:
    message = await session.scalar(
        select(PayMessage)
        .where(PayMessage.id == message_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if message is None:
        raise MessageNotFoundError()
    return message


async def _audit(
    session: AsyncSession, action: str, message: PayMessage, payload: dict[str, Any]
) -> None:
    await audit(
        session,
        By.system(ACTOR_ID),
        action,
        entity_type=ENTITY,
        entity_id=message.id,
        payload={"invoice_id": str(message.invoice_id), "status": message.status} | payload,
    )


async def _recipient(session: AsyncSession, message: PayMessage) -> Recipient:
    if message.recipient_kind == MessageRecipient.REQUESTER.value:
        if message.recipient_user_id is None:  # pragma: no cover - a check constraint
            raise ExecutorRefusedError("recipient_missing")
        return Recipient.user(message.recipient_user_id)
    email = opened(message, "recipient_email")
    if not isinstance(email, str) or not email:
        raise ExecutorRefusedError("recipient_missing")
    return Recipient.external(email, locale=message.locale)


def template_key(message: PayMessage) -> str:
    """The engine template a message is sent with: an exception message by email, or an
    AP reply in the vendor portal (``pay.vendor_portal.reply``)."""
    return PORTAL_REPLY if message.channel == MessageChannel.PORTAL.value else TEMPLATE_KEY


async def _variables(session: AsyncSession, message: PayMessage) -> dict[str, Any]:
    company = await session.scalar(
        select(Organisation.name).where(Organisation.id == message.org_id)
    )
    if message.channel == MessageChannel.PORTAL.value:
        invoice = await session.scalar(
            select(PayInvoice).where(PayInvoice.id == message.invoice_id)
        )
        return {
            "org_name": str(company or ""),
            "vendor_name": str(opened(message, "recipient_name") or ""),
            "invoice_reference": invoice.reference if invoice is not None else "",
            "invoice_number": invoice.invoice_number if invoice is not None else None,
            "message": str(opened(message, "message") or ""),
            "portal_url": absolute_url(public_web_url(), PORTAL_PATH),
        }
    return {
        "vendor_name": str(opened(message, "recipient_name") or ""),
        "company": str(company or ""),
        "message": str(opened(message, "message") or ""),
    }


async def propose(state: MessageState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    message_id = uuid.UUID(state["message_id"])
    async with ctx.transaction() as session:
        message = await _locked(session, message_id)
        if message.status != MessageStatus.SUBMITTED.value:
            return Command(goto=END)
        invoice = await session.scalar(
            select(PayInvoice).where(PayInvoice.id == message.invoice_id)
        )
        payload = await sensitive_message_payload(
            session,
            recipient=await _recipient(session, message),
            template_key=template_key(message),
            variables=await _variables(session, message),
            channel=CHANNEL,
        )
        reference = invoice.reference if invoice is not None else ""
        who = "vendor" if message.recipient_kind == MessageRecipient.VENDOR.value else "requester"
        title = f"Message to the {who} about invoice {reference}"
        if message.channel == MessageChannel.PORTAL.value:
            title = f"Reply to the vendor in the portal about invoice {reference}"
        context = {
            "kind": "pay.message",
            "invoice_reference": reference,
            "category": message.category,
            "template_id": message.template_id,
            "recipient_kind": message.recipient_kind,
            "ai_note": message.ai_note,
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
        async with ctx.transaction() as session:
            message = await _locked(session, message_id)
            message.status = MessageStatus.FAILED.value
            message.status_reason = f"approval_not_possible:{type(exc).__name__}"[:64]
            message.updated_at = datetime.now(UTC)
            await _audit(session, "pay.message.failed", message, {"reason": message.status_reason})
        return Command(goto=END)
    async with ctx.transaction() as session:
        message = await _locked(session, message_id)
        message.approval_id = uuid.UUID(approval_id)
        message.run_id = ctx.run_id
        message.status = MessageStatus.AWAITING_APPROVAL.value
        message.content_hash = message_hash(payload)
        message.updated_at = datetime.now(UTC)
        await _audit(
            session,
            "pay.message.awaiting_approval",
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


async def send_pay_message(session: AsyncSession, call: FinalActionCall) -> Mapping[str, Any]:
    """The final action: queue the approved email. Runs inside the executor's transaction,
    after it re-checked the approval; ``notify`` checks the payload hash once more."""
    message = await session.scalar(
        select(PayMessage).where(PayMessage.approval_id == call.approval_id).with_for_update()
    )
    if message is None:
        raise ExecutorRefusedError("message_not_found")
    if message.status != MessageStatus.AWAITING_APPROVAL.value:
        raise ExecutorRefusedError("message_not_awaiting_approval")
    try:
        results = await notify(
            session,
            recipient=await _recipient(session, message),
            template_key=template_key(message),
            variables=await _variables(session, message),
            dedupe_key=f"pay.msg:{message.id}",
            channels=[CHANNEL],
            approval_id=call.approval_id,
            registry=_registry(),
            workflow=WORKFLOW,
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR_ID,
        )
    except SensitiveTemplateError as exc:
        raise ExecutorRefusedError(f"sensitive_{exc.reason}") from None
    sent = next((r for r in results if r.channel == CHANNEL), None)
    if sent is None or sent.notification_id is None:
        raise ExecutorRefusedError("message_not_queued")
    now = datetime.now(UTC)
    message.status = MessageStatus.QUEUED.value
    message.notification_id = sent.notification_id
    message.sent_at = now
    message.updated_at = now
    await _audit(
        session,
        "pay.message.sent",
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
            message = await _locked(session, uuid.UUID(state["message_id"]))
            message.status = MessageStatus.FAILED.value
            message.status_reason = f"refused:{exc.reason}"[:64]
            message.updated_at = datetime.now(UTC)
            await _audit(session, "pay.message.failed", message, {"reason": message.status_reason})
        raise
    return {}


async def _close(ctx: WorkflowContext, state: MessageState, status: MessageStatus) -> None:
    async with ctx.transaction() as session:
        message = await _locked(session, uuid.UUID(state["message_id"]))
        if message.status != MessageStatus.AWAITING_APPROVAL.value:
            return
        message.status = status.value
        message.status_reason = state.get("outcome")
        message.updated_at = datetime.now(UTC)
        await _audit(
            session,
            f"pay.message.{status.value}",
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
