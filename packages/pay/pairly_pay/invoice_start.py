# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Starting the ``pay_invoice`` intake run, and the job that keeps intake moving.

Runs always start as the Pay intake agent (``agent_actor``: the ``agent`` role, which
holds ``pay.invoice.propose`` and nothing that decides), whoever sent the invoice. Two
ways in:

* the upload and API routes, after the response, when this process can scan and parse
  the document itself or it is already settled (``start_after_intake``);
* the worker job ``pay_invoice_intake_start`` every minute, for received invoices with
  no run (email intake, or an API process that could not start it). It finds the
  organisations through ``pay_pending_invoice_org_ids()`` (SECURITY DEFINER,
  organisation ids only, migration ``pay_0002``), then works in each one under Row
  Level Security. The same job sends an invoice whose run ended without finishing it
  (failed, cancelled) to a person: fail closed, never left ``extracting``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

import structlog
from sqlalchemy import select, text

from pairly_core.db import set_tenant
from pairly_core.ingestion.models import Document, DocumentStatus
from pairly_core.jobs.registry import JobContext, JobSpec
from pairly_core.rbac import agent_actor
from pairly_core.workflow.models import TERMINAL_RUN_STATUSES, WorkflowRun
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_pay.common import WORKFLOW, By, audit
from pairly_pay.invoice_graph import GRAPH
from pairly_pay.invoices import INVOICE_ENTITY, add_reasons, get_invoice
from pairly_pay.models import InvoiceStatus, PayInvoice

log = structlog.get_logger("pairly_pay.invoice_start")

# The Pay intake agent's fixed id (an agent, never a person).
AGENT_ID: Final = uuid.UUID("2f6b8c1d-4e3a-5d7f-9a1b-6c4e8d2f0a37")
JOB_NAME: Final = "pay_invoice_intake_start"
SETTLED: Final = frozenset(
    {
        DocumentStatus.PARSED.value,
        DocumentStatus.PARSE_FAILED.value,
        DocumentStatus.INFECTED.value,
        DocumentStatus.REJECTED.value,
        DocumentStatus.ERASED.value,
    }
)
# The job starts a run anyway (to fail closed in the run) after this long.
PATIENCE: Final = timedelta(minutes=10)
STUCK: Final = timedelta(minutes=15)
BATCH: Final = 50


async def start_invoice_run(
    runtime: WorkflowRuntime, org_id: uuid.UUID, invoice_id: uuid.UUID, document_id: uuid.UUID
) -> str | None:
    """Start the run. Returns its status, ``None`` on failure (the invoice stays
    ``received`` and the job tries again)."""
    actor = agent_actor(runtime.registry, org_id, AGENT_ID)
    try:
        view = await runtime.start_run(
            actor,
            workflow=WORKFLOW,
            graph=GRAPH,
            input={"invoice_id": str(invoice_id), "document_id": str(document_id)},
            entity_type=INVOICE_ENTITY,
            entity_id=str(invoice_id),
        )
    except Exception as exc:
        log.error(
            "pay_invoice_run_not_started", invoice_id=str(invoice_id), error=type(exc).__name__
        )
        return None
    return view.status


def can_drive(runtime: WorkflowRuntime) -> bool:
    services = runtime.services
    return (
        services.store is not None
        and services.scanner is not None
        and services.parse_config is not None
    )


async def start_after_intake(
    runtime: WorkflowRuntime, org_id: uuid.UUID, invoice_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    """The upload route's background task. Starts only when the run will not sit
    waiting for the worker: this process scans and parses, or the document is settled.
    Otherwise the worker job starts it once the document is parsed."""
    if not can_drive(runtime):
        session = runtime.sessionmaker()
        set_tenant(session, org_id, None)
        async with session, session.begin():
            status = await session.scalar(select(Document.status).where(Document.id == document_id))
        if status not in SETTLED:
            return
    await start_invoice_run(runtime, org_id, invoice_id, document_id)


# ------------------------------------------------------------------ the worker job


async def _org_ids(ctx: JobContext) -> list[uuid.UUID]:
    async with ctx.sessionmaker() as session, session.begin():
        rows = await session.execute(
            text("SELECT org_id FROM pay_pending_invoice_org_ids() AS org_id")
        )
        return [uuid.UUID(str(v)) for v in rows.scalars()]


async def fail_stuck(ctx: JobContext, org_id: uuid.UUID) -> int:
    """Invoices still ``extracting`` whose run is over: to a person."""
    session = ctx.sessionmaker()
    set_tenant(session, org_id, None)
    moved = 0
    async with session, session.begin():
        rows = await session.execute(
            select(PayInvoice.id, WorkflowRun.status)
            .join(WorkflowRun, WorkflowRun.id == PayInvoice.run_id)
            .where(
                PayInvoice.status == InvoiceStatus.EXTRACTING.value,
                PayInvoice.updated_at < datetime.now(UTC) - STUCK,
            )
            .limit(BATCH)
        )
        for row in rows.all():
            if row.status not in {s.value for s in TERMINAL_RUN_STATUSES}:
                continue
            invoice = await get_invoice(session, row.id, lock=True)
            if invoice.status != InvoiceStatus.EXTRACTING.value:
                continue
            add_reasons(invoice, [f"run_ended:{row.status}"])
            invoice.status = InvoiceStatus.NEEDS_REVIEW.value
            invoice.updated_at = datetime.now(UTC)
            await audit(
                session,
                By.system(JOB_NAME),
                "pay.invoice.needs_review",
                entity_type=INVOICE_ENTITY,
                entity_id=invoice.id,
                payload={"review_reasons": list(invoice.review_reasons[:20])},
            )
            moved += 1
    return moved


async def start_pending_org(ctx: JobContext, org_id: uuid.UUID) -> int:
    patience = datetime.now(UTC) - PATIENCE
    session = ctx.sessionmaker()
    set_tenant(session, org_id, None)
    async with session, session.begin():
        rows = await session.execute(
            select(PayInvoice.id, PayInvoice.document_id, PayInvoice.created_at, Document.status)
            .join(Document, Document.id == PayInvoice.document_id)
            .where(PayInvoice.status == InvoiceStatus.RECEIVED.value, PayInvoice.run_id.is_(None))
            .order_by(PayInvoice.created_at)
            .limit(BATCH)
        )
        pending = [
            (r.id, r.document_id)
            for r in rows
            if r.status in SETTLED or can_drive(ctx.runtime) or r.created_at < patience
        ]
    started = 0
    for invoice_id, document_id in pending:
        if await start_invoice_run(ctx.runtime, org_id, invoice_id, document_id) is not None:
            started += 1
    return started


async def start_pending(ctx: JobContext) -> dict[str, int]:
    """The plugin job. One failing organisation never stops the others."""
    started = moved = errors = 0
    for org_id in await _org_ids(ctx):
        try:
            moved += await fail_stuck(ctx, org_id)
            started += await start_pending_org(ctx, org_id)
        except Exception as exc:
            errors += 1
            log.error("pay_invoice_start_failed", org_id=str(org_id), error=type(exc).__name__)
    return {"started": started, "sent_to_review": moved, "errors": errors}


INTAKE_JOB: Final = JobSpec(
    JOB_NAME,
    "Start reading received invoices; send invoices whose run ended to a person",
    "every minute",
    run=start_pending,
    every_minutes=1,
)
