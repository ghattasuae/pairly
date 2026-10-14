# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The bank-detail change workflow, the signature control of Pairly Pay (brief 4.1.4).

Lifecycle of a change ticket (``TicketStatus``)::

    open_ticket ------------------------------> awaiting_verification   (hold placed)
    record_call: no_answer -------------------> awaiting_verification   (try again)
    record_call: not_confirmed ---------------> rejected                (hold kept, sticky)
    record_call: confirmed -------------------> awaiting_approval       (run starts)
    approval approved (two people) -----------> cooling_off | completed
    approval rejected / changes requested ----> rejected                (hold per rule)
    nobody may approve -----------------------> failed                  (hold kept, sticky)
    cooling-off over (activate_due) ----------> completed               (hold per rule)
    cancel (awaiting_verification|cooling_off)> cancelled               (hold per choice)

Rules, enforced here and again in the final action:

* **Locked ticket.** The proposed details are a new ``proposed`` bank account row that
  nobody can edit (a database trigger guards it), bound to the ticket by a keyed digest
  (``details_digest``, HMAC under the blind index key, so the digest reveals nothing).
  The approval payload carries the digest; the final action and the activation decrypt
  the proposed account again and refuse when it no longer matches. One open ticket per
  vendor (a unique index); a second request while one is open is refused (staff) or
  joins the open one (``open_or_join``, for invoice checks).
* **Hold.** Opening a ticket places the vendor's payment hold in the same transaction
  (``pairly_pay.holds`` has the full rule for lifting it).
* **Out of band, on the number on file.** The ticket snapshots the vendor's phone number
  on file when it opens; a vendor without one cannot get a ticket. The verifier sees
  only that number, and a call record always stores that number (its last four
  digits). Whatever contact details the request itself carried (``request_claims``) are
  stored encrypted for investigation and never shown, returned or used. The number on
  file cannot change while a ticket is open (``pairly_pay.api``). A number that changed
  within the settings window before the ticket opened flags the ticket
  (``phone_recently_changed``) for the verifier and the approvers.
* **Segregation of duties** (four eyes twice):

  1. the verifier is never the requester;
  2. neither the requester nor the verifier may approve, at any step, directly or as a
     delegate: both are preparers of the approval, so the approvals engine never
     assigns them and refuses their decision;
  3. two approvals by two different people: the ``pay.bank_change`` rules have two
     steps and the engine never lets one person decide two steps of a chain;
  4. the final action re-checks 2 and 3 from the decision rows (at least two distinct
     approvers, none of them the requester or the verifier), whatever the rules say,
     and refuses otherwise (fail closed).

  A change a person requested therefore needs four different people; a change opened
  by the system (an invoice check) or the vendor needs three.
* **Cooling-off.** The ticket keeps the organisation's cooling-off hours from when it
  opened. After the dual approval the new details wait that long (``cooling_off``,
  ``activates_at``) and anyone with ``pay.bank_change.approve`` may still cancel; the
  worker's ``pay_bank_change_activation`` job then activates them. Zero hours activates
  at once, inside the final action.
* **Rejection** keeps the old details (the proposed row becomes ``rejected``) and lifts
  the ticket's hold unless another ticket is open or a sticky reason applies.

Every step writes an audit event and a decision record (``record_step``); the approvals
engine adds its own records for each approval decision and for the final action.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.executor import ExecutorRefusedError
from pairly_core.approvals.models import ApprovalDecision
from pairly_core.crypto.blind_index import blind_index, blind_index_candidates
from pairly_core.crypto.types import seal
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.rbac import Actor, Denial, PermissionDeniedError
from pairly_core.workflow.spec import FinalActionCall
from pairly_pay.common import (
    TICKET_ENTITY,
    VENDOR_ENTITY,
    By,
    audit,
    decision_ids_of,
    new_reference,
    opened,
    record_step,
)
from pairly_pay.holds import STICKY, is_sticky, place_hold, recompute_hold
from pairly_pay.models import (
    OPEN_TICKET_STATUSES,
    AccountStatus,
    CallResult,
    HoldReason,
    PayBankChangeCall,
    PayBankChangeTicket,
    PayVendor,
    PayVendorBankAccount,
    TicketSource,
    TicketStatus,
    VendorStatus,
)
from pairly_pay.permissions import BANK_CHANGE_VERIFY
from pairly_pay.vendors import (
    CheckedBank,
    account_name_of,
    active_account,
    get_vendor,
    iban_of,
    new_account,
    phone_of,
    settings_of,
)

