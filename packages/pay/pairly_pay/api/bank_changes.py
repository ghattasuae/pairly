# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Bank-detail change routes (WP4.3, the signature control; see
``pairly_pay.bank_changes`` for the rules).

* ``POST /vendors/{id}/bank-changes``   open a locked ticket (``pay.bank_change.create``):
  payments are held at once. Contact details the request carried go in
  ``request_contact``: stored encrypted for investigation, never shown or used.
* ``GET  /bank-changes?status=&vendor_id=``  list (``pay.bank_change.read``)
* ``GET  /bank-changes/{id}``           the ticket with the verification checklist. The
  on-file number shows only to people who verify; the full new IBAN only to people
  who verify or approve.
* ``POST /bank-changes/{id}/calls``     record the call back on the number on file
  (``pay.bank_change.verify``, a second factor; never the requester, nor the person who
  recorded that number while it is recent or the vendor new, 403
  ``segregation_of_duties``, audited). ``confirmed``
  starts the dual approval run; ``not_confirmed`` rejects the change and keeps the hold.
* ``POST /bank-changes/{id}/callback-number``  ``{"phone", "source", "reference"}``: the
  number to call for an onboarding change that has none from an independent source yet
  (``pay.bank_change.create``); until then the change cannot be verified (409
  ``callback_source_missing``; security review 2026-10, M-05)
* ``POST /bank-changes/{id}/start-approval``  start the approval run again when it did
  not start (same rules as recording the call)
* ``POST /bank-changes/{id}/cancel``    cancel while waiting for the call
  (``pay.bank_change.verify``) or during cooling-off (``pay.bank_change.approve``),
  with a reason and whether to keep the payment hold

The approvals themselves happen in the inbox (``/v1/approvals``), approval type
``pay.bank_change``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Query
from sqlalchemy import select

from pairly_api.routes.approvals import Runtime
from pairly_core.errors import ConflictError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import Actor, Denial, authorize
from pairly_core.workflow.models import TERMINAL_RUN_STATUSES, WorkflowRun
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_pay.api.schemas import (
    PayBankChangeIn,
    PayCallbackIn,
    PayCallIn,
    PayCancelIn,
    PayTicketDetail,
    PayTicketList,
)
from pairly_pay.api.vendors import person
from pairly_pay.api.views import ticket_detail, ticket_summary
from pairly_pay.bank_changes import (
    SegregationOfDutiesError,
    TicketStateError,
    cancel_ticket,
    get_ticket,
    open_ticket,
    record_call,
    record_callback_number,
    refuse_phone_recorder,
)
from pairly_pay.common import TICKET_ENTITY, By, clean_text
from pairly_pay.graph import GRAPH
from pairly_pay.models import (
    CallbackSource,
    CallResult,
    PayBankChangeTicket,
    TicketSource,
    TicketStatus,
)
from pairly_pay.permissions import (
    BANK_CHANGE_APPROVE,
    BANK_CHANGE_CREATE,
    BANK_CHANGE_READ,
    BANK_CHANGE_VERIFY,
)
from pairly_pay.vendors import BankInput, InvalidPhoneError, check_bank, check_phone, get_vendor

log = structlog.get_logger("pairly_pay.api.bank_changes")

router = APIRouter(tags=["pay"])


async def _start_run(runtime: WorkflowRuntime, actor: Actor, ticket_id: uuid.UUID) -> None:
    """Start the dual approval run as the verifier. A failure leaves the ticket
    ``awaiting_approval`` without an approval; ``start-approval`` tries again."""
    try:
        await runtime.start_run(
            actor,
            workflow="pay",
            graph=GRAPH,
            input={"ticket_id": str(ticket_id)},
            entity_type=TICKET_ENTITY,
            entity_id=str(ticket_id),
        )
    except Exception as exc:
        log.warning(
            "pay_bank_change_run_not_started", ticket_id=str(ticket_id), error=type(exc).__name__
        )


async def _fresh(session: OrgSession, actor: Actor, ticket_id: uuid.UUID) -> PayTicketDetail:
    ticket = await session.scalar(
        select(PayBankChangeTicket)
        .where(PayBankChangeTicket.id == ticket_id)
        .execution_options(populate_existing=True)
    )
    if ticket is None:  # pragma: no cover - read right after writing it
        raise LookupError("ticket missing")
    return await ticket_detail(session, actor, ticket)


@router.post("/vendors/{vendor_id}/bank-changes", status_code=201, response_model=PayTicketDetail)
async def request_change(
    vendor_id: uuid.UUID, body: PayBankChangeIn, actor: CurrentActor, session: OrgSession
) -> PayTicketDetail:
    authorize(actor, BANK_CHANGE_CREATE)
    person(actor)
    await get_vendor(session, vendor_id)
    bank = check_bank(BankInput(**body.bank.model_dump()))
    claims = body.request_contact.model_dump(exclude_none=True) if body.request_contact else None
    opened = await open_ticket(
        session,
        vendor_id,
        bank,
        source=TicketSource(body.source),
        by=By.of(actor),
        note=body.note,
        claims=claims,
    )
    return await ticket_detail(session, actor, opened.ticket)


