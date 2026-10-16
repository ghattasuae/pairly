# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pay's ``document_intake`` for the batch API (``pairly_core.batches.intake``).

Every document submitted to the ``pay`` workflow in a batch becomes an invoice, exactly
as ``POST /v1/pay/invoices`` makes one: ``pay.invoice.upload`` is required, the source is
``api`` for a key and ``upload`` for a person, the same bytes are the same invoice (file
hash dedupe), and after the commit the intake run starts as the Pay intake agent
(``start_after_intake``), which only reads and proposes. Nothing is approved or paid.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.batches.intake import DocumentIntake, IntakeResult
from pairly_core.errors import ForbiddenError
from pairly_core.ingestion.models import Document
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_pay.common import By
from pairly_pay.intake import pay_policy
from pairly_pay.invoice_start import start_after_intake
from pairly_pay.invoices import INVOICE_ENTITY, create_invoice
from pairly_pay.models import InvoiceSource
from pairly_pay.permissions import INVOICE_UPLOAD

ENTITY_TYPE: Final = INVOICE_ENTITY


async def handle(session: AsyncSession, actor: Actor, document: Document) -> IntakeResult:
    authorize(actor, INVOICE_UPLOAD)
    if actor.kind not in {ActorKind.USER, ActorKind.API_KEY}:
        raise ForbiddenError("invoices come from a person or an API key")
    source = InvoiceSource.UPLOAD if actor.kind is ActorKind.USER else InvoiceSource.API
    invoice, created = await create_invoice(
        session, document=document, source=source, by=By.of(actor)
    )
    org_id, invoice_id, document_id = actor.org_id, invoice.id, document.id

    async def start(runtime: WorkflowRuntime) -> None:
        await start_after_intake(runtime, org_id, invoice_id, document_id)

    return IntakeResult(
        entity_type=ENTITY_TYPE,
        entity_id=str(invoice.id),
        created=created,
        after_commit=start if created else None,
    )


DOCUMENT_INTAKE: Final = DocumentIntake(permission=INVOICE_UPLOAD, handle=handle, policy=pay_policy)