FINAL_ACTION: Final = "approve_bank_change"
MAX_NOTE: Final = 2000
MAX_CLAIMS_BYTES: Final = 8 * 1024


class TicketNotFoundError(NotFoundError):
    code = "bank_change_not_found"
    title = "Bank-detail change not found"


class TicketAlreadyOpenError(ConflictError):
    code = "bank_change_already_open"
    title = "This vendor already has an open bank-detail change"

    def __init__(self, ticket_id: uuid.UUID) -> None:
        super().__init__("finish or cancel the open change first")
        self.ticket_id = ticket_id


class NoPhoneOnFileError(ConflictError):
    code = "no_phone_on_file"
    title = "The vendor has no phone number on file to call back"


class SameDetailsError(ConflictError):
    code = "bank_details_unchanged"
    title = "These are already the vendor's bank details"


class TicketStateError(ConflictError):
    code = "bank_change_state"
    title = "The change cannot take this step now"


class VendorNotOpenError(ConflictError):
    code = "vendor_not_open"
    title = "The vendor is rejected or archived"


class SegregationOfDutiesError(PermissionDeniedError):
    code = "segregation_of_duties"
    title = "Segregation of duties"


# ------------------------------------------------------------------ the locked details


def _canonical(iban: str, account_name: str, bank_name: str | None, swift: str | None) -> str:
    return json.dumps(
        {"account_name": account_name, "bank_name": bank_name, "iban": iban, "swift": swift},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def details_digest(
    org_id: uuid.UUID, iban: str, account_name: str, bank_name: str | None, swift: str | None
) -> str:
    """Keyed digest of a set of bank details (HMAC under the blind index key)."""
    digest = blind_index("text", _canonical(iban, account_name, bank_name, swift), org_id)
    if digest is None:  # pragma: no cover - the canonical JSON is never empty
        raise ValueError("cannot digest empty details")
    return digest


def details_match(digest: str, account: PayVendorBankAccount) -> bool:
    """Whether ``account`` decrypts to the details ``digest`` was made of (any
    configured blind index key version)."""
    try:
        canonical = _canonical(
            iban_of(account), account_name_of(account), account.bank_name, account.swift
        )
    except Exception:
        return False
    return digest in blind_index_candidates("text", canonical, account.org_id)


def digest_matches(ticket: PayBankChangeTicket, account: PayVendorBankAccount) -> bool:
    """Whether the stored proposed account still decrypts to the details the ticket was
    opened with (any configured blind index key version)."""
    return account.org_id == ticket.org_id and details_match(ticket.details_digest, account)


# ------------------------------------------------------------------ reading


async def get_ticket(
    session: AsyncSession, ticket_id: uuid.UUID, *, lock: bool = False
) -> PayBankChangeTicket:
    query = select(PayBankChangeTicket).where(PayBankChangeTicket.id == ticket_id)
    if lock:
        query = query.with_for_update()
    ticket = await session.scalar(query)
    if ticket is None:
        raise TicketNotFoundError()
    return ticket


async def open_ticket_of(session: AsyncSession, vendor_id: uuid.UUID) -> PayBankChangeTicket | None:
    return await session.scalar(
        select(PayBankChangeTicket).where(
            PayBankChangeTicket.vendor_id == vendor_id,
            PayBankChangeTicket.status.in_([s.value for s in OPEN_TICKET_STATUSES]),
        )
    )


async def _account(
    session: AsyncSession, account_id: uuid.UUID | None
) -> PayVendorBankAccount | None:
    if account_id is None:
        return None
    return await session.scalar(
        select(PayVendorBankAccount).where(PayVendorBankAccount.id == account_id)
    )


def verify_phone_of(ticket: PayBankChangeTicket) -> str:
    """The only number anyone may call for this ticket: the one on file at opening."""
    return str(opened(ticket, "verify_phone"))


async def calls_of(session: AsyncSession, ticket_id: uuid.UUID) -> list[PayBankChangeCall]:
    return list(
        await session.scalars(
            select(PayBankChangeCall)
            .where(PayBankChangeCall.ticket_id == ticket_id)
            .order_by(PayBankChangeCall.created_at, PayBankChangeCall.id)
        )
    )


async def refresh_decisions(session: AsyncSession, ticket: PayBankChangeTicket) -> None:
    ticket.decision_ids = await decision_ids_of(session, TICKET_ENTITY, ticket.id)


# ------------------------------------------------------------------ opening


@dataclass(frozen=True, slots=True)
class Opened:
    ticket: PayBankChangeTicket
    created: bool


def _clean_claims(claims: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What the request said about how to reach the vendor, kept but never used."""
    if not claims:
        return None
    clean = {str(k)[:40]: str(v)[:500] for k, v in list(claims.items())[:20] if v not in (None, "")}
    if not clean:
        return None
    raw = json.dumps(clean, ensure_ascii=False)
    if len(raw.encode("utf-8")) > MAX_CLAIMS_BYTES:
        raise BadRequestError("the request details are too long")
    return clean


async def open_ticket(
    session: AsyncSession,
    vendor_id: uuid.UUID,
    bank: CheckedBank,
    *,
    source: TicketSource,
    by: By,
    note: str | None = None,
    claims: Mapping[str, Any] | None = None,
    join_open: bool = False,
    reuse_account_id: uuid.UUID | None = None,
) -> Opened:
    """Open a locked change ticket and hold the vendor's payments, in one transaction.

    ``join_open`` (for automatic sources such as an invoice whose IBAN differs from the
    vendor master): when a ticket is already open, record the new request on it in the
    audit log and return it instead of refusing.

    ``reuse_account_id``: the vendor's own ``proposed`` account to put through the ticket
    (details a vendor sent with its onboarding form) instead of a new row.
    """
    now = datetime.now(UTC)
    vendor = await get_vendor(session, vendor_id, lock=True)
    if vendor.status in {VendorStatus.REJECTED.value, VendorStatus.ARCHIVED.value}:
        raise VendorNotOpenError()
    existing = await open_ticket_of(session, vendor.id)
    if existing is not None:
        if not join_open:
            raise TicketAlreadyOpenError(existing.id)
        await audit(
            session,
            by,
            "pay.bank_change.request_joined",
            entity_type=TICKET_ENTITY,
            entity_id=existing.id,
            payload={"source": source.value, "iban_last4": bank.last4},
        )
        # Make sure the vendor is held whatever happened before (fail closed).
        await place_hold(session, vendor, HoldReason.BANK_CHANGE, by, now)
        return Opened(existing, created=False)
    phone = phone_of(vendor)
    if not phone:
        raise NoPhoneOnFileError("add the vendor's phone number on file first")
    current = await active_account(session, vendor.id)
    proposed_bidx = blind_index_candidates("iban", bank.iban, vendor.org_id)
    if (
        current is not None
        and current.iban_bidx in proposed_bidx
        and account_name_of(current) == bank.account_name
        and current.bank_name == bank.bank_name
        and current.swift == bank.swift
    ):
        raise SameDetailsError()
    settings = await settings_of(session)
    window = timedelta(days=settings.phone_change_window_days)
    recent_phone = vendor.phone_changed_at is not None and now - vendor.phone_changed_at <= window

    if reuse_account_id is not None:
        account = await _account(session, reuse_account_id)
        if (
            account is None
            or account.vendor_id != vendor.id
            or account.status != AccountStatus.PROPOSED.value
        ):
            raise TicketStateError("the proposed details are not this vendor's")
    else:
        account = new_account(vendor, bank, AccountStatus.PROPOSED, by, now)
        session.add(account)
        await session.flush()
    ticket_id = uuid.uuid4()
    clean_claims = _clean_claims(claims)
    ticket = PayBankChangeTicket(
        id=ticket_id,
        org_id=vendor.org_id,
        reference=new_reference("BCT"),
        vendor_id=vendor.id,
        status=TicketStatus.AWAITING_VERIFICATION.value,
        source=source.value,
        requested_by_type=by.actor_type.value,
        requested_by_id=by.actor_id,
        requester_user_id=by.user_id,
        previous_account_id=current.id if current is not None else None,
        proposed_account_id=account.id,
        details_digest=details_digest(
            vendor.org_id, bank.iban, bank.account_name, bank.bank_name, bank.swift
        ),
        note=seal(PayBankChangeTicket, "note", note, org_id=vendor.org_id, row_id=ticket_id)
        if note
        else None,
        request_claims=seal(
            PayBankChangeTicket,
            "request_claims",
            clean_claims,
            org_id=vendor.org_id,
            row_id=ticket_id,
        )
        if clean_claims
        else None,
        verify_phone=seal(
            PayBankChangeTicket, "verify_phone", phone, org_id=vendor.org_id, row_id=ticket_id
        ),
        verify_phone_bidx=vendor.phone_bidx,
        phone_recently_changed=recent_phone,
        cooling_off_hours=settings.cooling_off_hours,
        approver_ids=[],
        decision_ids=[],
        created_at=now,
        updated_at=now,
    )
    session.add(ticket)
    await session.flush()
    await place_hold(session, vendor, HoldReason.BANK_CHANGE, by, now)
    payload = {
        "reference": ticket.reference,
        "vendor_id": str(vendor.id),
        "source": source.value,
        "previous_account_id": str(current.id) if current else None,
        "proposed_account_id": str(account.id),
        "iban_last4": bank.last4,
        "phone_recently_changed": recent_phone,
        "cooling_off_hours": ticket.cooling_off_hours,
        "request_carried_contact_details": clean_claims is not None,
    }
    await audit(
        session,
        by,
        "pay.bank_change.opened",
        entity_type=TICKET_ENTITY,
        entity_id=ticket.id,
        payload=payload,
    )
    await record_step(
        session,
        by,
        entity_type=TICKET_ENTITY,
        entity_id=ticket.id,
        action="bank_change_requested",
        inputs={
            "vendor_id": str(vendor.id),
            "source": source.value,
            "details_digest": ticket.details_digest,
        },
        output={"status": ticket.status, "payment_hold": True, "hold_reason": vendor.hold_reason},
        reason=note,
    )
    await refresh_decisions(session, ticket)
    return Opened(ticket, created=True)


# ------------------------------------------------------------------ verification


async def record_call(
    session: AsyncSession,
    actor: Actor,
    ticket_id: uuid.UUID,
    *,
    result: CallResult,
    notes: str | None,
    attested: bool,
) -> tuple[PayBankChangeTicket, PayBankChangeCall]:
    """Record a call back on the number on file. The caller holds
    ``pay.bank_change.verify`` (checked by the route, with its second factor)."""
    if not attested:
        raise BadRequestError("confirm that you called the number on file shown here")
    ticket = await get_ticket(session, ticket_id, lock=True)
    if ticket.status != TicketStatus.AWAITING_VERIFICATION.value:
        raise TicketStateError("this change is not waiting for a call")
    if ticket.requester_user_id is not None and ticket.requester_user_id == actor.id:
        raise SegregationOfDutiesError(
            "the person who asked for a change cannot verify it",
            denial=Denial(
                actor=actor, permission=BANK_CHANGE_VERIFY, reason="segregation_of_duties"
            ),
        )
    now = datetime.now(UTC)
    by = By.of(actor)
    number = verify_phone_of(ticket)
    call_id = uuid.uuid4()
    call = PayBankChangeCall(
        id=call_id,
        org_id=ticket.org_id,
        ticket_id=ticket.id,
        called_by=actor.id,
        result=result.value,
        notes=seal(PayBankChangeCall, "notes", notes, org_id=ticket.org_id, row_id=call_id)
        if notes
        else None,
        number_last4=number[-4:],
        created_at=now,
    )
    session.add(call)
    await session.flush()
    vendor = await get_vendor(session, ticket.vendor_id, lock=True)
    if result is CallResult.CONFIRMED:
        ticket.status = TicketStatus.AWAITING_APPROVAL.value
        ticket.verified_by = actor.id
        ticket.verified_at = now
        ticket.verification_call_id = call.id
    elif result is CallResult.NOT_CONFIRMED:
        await _reject(
            session,
            ticket,
            vendor,
            by,
            now,
            outcome="verification_failed",
            sticky=HoldReason.VERIFICATION_FAILED,
        )
    ticket.updated_at = now
    await session.flush()
    await audit(
        session,
        actor,
        "pay.bank_change.call_recorded",
        entity_type=TICKET_ENTITY,
        entity_id=ticket.id,
        payload={
            "call_id": str(call.id),
            "result": result.value,
            "number_last4": call.number_last4,
            "status": ticket.status,
        },
    )
    await record_step(
        session,
        by,
        entity_type=TICKET_ENTITY,
        entity_id=ticket.id,
        action=f"call_{result.value}",
        inputs={
            "call_id": str(call.id),
            "number_on_file_last4": call.number_last4,
            "number_source": "on_file_snapshot",
        },
        output={"status": ticket.status, "payment_hold": vendor.payment_hold},
        reason=notes,
    )
    await refresh_decisions(session, ticket)
    return ticket, call


# ------------------------------------------------------------------ approval


def approval_payload(
    ticket: PayBankChangeTicket, vendor: PayVendor, proposed: PayVendorBankAccount
) -> dict[str, Any]:
    """What the two approvers approve. Strings only (the executor hashes it)."""
    return {
        "ticket_id": str(ticket.id),
        "reference": ticket.reference,
        "vendor_id": str(vendor.id),
        "vendor_name": vendor.legal_name,
        "proposed_account_id": str(proposed.id),
        "previous_account_id": str(ticket.previous_account_id)
        if ticket.previous_account_id
        else None,
        "details_digest": ticket.details_digest,
        "new_iban_masked": f"{proposed.country}** **** {proposed.iban_last4}",
        "new_bank_name": proposed.bank_name,
        "verified_by": str(ticket.verified_by) if ticket.verified_by else None,
        "verification_call_id": str(ticket.verification_call_id)
        if ticket.verification_call_id
        else None,
        "cooling_off_hours": str(ticket.cooling_off_hours),
    }


def preparers(ticket: PayBankChangeTicket) -> list[uuid.UUID]:
    """People who may never approve this change: the requester and the verifier."""
    people = [p for p in (ticket.requester_user_id, ticket.verified_by) if p is not None]
    return list(dict.fromkeys(people))


async def approve_bank_change(session: AsyncSession, call: FinalActionCall) -> Mapping[str, Any]:
    """The final action, inside the executor's transaction after it re-checked the
    approval, the payload hash and every decision. Re-checks the ticket, the locked
    details and the segregation of duties itself (fail closed)."""
    try:
        ticket_id = uuid.UUID(str(call.payload.get("ticket_id")))
        ticket = await get_ticket(session, ticket_id, lock=True)
    except (ValueError, TicketNotFoundError):
        raise ExecutorRefusedError("ticket_not_found") from None
    if ticket.approval_id != call.approval_id or ticket.run_id != call.run_id:
        raise ExecutorRefusedError("ticket_not_of_this_approval")
    if ticket.status != TicketStatus.AWAITING_APPROVAL.value:
        raise ExecutorRefusedError("ticket_not_awaiting_approval")
    proposed = await _account(session, ticket.proposed_account_id)
    if proposed is None or proposed.status != AccountStatus.PROPOSED.value:
        raise ExecutorRefusedError("proposed_details_missing")
    if call.payload.get("details_digest") != ticket.details_digest or not digest_matches(
        ticket, proposed
    ):
        raise ExecutorRefusedError("details_changed")
    # Segregation of duties, from the decision rows (people decided for, too).
    rows = (
        await session.execute(
            select(ApprovalDecision.actor_user_id, ApprovalDecision.on_behalf_of).where(
                ApprovalDecision.approval_id == call.approval_id
            )
        )
    ).all()
    involved = {r.actor_user_id for r in rows} | {r.on_behalf_of for r in rows if r.on_behalf_of}
    if set(preparers(ticket)) & (involved | set(call.approver_ids)):
        raise ExecutorRefusedError("requester_or_verifier_approved")
    if len(set(call.approver_ids)) < 2:
        raise ExecutorRefusedError("dual_approval_missing")
    if ticket.verified_by is None or ticket.verification_call_id is None:
        raise ExecutorRefusedError("not_verified")

    now = datetime.now(UTC)
    by = By.system()
    ticket.approver_ids = [str(a) for a in call.approver_ids]
    ticket.approved_at = now
    if ticket.cooling_off_hours > 0:
        ticket.status = TicketStatus.COOLING_OFF.value
        ticket.activates_at = now + timedelta(hours=ticket.cooling_off_hours)
        ticket.updated_at = now
        await session.flush()
        await audit(
            session,
            by,
            "pay.bank_change.approved",
            entity_type=TICKET_ENTITY,
            entity_id=ticket.id,
            payload={
                "approval_id": str(call.approval_id),
                "approvers": ticket.approver_ids,
                "activates_at": ticket.activates_at.isoformat(),
            },
        )
    else:
        await audit(
            session,
            by,
            "pay.bank_change.approved",
            entity_type=TICKET_ENTITY,
            entity_id=ticket.id,
            payload={
                "approval_id": str(call.approval_id),
                "approvers": ticket.approver_ids,
                "activates_at": None,
            },
        )
        await activate(session, ticket, now=now)
    return {"ticket_id": str(ticket.id), "status": ticket.status}


# ------------------------------------------------------------------ activation


class ActivationRefusedError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def activate(session: AsyncSession, ticket: PayBankChangeTicket, *, now: datetime) -> None:
    """Make the approved details the vendor's active account (ticket locked by the caller).

    Refuses (``ActivationRefusedError``) when the ticket is not approved, its approval is
    missing or the locked details changed; the ticket then fails and the hold stays."""
    if ticket.status not in {TicketStatus.AWAITING_APPROVAL.value, TicketStatus.COOLING_OFF.value}:
        raise ActivationRefusedError("ticket_not_approved")
    if ticket.approved_at is None or len(set(ticket.approver_ids)) < 2:
        raise ActivationRefusedError("dual_approval_missing")
    if {str(p) for p in preparers(ticket)} & set(ticket.approver_ids):
        raise ActivationRefusedError("requester_or_verifier_approved")
    vendor = await get_vendor(session, ticket.vendor_id, lock=True)
    proposed = await _account(session, ticket.proposed_account_id)
    if (
        proposed is None
        or proposed.status != AccountStatus.PROPOSED.value
        or not digest_matches(ticket, proposed)
    ):
        raise ActivationRefusedError("details_changed")
    by = By.system()
    current = await active_account(session, vendor.id)
    if current is not None:
        current.status = AccountStatus.RETIRED.value
        current.retired_at = now
        await session.flush()
    proposed.status = AccountStatus.ACTIVE.value
    proposed.activated_at = now
    if (
        vendor.status == VendorStatus.PENDING.value
        and ticket.source == TicketSource.ONBOARDING.value
    ):
        vendor.status = VendorStatus.ACTIVE.value
        vendor.activated_at = now
    ticket.status = TicketStatus.COMPLETED.value
    ticket.completed_at = ticket.closed_at = now
    ticket.closed_by_type = by.actor_type.value
    ticket.closed_by_id = by.actor_id
    ticket.outcome = "activated"
    ticket.updated_at = now
    await session.flush()
    held = await recompute_hold(session, vendor, by, now)
    ticket.hold_kept = held
    await audit(
        session,
        by,
        "pay.bank_change.activated",
        entity_type=TICKET_ENTITY,
        entity_id=ticket.id,
        payload={
            "vendor_id": str(vendor.id),
            "retired_account_id": str(current.id) if current else None,
            "active_account_id": str(proposed.id),
            "iban_last4": proposed.iban_last4,
            "payment_hold": held,
        },
    )
    await audit(
        session,
        by,
        "pay.vendor.bank_details_changed",
        entity_type=VENDOR_ENTITY,
        entity_id=vendor.id,
        payload={
            "ticket_id": str(ticket.id),
            "active_account_id": str(proposed.id),
            "iban_last4": proposed.iban_last4,
        },
    )
    await record_step(
        session,
        by,
        entity_type=TICKET_ENTITY,
        entity_id=ticket.id,
        action="bank_details_activated",
        inputs={
            "details_digest": ticket.details_digest,
            "approvers": list(ticket.approver_ids),
            "cooling_off_hours": ticket.cooling_off_hours,
        },
        output={
            "status": ticket.status,
            "payment_hold": held,
            "active_account_id": str(proposed.id),
        },
        approval_ids=(ticket.approval_id,) if ticket.approval_id else (),
    )
    await refresh_decisions(session, ticket)


async def fail_ticket(
    session: AsyncSession, ticket: PayBankChangeTicket, reason: str, *, now: datetime | None = None
) -> None:
    """The change could not go on (fail closed): the old details stay, the hold stays
    and becomes sticky, a person looks."""
    now = now or datetime.now(UTC)
    vendor = await get_vendor(session, ticket.vendor_id, lock=True)
    await _reject(
        session,
        ticket,
        vendor,
        By.system(),
        now,
        outcome=reason[:64],
        sticky=HoldReason.APPROVAL_NOT_POSSIBLE,
        status=TicketStatus.FAILED,
    )


async def activate_due(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 100
) -> dict[str, int]:
    """Activate every ticket of the session's organisation whose cooling-off is over."""
    now = now or datetime.now(UTC)
    due = list(
        await session.scalars(
            select(PayBankChangeTicket.id)
            .where(
                PayBankChangeTicket.status == TicketStatus.COOLING_OFF.value,
                PayBankChangeTicket.activates_at <= now,
            )
            .order_by(PayBankChangeTicket.activates_at)
            .limit(limit)
        )
    )
    activated = failed = 0
    for ticket_id in due:
        ticket = await get_ticket(session, ticket_id, lock=True)
        if (
            ticket.status != TicketStatus.COOLING_OFF.value
            or ticket.activates_at is None
            or ticket.activates_at > now
        ):
            continue
        try:
            async with session.begin_nested():
                await activate(session, ticket, now=now)
            activated += 1
        except ActivationRefusedError as exc:
            await fail_ticket(session, ticket, f"activation_refused:{exc.reason}", now=now)
            failed += 1
    return {"activated": activated, "failed": failed}


# ------------------------------------------------------------------ closing


async def _reject(
    session: AsyncSession,
    ticket: PayBankChangeTicket,
    vendor: PayVendor,
    by: By,
    now: datetime,
    *,
    outcome: str,
    sticky: HoldReason | None = None,
    status: TicketStatus = TicketStatus.REJECTED,
    reason: str | None = None,
) -> None:
    proposed = await _account(session, ticket.proposed_account_id)
    if proposed is not None and proposed.status == AccountStatus.PROPOSED.value:
        proposed.status = AccountStatus.REJECTED.value
    ticket.status = status.value
    ticket.closed_at = ticket.updated_at = now
    ticket.closed_by_type = by.actor_type.value
    ticket.closed_by_id = by.actor_id
    ticket.outcome = outcome
    if reason:
        ticket.close_reason = seal(
            PayBankChangeTicket, "close_reason", reason, org_id=ticket.org_id, row_id=ticket.id
        )  # type: ignore[assignment]
    await session.flush()
    if sticky is not None:
        await place_hold(session, vendor, sticky, by, now)
    held = await recompute_hold(session, vendor, by, now)
    ticket.hold_kept = held
    await session.flush()
    await audit(
        session,
        by,
        f"pay.bank_change.{status.value}",
        entity_type=TICKET_ENTITY,
        entity_id=ticket.id,
        payload={"outcome": outcome, "payment_hold": held, "hold_reason": vendor.hold_reason},
    )
    await record_step(
        session,
        by,
        entity_type=TICKET_ENTITY,
        entity_id=ticket.id,
        action=f"bank_change_{status.value}",
        inputs={"outcome": outcome, "proposed_account_id": str(ticket.proposed_account_id)},
        output={
            "status": ticket.status,
            "payment_hold": held,
            "hold_reason": vendor.hold_reason,
            "old_details_kept": True,
        },
        reason=reason,
        approval_ids=(ticket.approval_id,) if ticket.approval_id else (),
    )
    await refresh_decisions(session, ticket)


async def close_after_decision(
    session: AsyncSession, ticket_id: uuid.UUID, outcome: str, reason: str | None
) -> None:
    """The approvers rejected the change (or asked for changes, which for bank details
    means no): keep the old details, hold per rule."""
    ticket = await get_ticket(session, ticket_id, lock=True)
    if ticket.status != TicketStatus.AWAITING_APPROVAL.value:
        return
    vendor = await get_vendor(session, ticket.vendor_id, lock=True)
    await _reject(
        session, ticket, vendor, By.system(), datetime.now(UTC), outcome=outcome, reason=reason
    )


async def cancel_ticket(
    session: AsyncSession, actor: Actor, ticket_id: uuid.UUID, *, reason: str, keep_hold: bool
) -> PayBankChangeTicket:
    """A person cancels a ticket waiting for its call, or one in its cooling-off period.
    The route checked the permission for the ticket's status. The old details stay."""
    ticket = await get_ticket(session, ticket_id, lock=True)
    if ticket.status not in {
        TicketStatus.AWAITING_VERIFICATION.value,
        TicketStatus.COOLING_OFF.value,
    }:
        raise TicketStateError(
            "only a change waiting for its call or in its cooling-off period can be cancelled"
        )
    vendor = await get_vendor(session, ticket.vendor_id, lock=True)
    await _reject(
        session,
        ticket,
        vendor,
        By.of(actor),
        datetime.now(UTC),
        outcome=f"cancelled_from_{ticket.status}",
        sticky=HoldReason.CANCELLED_KEEP_HOLD if keep_hold else None,
        status=TicketStatus.CANCELLED,
        reason=reason,
    )
    return ticket


def hold_is_sticky(vendor: PayVendor) -> bool:
    return is_sticky(vendor)


STICKY_REASONS: Final = frozenset(r.value for r in STICKY)
