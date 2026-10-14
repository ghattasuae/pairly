# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Invoice routes (WP4.4, WP4.5). Every change is audited in the request's transaction.

* ``POST /invoices``                      multipart/form-data, one ``file`` part. Needs
  ``pay.invoice.upload``; people (source ``upload``) and API keys holding that scope
  (source ``api``). Streamed, hashed, sniffed, size and type checked, deduplicated by
  file hash: the same file again returns the invoice it already is (``duplicate``).
  The ``pay_invoice`` run starts after the response. 202. Safe to retry with the same
  ``Idempotency-Key``.
* ``GET  /invoices?status=&q=&limit=``    newest first (``pay.invoice.read``).
* ``GET  /invoices/{id}``                 one invoice for the side-by-side viewer: the
  document, every field with confidence, flags, evidence spans and corrections, the
  vendor link and how it was found, the guardrail findings (their text only for people
  who process invoices) and why it needs a person.
* ``POST /invoices/{id}/corrections``     correct one field, with a reason
  (``pay.invoice.process`` and ``pay.extraction.correct``; people only). Recorded by
  the engine and filed under the vendor so the next invoice learns from it.
* ``POST /invoices/{id}/vendor``          link the vendor (``pay.invoice.process``).
* ``POST /invoices/{id}/review``          ``needs_review`` to ``ready`` after a person
  checked it (``pay.invoice.process``).
* ``POST /invoices/{id}/reject``          close it, with a reason (``pay.invoice.process``).
* ``GET  /intake/address``                whether the Pay inbound address is set up
  (``pay.settings.manage``).
* ``POST /intake/address/rotate``         a new inbound address; the full address is
  shown once (``pay.settings.manage``, a second factor).
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from pairly_api.idempotency import idempotent
from pairly_api.problems import problem_response
from pairly_api.routes.approvals import get_runtime
from pairly_api.uploads import (
    MULTIPART_OVERHEAD,
    UnsupportedBodyError,
    enqueue_scan,
    get_services,
    receive_upload,
    rejected_response,
)
from pairly_core.errors import BadRequestError, ForbiddenError
from pairly_core.extraction.schema import path_pattern
from pairly_core.extraction.service import StoredExtraction, correct_field, load_extraction
from pairly_core.identity import Context, CurrentActor, OrgSession
from pairly_core.ingestion.addresses import inbound_address, rotate_inbound_token
from pairly_core.ingestion.models import Document, DocumentSource, InboundAddress
from pairly_core.ingestion.service import (
    AuditBy,
    RejectReason,
    TooLargeError,
    ingest_spooled,
    record_rejection,
)
from pairly_core.models.tenancy import Organisation
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_pay.common import WORKFLOW, By, audit, record_step
from pairly_pay.intake import pay_policy
from pairly_pay.invoice_start import start_after_intake
from pairly_pay.invoices import (
    INVOICE_ENTITY,
    InvoiceStateError,
    add_reasons,
    create_invoice,
    get_invoice,
    link_vendor,
    mark_reviewed,
    refresh_decisions,
    refresh_values,
    reject_invoice,
)
from pairly_pay.learning import file_correction
from pairly_pay.models import (
    InvoiceSource,
    InvoiceStatus,
    PayIntakeEmail,
    PayInvoice,
    PayVendor,
)
from pairly_pay.permissions import (
    EXTRACTION_CORRECT,
    INVOICE_PROCESS,
    INVOICE_READ,
    INVOICE_UPLOAD,
    SETTINGS_MANAGE,
)

log = structlog.get_logger("pairly_pay.api.invoices")

router = APIRouter(tags=["pay"])
Runtime = Annotated[WorkflowRuntime, Depends(get_runtime)]

MAX_LIST: Final = 200
STATUS_VALUES = Literal[
    "received", "extracting", "needs_review", "ready", "approved", "scheduled", "paid", "rejected"
]


# ------------------------------------------------------------------ schemas


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PayInvoiceCorrectionIn(_In):
    field_path: str = Field(min_length=1, max_length=200)
    new_value: str | None = Field(default=None, max_length=2000)
    reason: str = Field(min_length=1, max_length=1000)


class PayInvoiceVendorIn(_In):
    vendor_id: uuid.UUID


class PayInvoiceReviewIn(_In):
    note: str | None = Field(default=None, max_length=1000)


class PayInvoiceRejectIn(_In):
    reason: str = Field(min_length=1, max_length=1000)


class PayInvoiceVendorOut(BaseModel):
    id: uuid.UUID
    reference: str
    legal_name: str
    status: str


