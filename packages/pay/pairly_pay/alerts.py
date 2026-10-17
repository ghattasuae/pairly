# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Urgent alert when a bank-detail change ticket opens: an invoice check opens or joins
one (``alert_bank_change``), or a vendor's contact asks for new bank details in the
vendor portal (``alert_portal_bank_change``, template ``pay.bank_change.portal_alert``).

Who is told: every member of the organisation who holds the approver or the finance
controller role for Pay (organisation wide or Pay only), the people who will verify and
approve the change. Each gets ``pay.bank_change.alert`` (category ``urgent``: sent at
once, never held for a digest) on every channel: in-app, email, and Slack when the
organisation has Slack and the person linked their account (``notify`` suppresses the
Slack row otherwise). The in-app item links to the ticket (``/pay/bank-changes/<id>``,
an app path the bell resolves in the viewer's locale); the email carries the same page
as a full link when the public web address is configured.

The message names the vendor, the invoice and the ticket references and the last four
characters of the printed account number, never more of it.

Runs inside the check's (or the portal request's) transaction; in a portal request the
recipients are found past the portal guard (``outside_portal``), never shown to the
vendor. Each person's notifications run in a savepoint and
a failure is logged and skipped: an alert never blocks the hold or the ticket.
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.service import role_members
from pairly_core.db import outside_portal
from pairly_core.notifications.links import absolute_url, public_web_url
from pairly_core.notifications.models import Urgency
from pairly_core.notifications.service import Extras, Recipient, notify
from pairly_pay.common import WORKFLOW
from pairly_pay.models import PayBankChangeTicket, PayInvoice, PayVendor
from pairly_pay.templates import BANK_CHANGE_ALERT, PORTAL_BANK_CHANGE_ALERT

log = structlog.get_logger("pairly_pay.alerts")

ALERT_ROLES: Final = ("approver", "finance_controller")
ALERT_ACTOR: Final = "pairly.pay.checks"
PORTAL_ALERT_ACTOR: Final = "pairly.pay.vendor_portal"


def ticket_path(ticket_id: uuid.UUID) -> str:
    """The app path of a bank-detail change ticket (no locale: the bell adds it)."""
    return f"/pay/bank-changes/{ticket_id}"


def _last4(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum())[-4:]


async def alert_bank_change(
    session: AsyncSession,
    *,
    ticket: PayBankChangeTicket,
    vendor: PayVendor,
    invoice: PayInvoice,
    iban_last4: str,
    joined: bool,
) -> int:
    """Notify the approvers and finance controllers. Returns how many people were told."""
    return await _alert(
        session,
        ticket=ticket,
        template_key=BANK_CHANGE_ALERT,
        variables={
            "vendor": vendor.trade_name or vendor.legal_name,
            "invoice_reference": invoice.reference,
            "ticket_reference": ticket.reference,
            "iban_last4": _last4(iban_last4),
            "joined": joined,
        },
        dedupe=f"{invoice.id}",
        actor_id=ALERT_ACTOR,
    )


async def alert_portal_bank_change(
    session: AsyncSession, *, ticket: PayBankChangeTicket, vendor: PayVendor, iban_last4: str
) -> int:
    """The same urgent alert, to the same people, for a bank change a vendor's contact
    asked for in the vendor portal. Returns how many people were told."""
    return await _alert(
        session,
        ticket=ticket,
        template_key=PORTAL_BANK_CHANGE_ALERT,
        variables={
            "vendor": vendor.trade_name or vendor.legal_name,
            "ticket_reference": ticket.reference,
            "iban_last4": _last4(iban_last4),
        },
        dedupe="portal",
        actor_id=PORTAL_ALERT_ACTOR,
    )


async def _alert(
    session: AsyncSession,
    *,
    ticket: PayBankChangeTicket,
    template_key: str,
    variables: dict[str, object],
    dedupe: str,
    actor_id: str,
) -> int:
    async with outside_portal(session):
        people = sorted(await role_members(session, ALERT_ROLES, WORKFLOW))
    path = ticket_path(ticket.id)
    url = absolute_url(public_web_url(), path)
    if url is not None:
        variables["ticket_url"] = url
    told = 0
    for person in people:
        try:
            async with session.begin_nested():
                await notify(
                    session,
                    recipient=Recipient.user(person),
                    template_key=template_key,
                    variables=variables,
                    dedupe_key=f"pay:bank_alert:{ticket.id}:{dedupe}:{person}",
                    urgency=Urgency.URGENT,
                    extras=Extras(link=path),
                    workflow=WORKFLOW,
                    actor_id=actor_id,
                )
            told += 1
        except Exception as exc:
            log.warning(
                "bank_change_alert_failed",
                ticket_id=str(ticket.id),
                user_id=str(person),
                error=type(exc).__name__,
            )
    return told
