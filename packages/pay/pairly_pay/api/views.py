# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Turning Pay rows into responses, with what each viewer may see.

Never returned to anyone: a ticket's ``request_claims`` (the contact details the change
request carried) and the full IBAN of an active or retired account. The full proposed
IBAN and the number on file of a ticket go only to the people who verify or approve
it (they read the IBAN back to the vendor on the call; the verifier dials the number).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.models import Approval, ApprovalDecision, ApprovalStep
from pairly_core.rbac import Actor
from pairly_pay.api.schemas import (
    PayApprovalStepOut,
    PayBankAccountOut,
    PayCallbackOut,
    PayCallOut,
    PayContactOut,
    PayDuplicateOut,
    PayTicketDetail,
    PayTicketSummary,
    PayVendorDetail,
    PayVendorSummary,
    PayVerificationOut,
)
from pairly_pay.bank_changes import (
    callback_reference_of,
    calls_of,
    needs_callback_source,
    open_ticket_of,
    verify_phone_of,
)
from pairly_pay.common import opened
from pairly_pay.holds import is_sticky
from pairly_pay.models import (
    OPEN_TICKET_STATUSES,
    AccountStatus,
    PayBankChangeTicket,
    PayVendor,
    PayVendorBankAccount,
    TicketStatus,
    VendorStatus,
)
from pairly_pay.permissions import (
    BANK_CHANGE_APPROVE,
    BANK_CHANGE_CREATE,
    BANK_CHANGE_READ,
    BANK_CHANGE_VERIFY,
    HOLD_MANAGE,
    VENDOR_WRITE,
)
from pairly_pay.vendors import (
    account_name_of,
    accounts_of,
    active_account,
    contacts_of,
    find_duplicates,
    iban_of,
    masked_account,
    phone_of,
    risk_profile,
)


def account_out(account: PayVendorBankAccount) -> PayBankAccountOut:
    return PayBankAccountOut(
        id=account.id,
        status=account.status,
        iban_masked=masked_account(account),
        country=account.country,
        bank_code=account.bank_code,
        bank_name=account.bank_name,
        swift=account.swift,
        account_name=account_name_of(account),
        created_at=account.created_at,
        activated_at=account.activated_at,
        retired_at=account.retired_at,
    )


async def vendor_summary(session: AsyncSession, vendor: PayVendor) -> PayVendorSummary:
    account = await active_account(session, vendor.id)
    ticket = await open_ticket_of(session, vendor.id)
    return _summary(vendor, account, ticket.id if ticket else None)


async def vendor_summaries(
    session: AsyncSession, vendors: Sequence[PayVendor]
) -> list[PayVendorSummary]:
    """``vendor_summary`` for a page of vendors in two queries (active accounts, open
    tickets), never two per vendor."""
    ids = [v.id for v in vendors]
    if not ids:
        return []
    accounts = {
        a.vendor_id: a
        for a in await session.scalars(
            select(PayVendorBankAccount).where(
                PayVendorBankAccount.vendor_id.in_(ids),
                PayVendorBankAccount.status == AccountStatus.ACTIVE.value,
            )
        )
    }
    tickets = dict(
        (
            await session.execute(
                select(PayBankChangeTicket.vendor_id, PayBankChangeTicket.id).where(
                    PayBankChangeTicket.vendor_id.in_(ids),
                    PayBankChangeTicket.status.in_([s.value for s in OPEN_TICKET_STATUSES]),
                )
            )
        )
        .tuples()
        .all()
    )
    return [_summary(v, accounts.get(v.id), tickets.get(v.id)) for v in vendors]


def _summary(
    vendor: PayVendor, account: PayVendorBankAccount | None, ticket_id: uuid.UUID | None
) -> PayVendorSummary:
    return PayVendorSummary(
        id=vendor.id,
        reference=vendor.reference,
        legal_name=vendor.legal_name,
        trade_name=vendor.trade_name,
        country=vendor.country,
        trn=vendor.trn,
        status=vendor.status,
        source=vendor.source,
        payment_hold=vendor.payment_hold,
        hold_reason=vendor.hold_reason,
        iban_masked=masked_account(account) if account else None,
        open_ticket_id=ticket_id,
        review_reasons=list(vendor.review_reasons),
        created_at=vendor.created_at,
    )