class PayInvoiceSummary(BaseModel):
    id: uuid.UUID
    reference: str
    status: str
    source: str
    document_id: uuid.UUID
    vendor: PayInvoiceVendorOut | None
    vendor_name: str | None
    invoice_number: str | None
    invoice_date: date | None
    due_date: date | None
    currency: str | None
    total: Decimal | None
    review_reasons: list[str]
    intake_reference: str | None
    created_at: datetime
    updated_at: datetime


class PayInvoiceList(BaseModel):
    items: list[PayInvoiceSummary]
    counts: dict[str, int]


class PayInvoiceCreated(PayInvoiceSummary):
    duplicate: bool


class PayInvoiceFieldOut(BaseModel):
    path: str
    pattern: str
    kind: str
    value: Any
    current_value: Any
    raw_value: str | None
    confidence: float
    threshold: float
    required: bool
    needs_correction: bool
    corrected: bool
    flags: list[str]
    spans: list[dict[str, Any]]


class PayInvoiceCorrectionOut(BaseModel):
    id: uuid.UUID
    field_path: str
    old_value: Any
    new_value: Any
    corrected_by: uuid.UUID
    reason: str
    created_at: datetime


class PayInvoiceExtractionOut(BaseModel):
    id: uuid.UUID
    status: str
    source: str | None
    reasons: list[str]
    model_name: str | None
    prompt_version: str
    from_recording: bool
    fields: list[PayInvoiceFieldOut]
    corrections: list[PayInvoiceCorrectionOut]
    # Paths a person added that were never read (for example a missed line item).
    extra_values: dict[str, Any]


class PayInvoiceFindingOut(BaseModel):
    id: str
    type: str
    code: str
    severity: str
    location: str
    page: int | None
    bbox: dict[str, float] | None
    remove: bool
    # The flagged text: only for people who process invoices.
    evidence: str | None


class PayInvoiceDocumentOut(BaseModel):
    id: uuid.UUID
    media_type: str
    original_filename: str | None
    status: str
    sha256: str


class PayInvoiceEmailOut(BaseModel):
    reference: str
    classification: str
    reasons: list[str]
    sender_domain: str | None
    forwarded_domain: str | None
    receipt_sent: bool


class PayInvoiceDetail(PayInvoiceSummary):
    po_number: str | None
    subtotal: Decimal | None
    vat_amount: Decimal | None
    vendor_trn: str | None
    sender_domains: list[str]
    vendor_match: dict[str, Any]
    document: PayInvoiceDocumentOut
    extraction: PayInvoiceExtractionOut | None
    findings: list[PayInvoiceFindingOut]
    removed_spans: list[dict[str, Any]]
    email: PayInvoiceEmailOut | None
    decision_ids: list[str]
    reviewed_at: datetime | None
    rejected_at: datetime | None
    can_process: bool


class PayInboundAddressOut(BaseModel):
    configured: bool
    rotated_at: datetime | None
    # Only right after a rotation: the token is never stored, so never shown again.
    address: str | None = None


# ------------------------------------------------------------------ helpers


async def _vendors(session: OrgSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, PayVendor]:
    if not ids:
        return {}
    rows = await session.scalars(select(PayVendor).where(PayVendor.id.in_(list(ids))))
    return {v.id: v for v in rows}


def _vendor_out(vendor: PayVendor | None) -> PayInvoiceVendorOut | None:
    if vendor is None:
        return None
    return PayInvoiceVendorOut(
        id=vendor.id, reference=vendor.reference, legal_name=vendor.legal_name, status=vendor.status
    )


def _summary_fields(invoice: PayInvoice, vendor: PayVendor | None) -> dict[str, Any]:
    return {
        "id": invoice.id,
        "reference": invoice.reference,
        "status": invoice.status,
        "source": invoice.source,
        "document_id": invoice.document_id,
        "vendor": _vendor_out(vendor),
        "vendor_name": invoice.vendor_name,
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.invoice_date,
        "due_date": invoice.due_date,
        "currency": invoice.currency,
        "total": invoice.total,
        "review_reasons": list(invoice.review_reasons),
        "intake_reference": invoice.intake_reference,
        "created_at": invoice.created_at,
        "updated_at": invoice.updated_at,
    }


