# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Appeals from the vendor portal (brief 2.3.3, 4.7): a human review of a decision.

A vendor's contact asks, from the portal page of one of its invoices, for a person to
review a decision about that invoice, by the decision's id. The same engine path as a
candidate's request in the candidate portal (``pairly_hire.portal.service.appeal``):
``pairly_core.decisions.appeals.create_appeal`` as the contact's portal actor, which
holds ``pay.appeal.create`` and nothing of staff. The appeal is audited
(``core.appeal.created``, the contact as the actor), has the engine's SLA, and the
organisation's Pay admins are told at once with its deadline
(``pay.vendor_portal.appeal``, category ``data_requests``). A person resolves it on the
decision's page like any appeal.

Which decisions a vendor may appeal, and only for its own invoice:

* ``duplicate_flag``: the invoice checks (a ``fraud_flag`` record) flagged it as a
  duplicate. Other check results are never shown to the vendor.
* ``rejection``: a person rejected the invoice (``pay.invoice.rejected``).
* ``bank_change``: a step of a bank-detail change ticket the invoice's checks opened or
  joined (the ticket id is in the bank check's finding), taken by staff or the system:
  the ticket opening that held payments, its rejection, cancellation or failure. The
  vendor's own request is not something to appeal.

The requester is the vendor (``pay_vendor:<vendor id>``), so every contact of the vendor
sees its appeals and a decision is appealed once. In the database the portal guard shows
a vendor portal session only these decision records and its vendor's appeals (migration
``pay_0018``); the code filters by the vendor itself as well.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.service import role_members
from pairly_core.audit import ActorType
from pairly_core.db import outside_portal
from pairly_core.decisions.appeals import AppealView, create_appeal
from pairly_core.decisions.escalation import appeal_ref
from pairly_core.decisions.ids import is_decision_id
from pairly_core.decisions.models import (
    Appeal,
    AppealStatus,
    DecisionKind,
    DecisionRecord,
    RequesterType,
)
from pairly_core.errors import ConflictError, NotFoundError
from pairly_core.notifications.links import absolute_url, decision_path, public_web_url
from pairly_core.notifications.models import Urgency
from pairly_core.notifications.service import Extras, Recipient, notify
from pairly_core.rbac import BuiltinRole
from pairly_pay.common import TICKET_ENTITY, WORKFLOW
from pairly_pay.models import PayBankChangeTicket, PayInvoice, PayVendor
from pairly_pay.portal.auth import PortalIdentity, portal_subject
from pairly_pay.templates import VENDOR_APPEAL

log = structlog.get_logger("pairly_pay.portal.appeals")

INVOICE_ENTITY: Final = "pay.invoice"
REJECTED_ACTION: Final = "pay.invoice.rejected"
DUPLICATE_CHECK: Final = "duplicates"
BANK_CHECK: Final = "bank"
# The steps of a bank-detail change ticket a vendor may ask a person to review.
TICKET_ACTIONS: Final = frozenset(
    {
        "bank_change_requested",
        "bank_change_rejected",
        "bank_change_cancelled",
        "bank_change_failed",
    }
)
MAX_TICKETS: Final = 20


class AppealableKind(StrEnum):
    DUPLICATE_FLAG = "duplicate_flag"
    REJECTION = "rejection"
    BANK_CHANGE = "bank_change"


def requester_id(identity: PortalIdentity) -> str:
    """The vendor as the requester: every contact of the vendor shares its appeals."""
    return portal_subject(identity.vendor_id)


def appealable_kind(
    row: DecisionRecord, *, invoice_id: uuid.UUID, ticket_ids: Iterable[uuid.UUID]
) -> AppealableKind | None:
    """What a vendor may appeal in ``row`` for its invoice ``invoice_id`` (whose checks
    named ``ticket_ids``), or None."""
    if row.workflow != WORKFLOW:
        return None
    if row.entity_type == INVOICE_ENTITY and row.entity_id == str(invoice_id):
        if row.kind == DecisionKind.FRAUD_FLAG.value:
            checks = (row.output or {}).get("checks")
            if isinstance(checks, Mapping) and checks.get(DUPLICATE_CHECK) == "fail":
                return AppealableKind.DUPLICATE_FLAG
            return None
        if row.kind == DecisionKind.FINAL_ACTION.value and row.final_action == REJECTED_ACTION:
            return AppealableKind.REJECTION
        return None
    if (
        row.entity_type == TICKET_ENTITY
        and row.entity_id in {str(t) for t in ticket_ids}
        and row.kind == DecisionKind.FINAL_ACTION.value
        and row.final_action in TICKET_ACTIONS
        and row.actor_type != ActorType.VENDOR.value
    ):
        return AppealableKind.BANK_CHANGE
    return None