async def ticket_summary(
    session: AsyncSession, ticket: PayBankChangeTicket, vendor: PayVendor | None = None
) -> PayTicketSummary:
    vendor = vendor or await session.scalar(
        select(PayVendor).where(PayVendor.id == ticket.vendor_id)
    )
    proposed = await session.scalar(
        select(PayVendorBankAccount).where(PayVendorBankAccount.id == ticket.proposed_account_id)
    )
    return PayTicketSummary(
        id=ticket.id,
        reference=ticket.reference,
        vendor_id=ticket.vendor_id,
        vendor_name=vendor.legal_name if vendor else "",
        vendor_reference=vendor.reference if vendor else "",
        status=ticket.status,
        source=ticket.source,
        new_iban_masked=masked_account(proposed) if proposed else "",
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        activates_at=ticket.activates_at,
        outcome=ticket.outcome,
    )


async def vendor_detail(session: AsyncSession, actor: Actor, vendor: PayVendor) -> PayVendorDetail:
    summary = await vendor_summary(session, vendor)
    tickets: list[PayTicketSummary] = []
    if actor.has(BANK_CHANGE_READ):
        rows = await session.scalars(
            select(PayBankChangeTicket)
            .where(PayBankChangeTicket.vendor_id == vendor.id)
            .order_by(PayBankChangeTicket.created_at.desc())
            .limit(20)
        )
        tickets = [await ticket_summary(session, t, vendor) for t in rows]
    duplicates: list[PayDuplicateOut] = []
    if vendor.status == VendorStatus.PENDING.value:
        accounts = await accounts_of(session, vendor.id)
        proposed = next((a for a in accounts if a.status == "proposed"), None)
        hints = await find_duplicates(
            session,
            vendor.org_id,
            legal_name=vendor.legal_name,
            trade_name=vendor.trade_name,
            trn=vendor.trn,
            iban=iban_of(proposed) if proposed else None,
            exclude=vendor.id,
        )
        duplicates = [PayDuplicateOut.model_validate(h.as_json()) for h in hints]
    contacts = [
        PayContactOut.model_validate({k: c.get(k) for k in ("name", "role", "email", "phone")})
        for c in contacts_of(vendor)
        if isinstance(c, dict) and c.get("name")
    ]
    open_vendor = vendor.status in {VendorStatus.ACTIVE.value, VendorStatus.PENDING.value}
    return PayVendorDetail(
        **summary.model_dump(),
        legal_name_ar=vendor.legal_name_ar,
        trade_licence=vendor.trade_licence,
        trn_status=vendor.trn_status,
        address=dict(vendor.address),
        contacts=contacts,
        phone_on_file=phone_of(vendor),
        phone_changed_at=vendor.phone_changed_at,
        email_domains=list(vendor.email_domains),
        category=vendor.category,
        payment_terms=vendor.payment_terms,
        document_ids=list(vendor.document_ids),
        hold_set_at=vendor.hold_set_at,
        hold_sticky=is_sticky(vendor),
        accounts=[account_out(a) for a in await accounts_of(session, vendor.id)],
        risk=await risk_profile(session, vendor),
        tickets=tickets,
        duplicates=duplicates,
        activated_at=vendor.activated_at,
        can_write=actor.has(VENDOR_WRITE) and open_vendor,
        can_request_change=actor.has(BANK_CHANGE_CREATE)
        and vendor.status == VendorStatus.ACTIVE.value
        and summary.open_ticket_id is None,
        can_manage_hold=actor.has(HOLD_MANAGE),
    )


async def _approval_view(
    session: AsyncSession, approval_id: uuid.UUID | None
) -> tuple[str | None, list[PayApprovalStepOut]]:
    if approval_id is None:
        return None, []
    approval = await session.scalar(select(Approval).where(Approval.id == approval_id))
    if approval is None:
        return None, []
    steps = list(
        await session.scalars(
            select(ApprovalStep)
            .where(ApprovalStep.approval_id == approval_id)
            .order_by(ApprovalStep.position)
        )
    )
    decisions = list(
        await session.scalars(
            select(ApprovalDecision).where(ApprovalDecision.approval_id == approval_id)
        )
    )
    out = [
        PayApprovalStepOut(
            name=s.name,
            status=s.status,
            decided_by=[d.actor_user_id for d in decisions if d.step_id == s.id],
        )
        for s in steps
    ]
    return approval.status, out


