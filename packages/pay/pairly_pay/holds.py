# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The vendor payment hold: when it is placed, and when it may lift.

``pay_vendors.payment_hold`` blocks every payment to the vendor (payment runs, WP4.7,
skip held vendors). The rule, in full:

1. Opening a bank-detail change ticket places the hold at once (reason
   ``bank_change``), in the same transaction that creates the ticket.
2. A ticket-driven hold lifts only when the vendor has no open ticket left
   (``recompute_hold`` after every ticket closes: completed, rejected, cancelled,
   failed).
3. Some outcomes leave the hold in place on purpose ("sticky" reasons), because they
   point at fraud or at a control that could not run:

   * ``verification_failed``: the vendor, called on the number on file, said it did not
     ask for the change;
   * ``approval_not_possible``: fewer than two people may approve, so the change could
     not get its dual approval (fail closed);
   * ``cancelled_keep_hold``: a person cancelled a ticket and chose to keep the hold;
   * ``manual``: a finance controller put the vendor on hold.

   A sticky reason outranks ``bank_change`` and survives every ticket closing. Only a
   person with ``pay.hold.manage`` (a second factor) releases it, with a written
   reason, and never while a ticket is open (the ticket's own hold stays).

Every change of the hold is audited (``pay.vendor.hold_placed``,
``pay.vendor.hold_lifted``). The ticket records whether its closing kept the hold
(``hold_kept``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_pay.common import VENDOR_ENTITY, By, audit
from pairly_pay.models import (
    OPEN_TICKET_STATUSES,
    HoldReason,
    PayBankChangeTicket,
    PayVendor,
)

STICKY: Final = frozenset(
    {
        HoldReason.VERIFICATION_FAILED,
        HoldReason.APPROVAL_NOT_POSSIBLE,
        HoldReason.CANCELLED_KEEP_HOLD,
        HoldReason.MANUAL,
    }
)


def is_sticky(vendor: PayVendor) -> bool:
    return vendor.payment_hold and vendor.hold_reason in {r.value for r in STICKY}


async def open_ticket_count(session: AsyncSession, vendor: PayVendor) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(PayBankChangeTicket)
        .where(
            PayBankChangeTicket.vendor_id == vendor.id,
            PayBankChangeTicket.status.in_([s.value for s in OPEN_TICKET_STATUSES]),
        )
    )
    return int(count or 0)


async def place_hold(
    session: AsyncSession, vendor: PayVendor, reason: HoldReason, by: By, now: datetime
) -> None:
    """Hold the vendor's payments. A sticky reason replaces ``bank_change``; nothing
    replaces a sticky reason (the first one stays until a person releases it)."""
    previous = vendor.hold_reason if vendor.payment_hold else None
    if is_sticky(vendor):
        reason_now = vendor.hold_reason
    elif reason in STICKY or previous is None:
        reason_now = reason.value
    else:
        reason_now = previous
    if vendor.payment_hold and reason_now == previous:
        return
    vendor.payment_hold = True
    vendor.hold_reason = reason_now
    vendor.hold_set_at = now
    vendor.updated_at = now
    await session.flush()
    await audit(
        session,
        by,
        "pay.vendor.hold_placed",
        entity_type=VENDOR_ENTITY,
        entity_id=vendor.id,
        payload={"reason": reason_now, "previous_reason": previous},
    )


async def _lift(session: AsyncSession, vendor: PayVendor, by: By, now: datetime) -> None:
    previous = vendor.hold_reason
    vendor.payment_hold = False
    vendor.hold_reason = None
    vendor.hold_set_at = None
    vendor.updated_at = now
    await session.flush()
    await audit(
        session,
        by,
        "pay.vendor.hold_lifted",
        entity_type=VENDOR_ENTITY,
        entity_id=vendor.id,
        payload={"previous_reason": previous},
    )


async def recompute_hold(session: AsyncSession, vendor: PayVendor, by: By, now: datetime) -> bool:
    """After a ticket closed: lift a ticket-driven hold when no ticket is open any more.
    Returns whether the vendor is still on hold."""
    if not vendor.payment_hold:
        return False
    if is_sticky(vendor):
        return True
    if await open_ticket_count(session, vendor) > 0:
        return True
    await _lift(session, vendor, by, now)
    return False


class HoldStillNeededError(Exception):
    """The hold cannot be released: a change ticket is still open."""


async def release_hold(session: AsyncSession, vendor: PayVendor, by: By, now: datetime) -> None:
    """A person releases a sticky (or manual) hold. Refused while a ticket is open."""
    if await open_ticket_count(session, vendor) > 0:
        raise HoldStillNeededError()
    if vendor.payment_hold:
        await _lift(session, vendor, by, now)
