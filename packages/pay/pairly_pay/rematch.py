# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Matching again when a purchase order changes (brief 4.5.2).

* **A goods receipt arrives** (``after_receipt``): the invoices of that PO that wait for
  a person because nothing was received (``check:missing_receipt``, status
  ``needs_review``) run every check again, so a delivery recorded later clears the
  exception by itself. Only those: an invoice a person already moved on (``ready``),
  one awaiting approval, approved, scheduled or paid is never touched.
* **A PO is cancelled** (``after_cancel``): its invoices still with a person
  (``needs_review`` or ``ready``) run the checks again and now fail matching with the
  reason. Invoices awaiting approval keep their results, which are stale from now on
  (``pairly_pay.freshness``), so they can no longer be approved.

Each run goes through ``recheck_invoice`` in the caller's transaction (audited, fail
closed: checks that cannot run leave the invoice stale). At most ``MAX_RECHECKS``
invoices per change, oldest first.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_pay.checks.service import CheckServices, RecheckTrigger, recheck_invoice
from pairly_pay.common import By
from pairly_pay.invoices import refresh_decisions
from pairly_pay.models import InvoiceStatus, PayInvoice

MAX_RECHECKS: Final = 50
MISSING_RECEIPT: Final = "check:missing_receipt"


async def _recheck(
    session: AsyncSession,
    invoices: Sequence[PayInvoice],
    services: CheckServices,
    trigger: RecheckTrigger,
    by: By,
) -> list[uuid.UUID]:
    done: list[uuid.UUID] = []
    for invoice in invoices:
        await recheck_invoice(session, invoice, services, trigger=trigger, by=by)
        await refresh_decisions(session, invoice)
        done.append(invoice.id)
    return done


async def after_receipt(
    session: AsyncSession, po_ids: Sequence[uuid.UUID], services: CheckServices, by: By
) -> list[uuid.UUID]:
    """Run the checks again on the invoices of these POs waiting for a goods receipt."""
    if not po_ids:
        return []
    rows = await session.scalars(
        select(PayInvoice)
        .where(
            PayInvoice.po_id.in_(list(po_ids)),
            PayInvoice.status == InvoiceStatus.NEEDS_REVIEW.value,
        )
        .order_by(PayInvoice.created_at, PayInvoice.id)
        .with_for_update()
        .limit(MAX_RECHECKS * 4)
    )
    waiting = [i for i in rows if MISSING_RECEIPT in (i.review_reasons or [])][:MAX_RECHECKS]
    return await _recheck(session, waiting, services, RecheckTrigger.GOODS_RECEIPT, by)


async def after_cancel(
    session: AsyncSession, po_id: uuid.UUID, services: CheckServices, by: By
) -> list[uuid.UUID]:
    """Run the checks again on the invoices of a cancelled PO still with a person."""
    rows = await session.scalars(
        select(PayInvoice)
        .where(
            PayInvoice.po_id == po_id,
            PayInvoice.status.in_([InvoiceStatus.NEEDS_REVIEW.value, InvoiceStatus.READY.value]),
        )
        .order_by(PayInvoice.created_at, PayInvoice.id)
        .with_for_update()
        .limit(MAX_RECHECKS)
    )
    return await _recheck(session, list(rows), services, RecheckTrigger.PO_CANCELLED, by)