def _str(value: Any) -> str | None:
    return str(value) if value is not None else None


async def ticket_detail(
    session: AsyncSession, actor: Actor, ticket: PayBankChangeTicket
) -> PayTicketDetail:
    vendor = await session.scalar(select(PayVendor).where(PayVendor.id == ticket.vendor_id))
    if vendor is None:  # pragma: no cover - a foreign key
        raise LookupError("vendor missing")
    summary = await ticket_summary(session, ticket, vendor)
    proposed = await session.scalar(
        select(PayVendorBankAccount).where(PayVendorBankAccount.id == ticket.proposed_account_id)
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
    if proposed is None:  # pragma: no cover - a foreign key
        raise LookupError("proposed account missing")
    verifier = actor.has(BANK_CHANGE_VERIFY)
    approver = actor.has(BANK_CHANGE_APPROVE)
    insider = verifier or approver
    phone = verify_phone_of(ticket)
    calls = [
        PayCallOut(
            id=c.id,
            result=c.result,
            called_by=c.called_by,
            number_last4=c.number_last4,
            notes=_str(opened(c, "notes")) if insider else None,
            created_at=c.created_at,
        )
        for c in await calls_of(session, ticket.id)
    ]
    approval_status, steps = await _approval_view(session, ticket.approval_id)
    awaiting_call = ticket.status == TicketStatus.AWAITING_VERIFICATION.value
    is_requester = ticket.requester_user_id is not None and ticket.requester_user_id == actor.id
    # Recorded the number to call (M-04): someone else calls it.
    recorded_phone = ticket.phone_set_by is not None and ticket.phone_set_by == actor.id
    can_cancel = (awaiting_call and verifier) or (
        ticket.status == TicketStatus.COOLING_OFF.value and approver
    )
    return PayTicketDetail(
        **summary.model_dump(),
        requested_by_type=ticket.requested_by_type,
        requested_by_id=ticket.requested_by_id,
        requester_user_id=ticket.requester_user_id,
        note=_str(opened(ticket, "note")) if insider or actor.has(BANK_CHANGE_CREATE) else None,
        request_had_contact_details=ticket.request_claims is not None,
        previous_account=account_out(previous) if previous else None,
        proposed_account=account_out(proposed),
        proposed_iban=iban_of(proposed) if insider else None,
        verification=PayVerificationOut(
            phone_on_file=phone if verifier else None,
            phone_last4=phone[-4:],
            phone_recently_changed=ticket.phone_recently_changed,
            callback=PayCallbackOut(
                source=ticket.callback_source,
                reference=callback_reference_of(ticket) if insider else None,
                recorded_by=ticket.callback_recorded_by,
                matches_claim=ticket.callback_matches_claim,
            )
            if ticket.callback_source
            else None,
            callback_required=needs_callback_source(ticket),
            verified_by=ticket.verified_by,
            verified_at=ticket.verified_at,
            calls=calls,
        ),
        approval_id=ticket.approval_id,
        approval_status=approval_status,
        approval_steps=steps,
        approver_ids=[uuid.UUID(a) for a in ticket.approver_ids],
        approved_at=ticket.approved_at,
        cooling_off_hours=ticket.cooling_off_hours,
        completed_at=ticket.completed_at,
        closed_at=ticket.closed_at,
        hold_kept=ticket.hold_kept,
        vendor_payment_hold=vendor.payment_hold,
        vendor_hold_reason=vendor.hold_reason,
        decision_ids=list(ticket.decision_ids),
        can_verify=awaiting_call
        and verifier
        and not is_requester
        and not recorded_phone
        and not needs_callback_source(ticket),
        can_cancel=bool(can_cancel),
        can_start_approval=ticket.status == TicketStatus.AWAITING_APPROVAL.value
        and ticket.approval_id is None
        and verifier
        and not is_requester
        and not recorded_phone,
    )