def _extraction(stored: StoredExtraction, source: str | None) -> PayInvoiceExtractionOut:
    row = stored.row
    current = stored.current_values()
    corrected = {c.field_path for c in stored.corrections}
    known = {f.path for f in stored.fields}
    return PayInvoiceExtractionOut(
        id=row.id,
        status=row.status,
        source=source,
        reasons=list(row.reasons),
        model_name=row.model_name,
        prompt_version=row.prompt_version,
        from_recording=row.from_recording,
        fields=[
            PayInvoiceFieldOut(
                path=f.path,
                pattern=f.pattern,
                kind=f.kind,
                value=f.value,
                current_value=current.get(f.path),
                raw_value=f.raw_value,
                confidence=f.confidence,
                threshold=f.threshold,
                required=f.required,
                needs_correction=f.needs_correction and f.path not in corrected,
                corrected=f.path in corrected,
                flags=list(f.flags),
                spans=list(f.spans),
            )
            for f in stored.fields
        ],
        corrections=[
            PayInvoiceCorrectionOut(
                id=c.id,
                field_path=c.field_path,
                old_value=c.old_value,
                new_value=c.new_value,
                corrected_by=c.corrected_by,
                reason=c.reason,
                created_at=c.created_at,
            )
            for c in stored.corrections
        ],
        extra_values={p: v for p, v in current.items() if p not in known},
    )


def _findings(invoice: PayInvoice, processor: bool) -> list[PayInvoiceFindingOut]:
    report = invoice.guardrail_report or {}
    out: list[PayInvoiceFindingOut] = []
    for item in list(report.get("findings") or [])[:200]:
        if not isinstance(item, dict):
            continue
        bbox = item.get("bbox")
        out.append(
            PayInvoiceFindingOut(
                id=str(item.get("id", "")),
                type=str(item.get("type", "")),
                code=str(item.get("code", "")),
                severity=str(item.get("severity", "")),
                location=str(item.get("location", "")),
                page=item.get("page") if isinstance(item.get("page"), int) else None,
                bbox=bbox if isinstance(bbox, dict) else None,
                remove=bool(item.get("remove", False)),
                evidence=str(item.get("evidence", ""))[:500] if processor else None,
            )
        )
    return out


async def _detail(session: OrgSession, actor: Actor, invoice: PayInvoice) -> PayInvoiceDetail:
    vendors = await _vendors(session, {invoice.vendor_id} if invoice.vendor_id else set())
    document = await session.scalar(select(Document).where(Document.id == invoice.document_id))
    if document is None:  # pragma: no cover - a foreign key
        raise BadRequestError("the invoice document is missing")
    stored = (
        await load_extraction(session, invoice.extraction_id) if invoice.extraction_id else None
    )
    email = (
        await session.scalar(
            select(PayIntakeEmail).where(PayIntakeEmail.id == invoice.intake_email_id)
        )
        if invoice.intake_email_id
        else None
    )
    processor = actor.has(INVOICE_PROCESS)
    return PayInvoiceDetail(
        **_summary_fields(invoice, vendors.get(invoice.vendor_id) if invoice.vendor_id else None),
        po_number=invoice.po_number,
        subtotal=invoice.subtotal,
        vat_amount=invoice.vat_amount,
        vendor_trn=invoice.vendor_trn,
        sender_domains=list(invoice.sender_domains),
        vendor_match=dict(invoice.vendor_match),
        document=PayInvoiceDocumentOut(
            id=document.id,
            media_type=document.media_type,
            original_filename=document.original_filename,
            status=document.status,
            sha256=document.sha256,
        ),
        extraction=_extraction(stored, invoice.extraction_source) if stored else None,
        findings=_findings(invoice, processor),
        removed_spans=list(invoice.removed_spans),
        email=PayInvoiceEmailOut(
            reference=email.reference,
            classification=email.classification,
            reasons=list(email.reasons),
            sender_domain=email.sender_domain,
            forwarded_domain=email.forwarded_domain,
            receipt_sent=email.receipt_sent,
        )
        if email
        else None,
        decision_ids=list(invoice.decision_ids),
        reviewed_at=invoice.reviewed_at,
        rejected_at=invoice.rejected_at,
        can_process=processor,
    )


