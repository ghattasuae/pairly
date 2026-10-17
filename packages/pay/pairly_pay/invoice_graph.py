# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The ``pay_invoice`` graph (version 1): from a received invoice to ``ready`` or
``needs_review``.

::

    ingest -> wait_parse --parsed--> guardrails --e-invoice--> einvoice --> match
                                                --other------> sanitize -> extract -> match
                         --not parsed (fail closed)-----------------------------------> finish
    match -> finish -> END

* ``ingest``      binds the invoice to this run (``received`` to ``extracting``).
* ``wait_parse``  drives the document through the virus scan and the sandboxed parse
                  when this process has those services, else waits for the worker
                  (bounded).
* ``guardrails``  ``scan_document`` on the parsed invoice; the report is kept on the
                  invoice and any finding of medium severity or worse sends it to a
                  person.
* ``einvoice``    a structured e-invoice (UBL 2.1, PINT AE) is read exactly, without a
                  model (``pairly_pay.einvoice``), and checked like any extraction.
* ``sanitize``    builds the only text a model may see; its digest binds ``extract``.
* ``extract``     ``extract`` with the ``PayInvoice`` schema through the model router
                  (recorded responses in ``recorded`` mode), with the few-shot hints of
                  the vendor the document text points to (``pairly_pay.learning``).
