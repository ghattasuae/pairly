# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Learning from corrections (brief 4.3.4): corrections filed per vendor, hints back.

When a person corrects a field of an invoice, the engine records the correction
(``extraction_corrections``, a decision record, an audit event). Pay files it under the
invoice's vendor (``pay_invoice_corrections``). The next invoice from that vendor gets
the vendor's recent corrections as few-shot hints in the extraction prompt
(``reviewer_corrections``): data, never instructions.

Selection is deterministic: newest first (time, then id), one hint per field pattern
(the newest correction of a field wins, so an old mistake does not come back), at most
``MAX_HINTS``. Bank-detail fields are never hints: a learned IBAN could teach the model
to read somebody else's account number. Their correction rows keep no values.

Whether the hints help is measured by the Pay evals (WP4.8), not assumed here.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.extraction.engine import FewShotHint
from pairly_core.extraction.service import StoredCorrection
from pairly_pay.invoice_schema import SENSITIVE_PREFIXES
from pairly_pay.models import PayInvoice, PayInvoiceCorrection

MAX_HINTS: Final = 8
SCAN: Final = 200


def is_sensitive(path: str) -> bool:
    return path.startswith(SENSITIVE_PREFIXES)


async def file_correction(
    session: AsyncSession, invoice: PayInvoice, correction: StoredCorrection, pattern: str
) -> PayInvoiceCorrection:
    """File a stored engine correction under the invoice's vendor."""
    sensitive = is_sensitive(correction.field_path)
    row = PayInvoiceCorrection(
        id=uuid.uuid4(),
        org_id=invoice.org_id,
        invoice_id=invoice.id,
        vendor_id=invoice.vendor_id,
        extraction_id=correction.extraction_id,
        correction_id=correction.id,
        field_path=correction.field_path,
        field_pattern=pattern,
        old_value=None if sensitive else correction.old_value,
        new_value=None if sensitive else correction.new_value,
        corrected_by=correction.corrected_by,
    )
    session.add(row)
    await session.flush()
    return row


async def vendor_hints(
    session: AsyncSession, vendor_id: uuid.UUID | None, *, limit: int = MAX_HINTS
) -> list[FewShotHint]:
    """The vendor's recent corrections as hints, newest first, capped. Empty without a
    vendor."""
    if vendor_id is None:
        return []
    limit = max(1, min(limit, MAX_HINTS))
    rows = (
        await session.scalars(
            select(PayInvoiceCorrection)
            .where(PayInvoiceCorrection.vendor_id == vendor_id)
            .order_by(PayInvoiceCorrection.created_at.desc(), PayInvoiceCorrection.id.desc())
            .limit(SCAN)
        )
    ).all()
    hints: list[FewShotHint] = []
    seen: set[str] = set()
    for row in rows:
        if is_sensitive(row.field_pattern) or row.field_pattern in seen:
            continue
        seen.add(row.field_pattern)
        hints.append(FewShotHint(row.field_pattern, row.old_value, row.new_value))
        if len(hints) >= limit:
            break
    return hints


def hint_inputs(hints: list[FewShotHint]) -> list[dict[str, Any]]:
    """What the prompt receives for ``hints`` (``extract`` sends exactly this)."""
    return [h.as_input() for h in hints]