def _person(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can do this")


# ------------------------------------------------------------------ intake


@router.post(
    "/invoices",
    status_code=202,
    response_model=PayInvoiceCreated,
    dependencies=[Depends(idempotent, scope="function")],
    responses={
        400: {"description": "Malformed body or an empty file"},
        413: {"description": "File larger than Pay allows"},
        415: {"description": "File type not allowed, or the body is not multipart"},
    },
)
async def submit_invoice(
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
    runtime: Runtime,
    background: BackgroundTasks,
) -> Any:
    authorize(actor, INVOICE_UPLOAD)
    if actor.kind not in {ActorKind.USER, ActorKind.API_KEY}:
        raise ForbiddenError("invoices come from a person or an API key")
    source = InvoiceSource.UPLOAD if actor.kind is ActorKind.USER else InvoiceSource.API
    doc_source = DocumentSource.UPLOAD if actor.kind is ActorKind.USER else DocumentSource.API
    services = get_services(request)
    policy = pay_policy(services.settings)
    audit_by = AuditBy.from_actor(actor)
    declared = request.headers.get("content-length")
    if (
        declared is not None
        and declared.isdigit()
        and int(declared) > policy.max_bytes + MULTIPART_OVERHEAD
    ):
        await record_rejection(
            session,
            by=audit_by,
            workflow=WORKFLOW,
            source=doc_source,
            reason=RejectReason.TOO_LARGE,
        )
        return rejected_response(request, RejectReason.TOO_LARGE, WORKFLOW)
    # Release the database connection while the body streams in.
    await session.commit()
    with tempfile.TemporaryDirectory(prefix="pairly-pay-", dir=services.tmp_dir) as tmp:
        try:
            spooled, filename = await receive_upload(
                request, Path(tmp), policy.max_bytes, services.settings.upload_timeout_seconds
            )
        except UnsupportedBodyError:
            return problem_response(
                request,
                415,
                "unsupported_media_type",
                detail="send multipart/form-data with a 'file' part",
            )
        except TooLargeError:
            await record_rejection(
                session,
                by=audit_by,
                workflow=WORKFLOW,
                source=doc_source,
                reason=RejectReason.TOO_LARGE,
            )
            return rejected_response(request, RejectReason.TOO_LARGE, WORKFLOW)
        outcome = await ingest_spooled(
            session,
            services.store,
            spooled=spooled,
            workflow=WORKFLOW,
            source=doc_source,
            filename=filename,
            policy=policy,
            by=audit_by,
            submitted_by=actor.id if actor.kind is ActorKind.USER else None,
        )
    if outcome.reason is not None or outcome.document is None:
        return rejected_response(request, outcome.reason or RejectReason.EMPTY, WORKFLOW)
    document = outcome.document
    invoice, created = await create_invoice(
        session, document=document, source=source, by=By.of(actor)
    )
    vendors = await _vendors(session, {invoice.vendor_id} if invoice.vendor_id else set())
    body = PayInvoiceCreated(
        **_summary_fields(invoice, vendors.get(invoice.vendor_id) if invoice.vendor_id else None),
        duplicate=not created,
    )
    # The run must see the invoice: commit before it starts (after the response).
    await session.commit()
    if created:
        if outcome.created and runtime.services.scanner is None:
            # No scanner here: the worker scans and parses; the run waits for it.
            background.add_task(enqueue_scan, services.queue, actor.org_id, document.id)
        background.add_task(start_after_intake, runtime, actor.org_id, invoice.id, document.id)
    return JSONResponse(body.model_dump(mode="json"), status_code=202)


# ------------------------------------------------------------------ reading


@router.get("/invoices", response_model=PayInvoiceList)
async def list_invoices(
    actor: CurrentActor,
    session: OrgSession,
    status: Annotated[STATUS_VALUES | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 100,
) -> PayInvoiceList:
    authorize(actor, INVOICE_READ)
    query = select(PayInvoice)
    if status is not None:
        query = query.where(PayInvoice.status == status)
    term = (q or "").strip()
    if term:
        like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(
            or_(
                PayInvoice.reference.ilike(like, escape="\\"),
                PayInvoice.invoice_number.ilike(like, escape="\\"),
                PayInvoice.vendor_name.ilike(like, escape="\\"),
            )
        )
    rows = list(
        await session.scalars(
            query.order_by(PayInvoice.created_at.desc(), PayInvoice.id).limit(limit)
        )
    )
    vendors = await _vendors(session, {r.vendor_id for r in rows if r.vendor_id})
    counts_rows = await session.execute(
        select(PayInvoice.status, func.count()).group_by(PayInvoice.status)
    )
    counts = {str(status): int(count) for status, count in counts_rows.all()}
    return PayInvoiceList(
        items=[
            PayInvoiceSummary(
                **_summary_fields(r, vendors.get(r.vendor_id) if r.vendor_id else None)
            )
            for r in rows
        ],
        counts=counts,
    )


@router.get("/invoices/{invoice_id}", response_model=PayInvoiceDetail)
async def get_invoice_detail(
    invoice_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> PayInvoiceDetail:
    authorize(actor, INVOICE_READ)
    invoice = await get_invoice(session, invoice_id)
    return await _detail(session, actor, invoice)


# ------------------------------------------------------------------ a person's actions


@router.post("/invoices/{invoice_id}/corrections", response_model=PayInvoiceDetail)
async def correct_invoice_field(
    invoice_id: uuid.UUID,
    body: PayInvoiceCorrectionIn,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
) -> PayInvoiceDetail:
    authorize(actor, INVOICE_PROCESS)
    authorize(actor, EXTRACTION_CORRECT)
    _person(actor)
    invoice = await get_invoice(session, invoice_id, lock=True)
    if invoice.status not in {InvoiceStatus.NEEDS_REVIEW.value, InvoiceStatus.READY.value}:
        raise InvoiceStateError(f"the invoice is {invoice.status}")
    if invoice.extraction_id is None:
        raise InvoiceStateError("nothing was read from this invoice yet")
    correction = await correct_field(
        session,
        actor,
        ctx.registry,
        invoice.extraction_id,
        field_path=body.field_path,
        new_value=body.new_value,
        reason=body.reason,
    )
    await file_correction(session, invoice, correction, path_pattern(body.field_path))
    await refresh_values(session, invoice)
    if invoice.status == InvoiceStatus.READY.value:
        # What was checked changed: a person looks again before it moves on.
        invoice.status = InvoiceStatus.NEEDS_REVIEW.value
        add_reasons(invoice, ["corrected_after_ready"])
    invoice.updated_at = correction.created_at
    await record_step(
        session,
        By.of(actor),
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        action="pay.invoice.corrected",
        inputs={"field_path": body.field_path, "correction_id": str(correction.id)},
        output={"status": invoice.status},
        reason=correction.reason,
    )
    await audit(
        session,
        actor,
        "pay.invoice.corrected",
        entity_type=INVOICE_ENTITY,
        entity_id=invoice.id,
        payload={
            "field_path": body.field_path,
            "correction_id": str(correction.id),
            "vendor_id": str(invoice.vendor_id) if invoice.vendor_id else None,
        },
    )
    await refresh_decisions(session, invoice)
    return await _detail(session, actor, invoice)


@router.post("/invoices/{invoice_id}/vendor", response_model=PayInvoiceDetail)
async def link_invoice_vendor(
    invoice_id: uuid.UUID, body: PayInvoiceVendorIn, actor: CurrentActor, session: OrgSession
) -> PayInvoiceDetail:
    authorize(actor, INVOICE_PROCESS)
    _person(actor)
    invoice = await link_vendor(session, actor, invoice_id, body.vendor_id)
    return await _detail(session, actor, invoice)


@router.post("/invoices/{invoice_id}/review", response_model=PayInvoiceDetail)
async def review_invoice(
    invoice_id: uuid.UUID, body: PayInvoiceReviewIn, actor: CurrentActor, session: OrgSession
) -> PayInvoiceDetail:
    authorize(actor, INVOICE_PROCESS)
    _person(actor)
    invoice = await mark_reviewed(session, actor, invoice_id, body.note)
    return await _detail(session, actor, invoice)


@router.post("/invoices/{invoice_id}/reject", response_model=PayInvoiceDetail)
async def reject(
    invoice_id: uuid.UUID, body: PayInvoiceRejectIn, actor: CurrentActor, session: OrgSession
) -> PayInvoiceDetail:
    authorize(actor, INVOICE_PROCESS)
    _person(actor)
    invoice = await reject_invoice(session, actor, invoice_id, body.reason)
    return await _detail(session, actor, invoice)


# ------------------------------------------------------------------ inbound address


@router.get("/intake/address", response_model=PayInboundAddressOut)
async def get_inbound_address(actor: CurrentActor, session: OrgSession) -> PayInboundAddressOut:
    authorize(actor, SETTINGS_MANAGE)
    row = await session.scalar(select(InboundAddress).where(InboundAddress.workflow == WORKFLOW))
    return PayInboundAddressOut(
        configured=row is not None, rotated_at=row.rotated_at if row else None
    )


@router.post("/intake/address/rotate", response_model=PayInboundAddressOut)
async def rotate_inbound_address(
    request: Request, actor: CurrentActor, session: OrgSession
) -> PayInboundAddressOut:
    authorize(actor, SETTINGS_MANAGE)
    _person(actor)
    org = await session.scalar(select(Organisation).where(Organisation.id == actor.org_id))
    if org is None:  # pragma: no cover - the actor's own organisation
        raise BadRequestError("organisation not found")
    by = By.of(actor)
    token = await rotate_inbound_token(
        session, workflow=WORKFLOW, actor_type=by.actor_type, actor_id=by.actor_id
    )
    domain = get_services(request).settings.inbound_email_domain
    row = await session.scalar(select(InboundAddress).where(InboundAddress.workflow == WORKFLOW))
    return PayInboundAddressOut(
        configured=True,
        rotated_at=row.rotated_at if row else None,
        address=inbound_address(WORKFLOW, org.slug, token, domain),
    )
