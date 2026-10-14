# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Are an invoice's check results still about the invoice as it is now?

The checks (``pairly_pay.checks``) ran on some inputs: the values read from the invoice
with every correction, the linked vendor and that vendor's bank details on file, and
the coding (department, category, and the GL account and cost centre of each line),
and the purchase order the invoice cites when one
with that number is on file (its id, status, vendor, currency and matching type: a PO
created, cancelled or closed later changes the match). When any of them changes
afterwards, the stored results describe another invoice. ``check_inputs_digest`` is a
SHA-256 over exactly those inputs; the checks store it (``pay_invoices.checks_digest``)
when they run.

* **stale**: the digest of the invoice now differs from the stored one (or the checks
  never finished). A person corrected a field, linked another vendor, changed the
  coding, or the vendor's bank details on file changed. The checks must run again.
* **failed**: the checks are current but one of them could not run (``error``), or the
  risk was never assessed. A person can still look at the invoice, but it can never be
  approved or paid until a later run succeeds (fail closed, brief rule 4).

An invoice whose checks are stale or failed can never be submitted for approval,
approved (by a person or by a rule), put in a payment run, or exported to a bank file.
Every one of those steps calls ``checks_problem`` again in its own transaction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.extraction.service import load_extraction
from pairly_pay.models import PayInvoice, PayInvoiceLineCoding
from pairly_pay.purchase_orders import effective_status, find_purchase_order
from pairly_pay.vendors import active_account

DIGEST_VERSION: Final = 1
STALE: Final = "checks_stale"
FAILED: Final = "checks_failed"
MISSING: Final = "checks_missing"


def _canonical(material: Mapping[str, Any]) -> bytes:
    return json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


async def check_inputs_digest(session: AsyncSession, invoice: PayInvoice) -> str:
    """SHA-256 of everything the checks read about this invoice (see module docstring)."""
    stored = (
        await load_extraction(session, invoice.extraction_id) if invoice.extraction_id else None
    )
    account = (
        await active_account(session, invoice.vendor_id) if invoice.vendor_id is not None else None
    )
    values = stored.current_values() if stored is not None else {}
    material: dict[str, Any] = {
        "v": DIGEST_VERSION,
        "extraction_id": str(invoice.extraction_id) if invoice.extraction_id else None,
        "values": values,
        "vendor_id": str(invoice.vendor_id) if invoice.vendor_id else None,
        "vendor_account_id": str(account.id) if account is not None else None,
        "department": invoice.department,
        "category": invoice.category,
    }
    coded = await session.execute(
        select(
            PayInvoiceLineCoding.line_index,
            PayInvoiceLineCoding.gl_code,
            PayInvoiceLineCoding.cost_centre,
        )
        .where(PayInvoiceLineCoding.invoice_id == invoice.id)
        .order_by(PayInvoiceLineCoding.line_index)
    )
    lines = [[i, gl, cc] for i, gl, cc in coded.all() if gl or cc]
    if lines:
        # Only when a line is coded, so invoices without line coding keep their digest.
        material["line_coding"] = lines
    cited = values.get("po_number")
    po = await find_purchase_order(session, cited) if isinstance(cited, str) else None
    if po is not None:
        # Only when a PO is on file, so invoices without one keep their digest. Receipts
        # are left out on purpose: a later delivery for another invoice of the same PO
        # must not make an approved invoice stale.
        material["po"] = {
            "id": str(po.id),
            # A PO closed after the invoice arrived is still open for it: closing never
            # makes an invoice already in flight stale. Cancelling does.
            "status": effective_status(po, invoice.created_at),
            "vendor_id": str(po.vendor_id),
            "currency": po.currency,
            "match_type": po.match_type,
        }
    return hashlib.sha256(_canonical(material)).hexdigest()


def checks_failed(invoice: PayInvoice) -> bool:
    """A check that could not run, or no risk assessment: never approvable."""
    if not invoice.checks:
        return True
    for item in invoice.checks:
        if not isinstance(item, Mapping) or item.get("status") not in {"pass", "fail"}:
            return True
    risk = invoice.risk if isinstance(invoice.risk, Mapping) else {}
    return not risk.get("assessment_id") or not risk.get("level")


async def checks_problem(session: AsyncSession, invoice: PayInvoice) -> str | None:
    """``None`` when the stored checks are current and complete, else why not."""
    if invoice.checks_digest is None or invoice.checked_at is None:
        return MISSING
    if await check_inputs_digest(session, invoice) != invoice.checks_digest:
        return STALE
    if checks_failed(invoice):
        return FAILED
    return None