@router.get("/bank-changes", response_model=PayTicketList)
async def list_changes(
    actor: CurrentActor,
    session: OrgSession,
    status: Annotated[
        Literal[
            "awaiting_verification",
            "awaiting_approval",
            "cooling_off",
            "completed",
            "rejected",
            "cancelled",
            "failed",
        ]
        | None,
        Query(),
    ] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    open_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> PayTicketList:
    authorize(actor, BANK_CHANGE_READ)
    query = select(PayBankChangeTicket).order_by(
        PayBankChangeTicket.created_at.desc(), PayBankChangeTicket.id
    )
    if status is not None:
        query = query.where(PayBankChangeTicket.status == TicketStatus(status).value)
    if open_only:
        query = query.where(
            PayBankChangeTicket.status.in_(
                [
                    TicketStatus.AWAITING_VERIFICATION.value,
                    TicketStatus.AWAITING_APPROVAL.value,
                    TicketStatus.COOLING_OFF.value,
                ]
            )
        )
    if vendor_id is not None:
        query = query.where(PayBankChangeTicket.vendor_id == vendor_id)
    rows = list(await session.scalars(query.limit(limit)))
    return PayTicketList(items=[await ticket_summary(session, t) for t in rows])


@router.get("/bank-changes/{ticket_id}", response_model=PayTicketDetail)
async def get_change(
    ticket_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> PayTicketDetail:
    authorize(actor, BANK_CHANGE_READ)
    return await ticket_detail(session, actor, await get_ticket(session, ticket_id))


@router.post("/bank-changes/{ticket_id}/calls", response_model=PayTicketDetail)
async def record(
    ticket_id: uuid.UUID,
    body: PayCallIn,
    actor: CurrentActor,
    session: OrgSession,
    runtime: Runtime,
) -> PayTicketDetail:
    authorize(actor, BANK_CHANGE_VERIFY)
    person(actor)
    ticket, _ = await record_call(
        session,
        actor,
        ticket_id,
        result=CallResult(body.result),
        notes=body.notes,
        attested=body.called_number_on_file,
    )
    started = ticket.status == TicketStatus.AWAITING_APPROVAL.value
    # The run reads the ticket: commit before it starts.
    await session.commit()
    if started:
        await _start_run(runtime, actor, ticket_id)
    return await _fresh(session, actor, ticket_id)


@router.post("/bank-changes/{ticket_id}/callback-number", response_model=PayTicketDetail)
async def callback_number(
    ticket_id: uuid.UUID, body: PayCallbackIn, actor: CurrentActor, session: OrgSession
) -> PayTicketDetail:
    """The number to call for an onboarding change opened without one from an
    independent source (M-05): it becomes the number on file and the only one to call;
    whoever records it does not verify."""
    authorize(actor, BANK_CHANGE_CREATE)
    person(actor)
    phone = check_phone(body.phone)
    if phone is None:
        raise InvalidPhoneError("phone_format")
    ticket = await record_callback_number(
        session,
        actor,
        ticket_id,
        phone=phone,
        source=CallbackSource(body.source),
        reference=clean_text(body.reference, 500),
    )
    return await ticket_detail(session, actor, ticket)


@router.post("/bank-changes/{ticket_id}/start-approval", response_model=PayTicketDetail)
async def start_approval(
    ticket_id: uuid.UUID, actor: CurrentActor, session: OrgSession, runtime: Runtime
) -> PayTicketDetail:
    authorize(actor, BANK_CHANGE_VERIFY)
    person(actor)
    ticket = await get_ticket(session, ticket_id, lock=True)
    if ticket.status != TicketStatus.AWAITING_APPROVAL.value or ticket.approval_id is not None:
        raise TicketStateError("the approval already started or the change is not verified")
    if ticket.requester_user_id == actor.id:
        raise SegregationOfDutiesError(
            "the person who asked for a change cannot start its approval",
            denial=Denial(
                actor=actor, permission=BANK_CHANGE_VERIFY, reason="segregation_of_duties"
            ),
        )
    refuse_phone_recorder(ticket, actor, "start its approval")
    if ticket.run_id is not None:
        # A run owns it; only a run that ended may be replaced.
        run = await session.scalar(select(WorkflowRun).where(WorkflowRun.id == ticket.run_id))
        if run is not None and run.status not in {s.value for s in TERMINAL_RUN_STATUSES}:
            raise ConflictError("the approval run is still going")
        ticket.run_id = None
    await session.commit()
    await _start_run(runtime, actor, ticket_id)
    return await _fresh(session, actor, ticket_id)


@router.post("/bank-changes/{ticket_id}/cancel", response_model=PayTicketDetail)
async def cancel(
    ticket_id: uuid.UUID, body: PayCancelIn, actor: CurrentActor, session: OrgSession
) -> PayTicketDetail:
    person(actor)
    ticket = await get_ticket(session, ticket_id)
    if ticket.status == TicketStatus.COOLING_OFF.value:
        authorize(actor, BANK_CHANGE_APPROVE)
    else:
        authorize(actor, BANK_CHANGE_VERIFY)
    ticket = await cancel_ticket(
        session, actor, ticket_id, reason=body.reason, keep_hold=body.keep_hold
    )
    return await ticket_detail(session, actor, ticket)