def ticket_ids_of(checks: Sequence[Any]) -> list[uuid.UUID]:
    """The bank-detail change tickets the invoice's bank check opened or joined."""
    found: list[uuid.UUID] = []
    for check in checks or ():
        if not isinstance(check, Mapping) or check.get("check") != BANK_CHECK:
            continue
        for finding in check.get("findings") or ():
            evidence = finding.get("evidence") if isinstance(finding, Mapping) else None
            raw = evidence.get("ticket_id") if isinstance(evidence, Mapping) else None
            try:
                ticket = uuid.UUID(str(raw)) if raw else None
            except ValueError:
                ticket = None
            if ticket is not None and ticket not in found:
                found.append(ticket)
    return found[:MAX_TICKETS]


@dataclass(frozen=True, slots=True)
class PortalAppeal:
    id: uuid.UUID
    reference: str
    status: str
    outcome: str | None
    sla_due_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PortalDecision:
    decision_id: str
    kind: AppealableKind
    decided_at: datetime
    can_appeal: bool
    appeal: PortalAppeal | None


async def _tickets(
    session: AsyncSession, identity: PortalIdentity, invoice: PayInvoice
) -> list[uuid.UUID]:
    named = ticket_ids_of(invoice.checks)
    if not named:
        return []
    own = set(
        await session.scalars(
            select(PayBankChangeTicket.id).where(
                PayBankChangeTicket.id.in_(named),
                PayBankChangeTicket.vendor_id == identity.vendor_id,
            )
        )
    )
    return [t for t in named if t in own]


async def _candidates(
    session: AsyncSession, invoice: PayInvoice, tickets: Sequence[uuid.UUID]
) -> list[DecisionRecord]:
    clauses = [
        (DecisionRecord.entity_type == INVOICE_ENTITY)
        & (DecisionRecord.entity_id == str(invoice.id))
    ]
    if tickets:
        clauses.append(
            (DecisionRecord.entity_type == TICKET_ENTITY)
            & DecisionRecord.entity_id.in_([str(t) for t in tickets])
        )
    return list(
        await session.scalars(
            select(DecisionRecord)
            .where(DecisionRecord.workflow == WORKFLOW, or_(*clauses))
            .order_by(DecisionRecord.decided_at, DecisionRecord.id)
            .limit(200)
        )
    )


async def _appeals(
    session: AsyncSession, identity: PortalIdentity, decision_ids: Sequence[str]
) -> dict[str, Appeal]:
    if not decision_ids:
        return {}
    rows = await session.scalars(
        select(Appeal)
        .where(
            Appeal.requester_type == RequesterType.VENDOR.value,
            Appeal.requester_id == requester_id(identity),
            Appeal.decision_id.in_(list(decision_ids)),
        )
        .order_by(Appeal.created_at)
    )
    return {a.decision_id: a for a in rows}


def _appeal_out(row: Appeal) -> PortalAppeal:
    return PortalAppeal(
        id=row.id,
        reference=appeal_ref(row.id),
        status=row.status,
        outcome=row.outcome,
        sla_due_at=row.sla_due_at,
        created_at=row.created_at,
    )