* ``match``       links the vendor (TRN, IBAN blind index, sender domain, name).
* ``finish``      runs the registered checks (``CHECKS``: the WP4.6 validation and
                  fraud checks of ``pairly_pay.checks``, each in a savepoint, fail
                  closed), then stops at ``ready`` when nothing needs a person, else
                  ``needs_review`` with every reason. A bank mismatch opens a change
                  ticket (the vendor's payments are held) in this transaction.

Approvals and payment (WP4.7) start from ``ready`` invoices; this graph never approves,
schedules or pays. Fail closed at every step: a step that cannot run (no parse, model
down, no recording, guardrail error, unreadable e-invoice) records why on the invoice
and the run goes on to ``finish``, which can then only choose ``needs_review``. Graph
state carries ids only; each step reads what it needs from the database.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Final, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType
from pairly_core.documents.model import NormalizedDocument
from pairly_core.documents.pipeline import parse_document
from pairly_core.extraction.engine import ExtractionResult, FewShotHint, extract
from pairly_core.extraction.guarded import GuardedInput
from pairly_core.extraction.service import save_extraction
from pairly_core.guardrails import ScanContext, scan_document
from pairly_core.guardrails.report import GuardrailReport, text_digest
from pairly_core.guardrails.sanitize import ReportMismatchError
from pairly_core.ingestion.models import Document, DocumentStatus
from pairly_core.ingestion.scan import scan_document as virus_scan
from pairly_core.ingestion.storage import StorageError
from pairly_core.llm.router import ModelRouter
from pairly_core.llm.types import CallContext
from pairly_core.workflow.context import WorkflowContext
from pairly_core.workflow.runtime import node_retry
from pairly_pay.checks.service import CheckServices, load_settings, run_invoice_checks
from pairly_pay.coding import auto_code
from pairly_pay.common import WORKFLOW, By, audit
from pairly_pay.einvoice import is_einvoice, read_einvoice
from pairly_pay.invoice_schema import PayInvoice
from pairly_pay.invoices import (
    INVOICE_ENTITY,
    add_reasons,
    get_invoice,
    refresh_decisions,
    refresh_values,
    usable_values,
)
from pairly_pay.learning import vendor_hints
from pairly_pay.matching import match_vendor, prematch
from pairly_pay.models import DocumentType, ExtractionSource, InvoiceSource, InvoiceStatus
from pairly_pay.models import PayInvoice as PayInvoiceRow

log = structlog.get_logger("pairly_pay.invoice_graph")

GRAPH: Final = "pay_invoice"
VERSION: Final = 1
ACTOR_ID: Final = "pairly.pay.invoice_graph"
MAX_NORMALIZED_BYTES: Final = 64 * 1024 * 1024
PARSE_WAIT_SECONDS: Final = float(os.environ.get("PAIRLY_PAY_PARSE_WAIT_SECONDS", "30"))
PARSE_POLL_SECONDS: Final = 1.0
_PENDING = frozenset({DocumentStatus.RECEIVED, DocumentStatus.SCANNING, DocumentStatus.CLEAN})

# A check that runs in ``finish`` and returns review reasons (empty: nothing found).
# WP4.6 registers the validation and fraud checks (``pairly_pay.checks.service``).
InvoiceCheck = Callable[[AsyncSession, PayInvoiceRow, WorkflowContext], Awaitable[list[str]]]


async def validation_checks(
    session: AsyncSession, invoice: PayInvoiceRow, ctx: WorkflowContext
) -> list[str]:
    """Arithmetic, VAT, duplicates, bank mismatch, impersonation, amounts, fake
    signals, injection, policy and the risk score (``pairly_pay.checks``). When the
    organisation turned auto-coding on, high confidence GL codes are applied first
    (``pairly_pay.coding``), so the checks read them."""
    services = ctx.services
    if (await load_settings(session)).auto_code_high_confidence:
        try:
            async with session.begin_nested():
                await auto_code(session, invoice)
        except Exception as exc:  # coding is a convenience: a person codes it instead
            log.error("pay.invoice.auto_code_failed", error=type(exc).__name__)
    return await run_invoice_checks(
        session,
        invoice,
        CheckServices(
            org_id=ctx.org_id,
            store=services.store,
            router=services.router,
            embedder=services.embedder,
        ),
    )


CHECKS: list[InvoiceCheck] = [validation_checks]


class InvoiceState(TypedDict, total=False):
    invoice_id: str
    document_id: str


Ctx = Runtime[WorkflowContext]


# ------------------------------------------------------------------ helpers


def _ids(state: InvoiceState) -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.UUID(state["invoice_id"]), uuid.UUID(state["document_id"])


async def _reasons(ctx: WorkflowContext, invoice_id: uuid.UUID, *reasons: str) -> None:
    async with ctx.transaction() as session:
        invoice = await get_invoice(session, invoice_id, lock=True)
        add_reasons(invoice, reasons)
        invoice.updated_at = datetime.now(UTC)


async def _audit(
    ctx: WorkflowContext,
    session: AsyncSession,
    action: str,
    invoice_id: uuid.UUID,
    payload: dict[str, Any],
) -> None:
    await audit(
        session,
        By.system(ACTOR_ID),
        action,
        entity_type=INVOICE_ENTITY,
        entity_id=invoice_id,
        payload={"run_id": str(ctx.run_id), **payload},
    )


async def _document(ctx: WorkflowContext, document_id: uuid.UUID) -> Document:
    async with ctx.transaction() as session:
        doc = await session.scalar(
            select(Document)
            .where(Document.id == document_id)
            .execution_options(populate_existing=True)
        )
        if doc is None or doc.workflow != WORKFLOW:
            raise LookupError("invoice document not found")
        session.expunge(doc)
        return doc


async def _normalized(ctx: WorkflowContext, document_id: uuid.UUID) -> NormalizedDocument | str:
    """The parsed invoice, or the reason it cannot be read (fail closed)."""
    store = ctx.services.store
    if store is None:
        return "document_store_unavailable"
    doc = await _document(ctx, document_id)
    if doc.status != DocumentStatus.PARSED or not doc.parse_result_key:
        return f"document_not_parsed:{doc.status}"
    try:
        raw = await store.get_bytes(doc.parse_result_key, max_bytes=MAX_NORMALIZED_BYTES)
        return NormalizedDocument.model_validate_json(raw)
    except (StorageError, ValidationError, ValueError):
        return "parse_result_unavailable"


async def _snapshot(ctx: WorkflowContext, invoice_id: uuid.UUID) -> PayInvoiceRow:
    async with ctx.transaction() as session:
        invoice = await get_invoice(session, invoice_id)
        session.expunge(invoice)
    return invoice


def _report(invoice: PayInvoiceRow) -> GuardrailReport | None:
    if not invoice.guardrail_report:
        return None
    return GuardrailReport.model_validate(invoice.guardrail_report)


async def read_with_model(
    doc: NormalizedDocument,
    guarded: GuardedInput,
    *,
    router: ModelRouter,
    org_id: uuid.UUID,
    invoice_id: uuid.UUID,
    hints: Sequence[FewShotHint],
) -> ExtractionResult[PayInvoice]:
    """The model extraction of one invoice (the engine's ``core_extract`` prompt)."""
    return await extract(
        doc,
        guarded,
        PayInvoice,
        router=router,
        org_id=org_id,
        context=CallContext(
            actor_id=ACTOR_ID,
            workflow=WORKFLOW,
            entity_type=INVOICE_ENTITY,
            entity_id=str(invoice_id),
        ),
        hints=tuple(hints),
    )


async def _save(
    ctx: WorkflowContext,
    invoice_id: uuid.UUID,
    document_id: uuid.UUID,
    result: ExtractionResult[PayInvoice],
    source: ExtractionSource,
    template_key: str | None,
) -> None:
    async with ctx.transaction() as session:
        extraction_id = await save_extraction(
            session,
            result,
            document_id=document_id,
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR_ID,
            template_key=template_key,
        )
        invoice = await get_invoice(session, invoice_id, lock=True)
        invoice.extraction_id = extraction_id
        invoice.extraction_source = source.value
        await refresh_values(session, invoice)
        add_reasons(invoice, (f"extraction:{r}" for r in result.reasons))
        invoice.updated_at = datetime.now(UTC)
        await _audit(
            ctx,
            session,
            "pay.invoice.extracted",
            invoice_id,
            {
                "extraction_id": str(extraction_id),
                "source": source.value,
                "status": result.status.value,
                "reasons": list(result.reasons[:20]),
                "from_recording": result.model_call.from_recording,
            },
        )


# ------------------------------------------------------------------ nodes


async def ingest(state: InvoiceState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    invoice_id, document_id = _ids(state)
    async with ctx.transaction() as session:
        invoice = await get_invoice(session, invoice_id, lock=True)
        if invoice.document_id != document_id:
            raise LookupError("the run's document is not the invoice's document")
        if invoice.run_id is None and invoice.status == InvoiceStatus.RECEIVED.value:
            invoice.run_id = ctx.run_id
            invoice.status = InvoiceStatus.EXTRACTING.value
            invoice.updated_at = datetime.now(UTC)
            await _audit(ctx, session, "pay.invoice.extracting", invoice_id, {})
        elif invoice.run_id != ctx.run_id:
            # Another run owns it, or a person already closed it: nothing to do.
            return Command(goto=END)
    return Command(goto="wait_parse")


async def wait_parse(state: InvoiceState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    invoice_id, document_id = _ids(state)
    services = ctx.services
    doc = await _document(ctx, document_id)
    if doc.status in {DocumentStatus.RECEIVED, DocumentStatus.SCANNING} and (
        services.store is not None and services.scanner is not None
    ):
        await virus_scan(
            ctx.sessionmaker,
            services.store,
            services.scanner,
            org_id=ctx.org_id,
            document_id=document_id,
        )
        doc = await _document(ctx, document_id)
    if doc.status == DocumentStatus.CLEAN and (
        services.store is not None and services.parse_config is not None
    ):
        await parse_document(
            ctx.sessionmaker,
            services.store,
            org_id=ctx.org_id,
            document_id=document_id,
            config=services.parse_config,
        )
        doc = await _document(ctx, document_id)
    deadline = asyncio.get_running_loop().time() + PARSE_WAIT_SECONDS
    while doc.status in _PENDING and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(PARSE_POLL_SECONDS)
        doc = await _document(ctx, document_id)
    if doc.status == DocumentStatus.PARSED:
        return Command(goto="guardrails")
    reason = doc.status_reason or doc.status
    await _reasons(ctx, invoice_id, f"document_not_parsed:{reason}"[:200])
    return Command(goto="finish")


async def guardrails(state: InvoiceState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    invoice_id, document_id = _ids(state)
    normalized = await _normalized(ctx, document_id)
    if isinstance(normalized, str):
        await _reasons(ctx, invoice_id, normalized)
        return Command(goto="finish")
    doc = await _document(ctx, document_id)
    report = scan_document(normalized, context=ScanContext(filename=doc.original_filename))
    async with ctx.transaction() as session:
        invoice = await get_invoice(session, invoice_id, lock=True)
        invoice.guardrail_report = report.model_dump(mode="json")
        flagged = [f.id for f in report.flagged_findings()]
        if flagged:
            add_reasons(invoice, ["guardrails_flagged:" + ",".join(flagged[:20])])
        if report.errors:
            add_reasons(invoice, ["guardrails_error"])
        invoice.updated_at = datetime.now(UTC)
        await _audit(
            ctx,
            session,
            "pay.invoice.guardrails_scanned",
            invoice_id,
            {
                "findings": len(report.findings),
                "flagged": flagged[:20],
                "errors": len(report.errors),
                "types": sorted({f.type.value for f in report.findings}),
            },
        )
    return Command(goto="einvoice" if is_einvoice(normalized) else "sanitize")


async def einvoice(state: InvoiceState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    invoice_id, document_id = _ids(state)
    normalized = await _normalized(ctx, document_id)
    report = _report(await _snapshot(ctx, invoice_id))
    if isinstance(normalized, str) or report is None:
        await _reasons(ctx, invoice_id, "einvoice_unreadable")
        return Command(goto="match")
    try:
        guarded = GuardedInput.build(normalized, report)
        result = read_einvoice(normalized, guarded)
    except (ReportMismatchError, ValueError, KeyError) as exc:
        await _reasons(ctx, invoice_id, f"einvoice_unreadable:{type(exc).__name__}")
        return Command(goto="match")
    await _save(ctx, invoice_id, document_id, result, ExtractionSource.EINVOICE, None)
    return Command(goto="match")


async def sanitize(state: InvoiceState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    invoice_id, document_id = _ids(state)
    normalized = await _normalized(ctx, document_id)
    report = _report(await _snapshot(ctx, invoice_id))
    if isinstance(normalized, str) or report is None:
        await _reasons(ctx, invoice_id, "sanitize_failed")
        return Command(goto="match")
    try:
        guarded = GuardedInput.build(normalized, report)
    except ReportMismatchError:
        await _reasons(ctx, invoice_id, "guardrail_report_mismatch")
        return Command(goto="match")
    removed = [{"start": s.start, "end": s.end} for s in guarded.model_input.removed]
    async with ctx.transaction() as session:
        invoice = await get_invoice(session, invoice_id, lock=True)
        invoice.sanitized_sha256 = text_digest(guarded.text)
        invoice.removed_spans = removed
        invoice.updated_at = datetime.now(UTC)
        await _audit(
            ctx,
            session,
            "pay.invoice.sanitized",
            invoice_id,
            {
                "removed_spans": len(removed),
                "removed_by": list(guarded.model_input.removed_findings[:20]),
                "sanitized_sha256": invoice.sanitized_sha256,
            },
        )
    return Command(goto="extract")


async def extract_fields(state: InvoiceState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    invoice_id, document_id = _ids(state)
    normalized = await _normalized(ctx, document_id)
    invoice = await _snapshot(ctx, invoice_id)
    report = _report(invoice)
    if isinstance(normalized, str) or report is None:
        await _reasons(ctx, invoice_id, "extraction_skipped:no_document")
        return Command(goto="match")
    guarded = GuardedInput.build(normalized, report)
    if text_digest(guarded.text) != invoice.sanitized_sha256:
        # Not the text the sanitise step recorded: never send it to a model.
        await _reasons(ctx, invoice_id, "sanitized_input_changed")
        return Command(goto="match")
    router = ctx.services.router
    if router is None:
        await _reasons(ctx, invoice_id, "model_router_unavailable")
        return Command(goto="match")
    async with ctx.transaction() as session:
        # Which vendor the text points to (TRN or IBAN printed, or the sender): only to
        # pick that vendor's corrections as hints. Nothing is linked here.
        hinted = await prematch(
            session, ctx.org_id, text=guarded.text, domains=invoice.sender_domains
        )
        hints = await vendor_hints(session, hinted)
    result = await read_with_model(
        normalized, guarded, router=router, org_id=ctx.org_id, invoice_id=invoice_id, hints=hints
    )
    template_key = f"vendor:{hinted}" if hinted else None
    await _save(ctx, invoice_id, document_id, result, ExtractionSource.MODEL, template_key)
    if hints:
        async with ctx.transaction() as session:
            await _audit(
                ctx,
                session,
                "pay.invoice.hints_used",
                invoice_id,
                {"vendor_id": str(hinted), "hints": len(hints)},
            )
    return Command(goto="match")


async def match(state: InvoiceState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    invoice_id, _ = _ids(state)
    async with ctx.transaction() as session:
        invoice = await get_invoice(session, invoice_id, lock=True)
        stored = await refresh_values(session, invoice)
        values = usable_values(stored)
        iban = values.get("bank.iban")
        found = await match_vendor(
            session,
            ctx.org_id,
            trn=invoice.vendor_trn,
            iban=iban if isinstance(iban, str) else None,
            name=invoice.vendor_name,
            domains=invoice.sender_domains,
        )
        if invoice.source == InvoiceSource.VENDOR_PORTAL.value and invoice.vendor_id is not None:
            # Sent by a signed-in contact of this vendor: the vendor is known. What the
            # document says is kept for the reviewer; a different vendor is a reason.
            portal_vendor = invoice.vendor_id
            invoice.vendor_match = found.as_json() | {
                "vendor_id": str(portal_vendor),
                "method": "vendor_portal",
                "read_vendor_id": str(found.vendor_id) if found.vendor_id else None,
            }
            if found.vendor_id is not None and found.vendor_id != portal_vendor:
                add_reasons(invoice, ["portal_vendor_mismatch"])
        else:
            invoice.vendor_id = found.vendor_id
            invoice.vendor_match = found.as_json()
            add_reasons(invoice, found.reasons)
        invoice.updated_at = datetime.now(UTC)
        await _audit(
            ctx,
            session,
            "pay.invoice.vendor_matched",
            invoice_id,
            {
                "vendor_id": str(found.vendor_id) if found.vendor_id else None,
                "method": found.method,
                "conflict": found.conflict,
                "signals": sorted(found.candidates),
            },
        )
    return {}


async def finish(state: InvoiceState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    invoice_id, _ = _ids(state)
    async with ctx.transaction() as session:
        invoice = await get_invoice(session, invoice_id, lock=True)
        if invoice.status != InvoiceStatus.EXTRACTING.value or invoice.run_id != ctx.run_id:
            return {}  # a person acted meanwhile (rejected it): leave it as it is
        if invoice.extraction_id is None:
            add_reasons(invoice, ["not_read"])
        if invoice.document_type == DocumentType.CREDIT_NOTE.value:
            # A person always reads a credit note and applies it to its invoice.
            add_reasons(invoice, ["credit_note"])
        for check in CHECKS:
            try:
                # A savepoint: a check that fails leaves the transaction usable.
                async with session.begin_nested():
                    reasons = await check(session, invoice, ctx)
                add_reasons(invoice, reasons)
            except Exception as exc:  # a check that cannot run sends it to a person
                log.error("pay.invoice.check_failed", error=type(exc).__name__)
                await session.refresh(invoice)
                add_reasons(invoice, [f"check_failed:{type(exc).__name__}"])
        status = InvoiceStatus.NEEDS_REVIEW if invoice.review_reasons else InvoiceStatus.READY
        invoice.status = status.value
        invoice.updated_at = datetime.now(UTC)
        await _audit(
            ctx,
            session,
            f"pay.invoice.{status.value}",
            invoice_id,
            {"review_reasons": list(invoice.review_reasons[:20])},
        )
        await refresh_decisions(session, invoice)
    return {}


# ------------------------------------------------------------------ graph


def build_graph() -> StateGraph[Any, Any, Any, Any]:
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(InvoiceState, context_schema=WorkflowContext)
    retry = node_retry()
    graph.add_node(
        "ingest", ingest, retry_policy=retry, timeout=30, destinations=("wait_parse", END)
    )
    graph.add_node(
        "wait_parse",
        wait_parse,
        retry_policy=retry,
        timeout=PARSE_WAIT_SECONDS + 300,
        destinations=("guardrails", "finish"),
    )
    graph.add_node(
        "guardrails",
        guardrails,
        retry_policy=retry,
        timeout=120,
        destinations=("einvoice", "sanitize", "finish"),
    )
    graph.add_node("einvoice", einvoice, retry_policy=retry, timeout=60, destinations=("match",))
    graph.add_node(
        "sanitize", sanitize, retry_policy=retry, timeout=60, destinations=("extract", "match")
    )
    graph.add_node(
        "extract", extract_fields, retry_policy=retry, timeout=300, destinations=("match",)
    )
    graph.add_node("match", match, retry_policy=retry, timeout=60)
    graph.add_node("finish", finish, retry_policy=retry, timeout=60)
    graph.add_edge(START, "ingest")
    graph.add_edge("match", "finish")
    graph.add_edge("finish", END)
    return graph