async def decisions(
    session: AsyncSession, identity: PortalIdentity, invoice: PayInvoice
) -> list[PortalDecision]:
    """The decisions about ``invoice`` (one of the vendor's own) the vendor may ask a
    person to review, each with its appeal when there is one."""
    if invoice.vendor_id != identity.vendor_id:
        raise NotFoundError("invoice not found")
    tickets = await _tickets(session, identity, invoice)
    rows = [
        (row, kind)
        for row in await _candidates(session, invoice, tickets)
        if (kind := appealable_kind(row, invoice_id=invoice.id, ticket_ids=tickets)) is not None
    ]
    appeals = await _appeals(session, identity, [row.id for row, _ in rows])
    return [
        PortalDecision(
            decision_id=row.id,
            kind=kind,
            decided_at=row.decided_at,
            can_appeal=row.id not in appeals,
            appeal=_appeal_out(appeals[row.id]) if row.id in appeals else None,
        )
        for row, kind in rows
    ]


async def appeal(
    session: AsyncSession,
    identity: PortalIdentity,
    invoice: PayInvoice,
    decision_id: str,
    reason: str,
) -> AppealView:
    """Ask a person to review one decision about ``invoice``. 404 for a decision that is
    not one the vendor may appeal (another invoice's, another vendor's, a kind it never
    sees); 409 when the vendor already asked."""
    if invoice.vendor_id != identity.vendor_id or not is_decision_id(decision_id):
        raise NotFoundError("decision not found")
    found = {d.decision_id: d for d in await decisions(session, identity, invoice)}
    target = found.get(decision_id)
    if target is None:
        raise NotFoundError("decision not found")
    if target.appeal is not None:
        raise ConflictError("you already asked for a review of this decision")
    view = await create_appeal(
        session,
        identity.actor(),
        decision_id=decision_id,
        requester_type=RequesterType.VENDOR,
        requester_id=requester_id(identity),
        reason=reason,
    )
    await notify_admins(session, invoice=invoice, view=view, kind=target.kind)
    return view


async def pay_admins(session: AsyncSession) -> list[uuid.UUID]:
    """Members with the admin role for Pay (organisation wide or Pay only)."""
    # Memberships are closed to a portal session (portal guard, migration 0022): finding
    # the admins is the engine's work, never shown to the vendor.
    async with outside_portal(session):
        people = await role_members(session, (BuiltinRole.ADMIN.value,), WORKFLOW)
    return sorted(people, key=str)


async def notify_admins(
    session: AsyncSession, *, invoice: PayInvoice, view: AppealView, kind: AppealableKind
) -> int:
    """Tell the Pay admins (in the app and by email, urgent) with the appeal's deadline.
    Each person's notifications run in a savepoint; a failure is logged and skipped, so
    the appeal itself always stands. Returns how many people were told."""
    vendor = await session.scalar(select(PayVendor).where(PayVendor.id == invoice.vendor_id))
    path = decision_path(view.decision_id)
    variables: dict[str, object] = {
        "vendor": (vendor.trade_name or vendor.legal_name) if vendor is not None else "",
        "invoice_reference": invoice.reference,
        "appeal_ref": appeal_ref(view.id),
        "decision": kind.value,
        "due_at": view.sla_due_at.date().isoformat(),
    }
    url = absolute_url(public_web_url(), path)
    if url is not None:
        variables["appeal_url"] = url
    told = 0
    for person in await pay_admins(session):
        try:
            async with session.begin_nested():
                await notify(
                    session,
                    recipient=Recipient.user(person),
                    template_key=VENDOR_APPEAL,
                    variables=variables,
                    dedupe_key=f"pay.vendor_appeal:{view.id}:{person}",
                    channels=["in_app", "email"],
                    urgency=Urgency.URGENT,
                    extras=Extras(
                        link=path,
                        extra={"appeal_id": str(view.id), "due_at": view.sla_due_at.isoformat()},
                    ),
                    workflow=WORKFLOW,
                    actor_id="pairly.pay.vendor_portal",
                )
            told += 1
        except Exception as exc:
            log.warning(
                "vendor_appeal_notification_failed",
                appeal_id=str(view.id),
                user_id=str(person),
                error=type(exc).__name__,
            )
    return told


__all__ = [
    "AppealStatus",
    "AppealableKind",
    "PortalAppeal",
    "PortalDecision",
    "appeal",
    "appealable_kind",
    "decisions",
    "notify_admins",
    "requester_id",
    "ticket_ids_of",
]
