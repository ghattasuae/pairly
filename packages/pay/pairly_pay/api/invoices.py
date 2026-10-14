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
  who process invoices), why it needs a person, every check's result with its findings
  (one sentence each, English and Arabic), the risk score, and each suspected original
  of a duplicate side by side (``compare``).
* ``POST /invoices/{id}/corrections``     correct one field, with a reason
  (``pay.invoice.process`` and ``pay.extraction.correct``; people only). Recorded by
  the engine and filed under the vendor so the next invoice learns from it.
* ``POST /invoices/{id}/vendor``          link the vendor (``pay.invoice.process``).
* ``POST /invoices/{id}/coding``          department and category, which the approval
  rules route on (``pay.invoice.process``).
* ``POST /invoices/{id}/checks``          run every check again (``pay.invoice.process``).
* ``POST /invoices/{id}/review``          ``needs_review`` to ``ready`` after a person
  checked it (``pay.invoice.process``). Refused while the checks are stale.

Security: a correction, a vendor link and a coding change re-run every check and the
risk in the same transaction (``recheck_invoice``), so the results always describe the
invoice as it is. A bank-detail correction that differs from the vendor master opens or
joins the change ticket and holds the vendor, like the automatic check.
* ``POST /invoices/{id}/reject``          close it, with a reason (``pay.invoice.process``).
* ``POST /invoices/{id}/submit``          send a ``ready`` invoice for approval
  (``pay.invoice.submit``, ``pairly_pay.approvals``). The approvers decide in the
  engine's inbox, from the signed email links or from Slack.

The inbound email address is made with ``pairly-admin inbound rotate`` (the token is
shown once; the application role cannot write addresses).
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
from pydantic import BaseModel, ConfigDict, Field, ValidationError
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
from pairly_core.documents.model import NormalizedDocument
from pairly_core.errors import BadRequestError, ForbiddenError
from pairly_core.extraction.schema import path_pattern
from pairly_core.extraction.service import StoredExtraction, correct_field, load_extraction
from pairly_core.identity import Context, CurrentActor, OrgSession
from pairly_core.ingestion.models import Document, DocumentSource
from pairly_core.ingestion.service import (
    AuditBy,
    RejectReason,
    TooLargeError,
    ingest_spooled,
    record_rejection,
)
from pairly_core.ingestion.storage import StorageError
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_pay.approvals import GRAPH as APPROVAL_GRAPH
from pairly_pay.approvals import NotApprovableError, back_to_person, submit_for_approval
from pairly_pay.checks.service import (
    CheckServices,
    RecheckTrigger,
    lines_of,
    recheck_invoice,
    suspected_originals,
)
from pairly_pay.common import WORKFLOW, By, audit, record_step
from pairly_pay.freshness import FAILED, MISSING, STALE, checks_problem
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
    set_coding,
)
from pairly_pay.learning import file_correction
from pairly_pay.models import (
    InvoiceSource,
    InvoiceStatus,
    PayIntakeEmail,
    PayInvoice,
    PayVendor,
)
from pairly_pay.payability import blockers
from pairly_pay.permissions import (
    EXTRACTION_CORRECT,
    INVOICE_PROCESS,
    INVOICE_READ,
    INVOICE_SUBMIT,
    INVOICE_UPLOAD,
)

log = structlog.get_logger("pairly_pay.api.invoices")

CHECKS_STATE: Final[dict[str | None, Literal["current", "stale", "failed", "missing"]]] = {
    None: "current",
    STALE: "stale",
    FAILED: "failed",
    MISSING: "missing",
}

router = APIRouter(tags=["pay"])
Runtime = Annotated[WorkflowRuntime, Depends(get_runtime)]

MAX_LIST: Final = 200
STATUS_VALUES = Literal[
    "received",
    "extracting",
    "needs_review",
    "ready",
    "awaiting_approval",
    "approved",
    "scheduled",
    "paid",
    "rejected",
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


CodingKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class PayInvoiceCodingIn(_In):
    department: CodingKey | None = None
    category: CodingKey | None = None


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
    department: str | None = None
    category: str | None = None
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
    # The parsed text, for documents without pages to draw (email body, DOCX, XML):
    # the viewer shows it and marks evidence by its offsets.
    text: str | None = None


class PayInvoiceEmailOut(BaseModel):
    reference: str
    classification: str
    reasons: list[str]
    sender_domain: str | None
    forwarded_domain: str | None
    receipt_sent: bool


class PayCheckFindingOut(BaseModel):
    code: str
    rule: str
    severity: str
    # One sentence in English and Arabic.
    message: dict[str, str]
    evidence: dict[str, Any]
    original_id: uuid.UUID | None
    related_ids: list[uuid.UUID]
    needs_review: bool


class PayCheckOut(BaseModel):
    check: str
    status: Literal["pass", "fail", "error"]
    error: str | None
    findings: list[PayCheckFindingOut]


class PayRiskOut(BaseModel):
    assessment_id: uuid.UUID | None
    score: float | None
    level: str
    requires_human: bool
    reasons: list[str]
    explanation: dict[str, str]


class PayCompareLineOut(BaseModel):
    description: str | None
    quantity: str | None
    unit_price: str | None
    line_total: str | None


class PayCompareSideOut(BaseModel):
    id: uuid.UUID
    reference: str
    status: str
    document_id: uuid.UUID
    vendor_name: str | None
    invoice_number: str | None
    invoice_date: date | None
    due_date: date | None
    currency: str | None
    po_number: str | None
    subtotal: Decimal | None
    vat_amount: Decimal | None
    total: Decimal | None
    lines: list[PayCompareLineOut]
    created_at: datetime


class PayCompareOut(BaseModel):
    """A suspected original, side by side with this invoice (brief 4.4.2)."""

    rules: list[str]
    invoice: PayCompareSideOut
    original: PayCompareSideOut


class PayInvoiceApprovalOut(BaseModel):
    """Who sent it for approval, the engine approval, and what was approved."""

    submitted_by: uuid.UUID | None
    submitted_at: datetime | None
    approval_id: uuid.UUID | None
    run_id: uuid.UUID | None
    approved_at: datetime | None
    approved_by_rule: bool
    approver_ids: list[str]
    approved_amount: Decimal | None
    approved_currency: str | None
    approved_account_id: uuid.UUID | None


class PayInvoiceSubmitIn(_In):
    note: str | None = Field(default=None, max_length=1000)


class PayInvoiceDetail(PayInvoiceSummary):
    po_number: str | None
    # Purchase order matching (``pairly_pay.checks.matching``): the PO the invoice cites
    # when it is on file, and how its lines were allocated ({status, po_id, lines}).
    po_id: uuid.UUID | None = None
    po_match: dict[str, Any] = Field(default_factory=dict)
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
    # ``current``: the check results are about the invoice as it is now; ``stale``: it
    # changed since (run the checks again); ``failed``: a check could not run;
    # ``missing``: the checks never finished. Only ``current`` may be approved or paid.
    checks_state: Literal["current", "stale", "failed", "missing"] = "missing"
    # Why it cannot be approved or paid now (``pairly_pay.payability``; empty: nothing).
    blockers: list[str] = Field(default_factory=list)
    approval: PayInvoiceApprovalOut | None = None
    # Payment (``pairly_pay.payment_runs``): the run that pays it, and when it was paid.
    payment_run_id: uuid.UUID | None = None
    paid_at: datetime | None = None
    payment_reference: str | None = None
    can_process: bool
    # Validation and fraud checks (WP4.6): every check's result, the risk score, and the
    # suspected originals of a duplicate side by side.
    checks: list[PayCheckOut] = Field(default_factory=list)
    risk: PayRiskOut | None = None
    compare: list[PayCompareOut] = Field(default_factory=list)


# ------------------------------------------------------------------ helpers


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _checks(invoice: PayInvoice) -> list[PayCheckOut]:
    out: list[PayCheckOut] = []
    for item in invoice.checks[:20]:
        if not isinstance(item, dict) or item.get("status") not in {"pass", "fail", "error"}:
            continue
        findings: list[PayCheckFindingOut] = []
        for f in list(item.get("findings") or [])[:40]:
            if not isinstance(f, dict):
                continue
            raw = f.get("message")
            message: dict[str, Any] = raw if isinstance(raw, dict) else {}
            findings.append(
                PayCheckFindingOut(
                    code=str(f.get("code", "")),
                    rule=str(f.get("rule", "")),
                    severity=str(f.get("severity", "")),
                    message={"en": str(message.get("en", "")), "ar": str(message.get("ar", ""))},
                    evidence=dict(f.get("evidence") or {}),
                    original_id=_uuid(f.get("original_id")) if f.get("original_id") else None,
                    related_ids=[
                        u for u in (_uuid(r) for r in f.get("related_ids") or []) if u is not None
                    ],
                    needs_review=bool(f.get("needs_review")),
                )
            )
        out.append(
            PayCheckOut(
                check=str(item.get("check", "")),
                status=item["status"],
                error=str(item["error"]) if item.get("error") else None,
                findings=findings,
            )
        )
    return out


def _risk(invoice: PayInvoice) -> PayRiskOut | None:
    data = invoice.risk or {}
    if not data:
        return None
    raw = data.get("explanation")
    explanation: dict[str, Any] = raw if isinstance(raw, dict) else {}
    score = data.get("score")
    return PayRiskOut(
        assessment_id=_uuid(data.get("assessment_id")) if data.get("assessment_id") else None,
        score=float(score) if isinstance(score, int | float) else None,
        level=str(data.get("level", "high")),
        requires_human=bool(data.get("requires_human", True)),
        reasons=[str(r) for r in data.get("reasons") or []][:20],
        explanation={"en": str(explanation.get("en", "")), "ar": str(explanation.get("ar", ""))},
    )


def _side(invoice: PayInvoice, stored: StoredExtraction | None) -> PayCompareSideOut:
    values = stored.current_values() if stored is not None else {}
    lines = lines_of(values)

    def text(value: Decimal | None) -> str | None:
        return None if value is None else f"{value.normalize():f}"

    return PayCompareSideOut(
        id=invoice.id,
        reference=invoice.reference,
        status=invoice.status,
        document_id=invoice.document_id,
        vendor_name=invoice.vendor_name,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        currency=invoice.currency,
        po_number=invoice.po_number,
        subtotal=money(invoice.subtotal),
        vat_amount=money(invoice.vat_amount),
        total=money(invoice.total),
        lines=[
            PayCompareLineOut(
                description=x.description,
                quantity=text(x.quantity),
                unit_price=text(x.unit_price),
                line_total=text(x.line_total),
            )
            for x in lines[:100]
        ],
        created_at=invoice.created_at,
    )


async def _compare(
    session: OrgSession, invoice: PayInvoice, stored: StoredExtraction | None
) -> list[PayCompareOut]:
    """The suspected originals (this organisation's only: the session is org bound)."""
    pairs = suspected_originals(invoice)[:5]
    if not pairs:
        return []
    wanted = {u: rules for value, rules in pairs if (u := _uuid(value)) is not None}
    rows = await session.scalars(select(PayInvoice).where(PayInvoice.id.in_(list(wanted))))
    mine = _side(invoice, stored)
    out: list[PayCompareOut] = []
    for row in rows:
        other = await load_extraction(session, row.extraction_id) if row.extraction_id else None
        out.append(
            PayCompareOut(
                rules=sorted(set(wanted[row.id])), invoice=mine, original=_side(row, other)
            )
        )
    return out


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


_CENT: Final = Decimal("0.01")


def money(value: Decimal | None) -> Decimal | None:
    """The stored amount as printed: two places, more only when the invoice had more
    (the column keeps four, for three-place currencies)."""
    if value is None:
        return None
    cents = value.quantize(_CENT)
    return cents if cents == value else value.normalize()


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
        "total": money(invoice.total),
        "review_reasons": list(invoice.review_reasons),
        "intake_reference": invoice.intake_reference,
        "department": invoice.department,
        "category": invoice.category,
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


_DRAWN: Final = ("application/pdf", "image/")
MAX_TEXT: Final = 200_000


async def _text(request: Request, document: Document) -> str | None:
    """The parsed text of a document the viewer cannot draw, or None."""
    if document.media_type.startswith(_DRAWN) or not document.parse_result_key:
        return None
    try:
        raw = await get_services(request).store.get_bytes(
            document.parse_result_key, max_bytes=64 * 1024 * 1024
        )
        return NormalizedDocument.model_validate_json(raw).text[:MAX_TEXT]
    except (StorageError, ValidationError, ValueError):
        return None


async def _detail(
    request: Request, session: OrgSession, actor: Actor, invoice: PayInvoice
) -> PayInvoiceDetail:
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
        po_id=invoice.po_id,
        po_match=dict(invoice.po_match or {}),
        subtotal=money(invoice.subtotal),
        vat_amount=money(invoice.vat_amount),
        vendor_trn=invoice.vendor_trn,
        sender_domains=list(invoice.sender_domains),
        vendor_match=dict(invoice.vendor_match),
        document=PayInvoiceDocumentOut(
            id=document.id,
            media_type=document.media_type,
            original_filename=document.original_filename,
            status=document.status,
            sha256=document.sha256,
            text=await _text(request, document),
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
        checks=_checks(invoice),
        risk=_risk(invoice),
        compare=await _compare(session, invoice, stored),
        checks_state=CHECKS_STATE[await checks_problem(session, invoice)],
        blockers=await blockers(session, invoice)
        if invoice.status
        in {
            InvoiceStatus.READY.value,
            InvoiceStatus.AWAITING_APPROVAL.value,
            InvoiceStatus.APPROVED.value,
            InvoiceStatus.SCHEDULED.value,
        }
        else [],
        approval=PayInvoiceApprovalOut(
            submitted_by=invoice.approval_submitted_by,
            submitted_at=invoice.approval_submitted_at,
            approval_id=invoice.approval_id,
            run_id=invoice.approval_run_id,
            approved_at=invoice.approved_at,
            approved_by_rule=invoice.approved_by_rule,
            approver_ids=list(invoice.approver_ids),
            approved_amount=money(invoice.approved_amount),
            approved_currency=invoice.approved_currency,
            approved_account_id=invoice.approved_account_id,
        )
        if invoice.approval_submitted_at is not None
        else None,
        payment_run_id=invoice.payment_run_id,
        paid_at=invoice.paid_at,
        payment_reference=invoice.payment_reference,
    )


def check_services(runtime: WorkflowRuntime, actor: Actor) -> CheckServices:
    """What the checks may use in a request: the engine services the runs use."""
    services = runtime.services
    return CheckServices(
        org_id=actor.org_id,
        store=services.store,
        router=services.router,
        embedder=services.embedder,
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
    request: Request, invoice_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> PayInvoiceDetail:
    authorize(actor, INVOICE_READ)
    invoice = await get_invoice(session, invoice_id)
    return await _detail(request, session, actor, invoice)


# ------------------------------------------------------------------ a person's actions


@router.post("/invoices/{invoice_id}/corrections", response_model=PayInvoiceDetail)
async def correct_invoice_field(
    request: Request,
    invoice_id: uuid.UUID,
    body: PayInvoiceCorrectionIn,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
    runtime: Runtime,
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
    # Security: the checks and the risk run again on the corrected invoice, now, in this
    # transaction. A bank-detail correction that differs from the vendor master opens or
    # joins the change ticket and holds the vendor, exactly like the automatic check.
    await recheck_invoice(
        session,
        invoice,
        check_services(runtime, actor),
        trigger=RecheckTrigger.CORRECTION,
        by=By.of(actor),
    )
    await refresh_decisions(session, invoice)
    return await _detail(request, session, actor, invoice)


@router.post("/invoices/{invoice_id}/vendor", response_model=PayInvoiceDetail)
async def link_invoice_vendor(
    request: Request,
    invoice_id: uuid.UUID,
    body: PayInvoiceVendorIn,
    actor: CurrentActor,
    session: OrgSession,
    runtime: Runtime,
) -> PayInvoiceDetail:
    authorize(actor, INVOICE_PROCESS)
    _person(actor)
    invoice = await link_vendor(session, actor, invoice_id, body.vendor_id)
    # Another vendor changes what was checked (bank details, duplicates, domains).
    await recheck_invoice(
        session,
        invoice,
        check_services(runtime, actor),
        trigger=RecheckTrigger.VENDOR_LINKED,
        by=By.of(actor),
    )
    await refresh_decisions(session, invoice)
    return await _detail(request, session, actor, invoice)


@router.post("/invoices/{invoice_id}/coding", response_model=PayInvoiceDetail)
async def code_invoice(
    request: Request,
    invoice_id: uuid.UUID,
    body: PayInvoiceCodingIn,
    actor: CurrentActor,
    session: OrgSession,
    runtime: Runtime,
) -> PayInvoiceDetail:
    """Set the department and category the approval rules route on. The checks run
    again (the spend policy reads the coding)."""
    authorize(actor, INVOICE_PROCESS)
    _person(actor)
    invoice = await set_coding(session, actor, invoice_id, body.department, body.category)
    await recheck_invoice(
        session,
        invoice,
        check_services(runtime, actor),
        trigger=RecheckTrigger.CODING,
        by=By.of(actor),
    )
    await refresh_decisions(session, invoice)
    return await _detail(request, session, actor, invoice)


@router.post("/invoices/{invoice_id}/checks", response_model=PayInvoiceDetail)
async def rerun_checks(
    request: Request,
    invoice_id: uuid.UUID,
    actor: CurrentActor,
    session: OrgSession,
    runtime: Runtime,
) -> PayInvoiceDetail:
    """Run every check again (stale results, or a check that could not run)."""
    authorize(actor, INVOICE_PROCESS)
    _person(actor)
    invoice = await get_invoice(session, invoice_id, lock=True)
    if invoice.status not in {InvoiceStatus.NEEDS_REVIEW.value, InvoiceStatus.READY.value}:
        raise InvoiceStateError(f"the invoice is {invoice.status}")
    if invoice.extraction_id is None:
        raise InvoiceStateError("nothing was read from this invoice yet")
    await recheck_invoice(
        session,
        invoice,
        check_services(runtime, actor),
        trigger=RecheckTrigger.PERSON,
        by=By.of(actor),
    )
    await refresh_decisions(session, invoice)
    return await _detail(request, session, actor, invoice)


@router.post("/invoices/{invoice_id}/review", response_model=PayInvoiceDetail)
async def review_invoice(
    request: Request,
    invoice_id: uuid.UUID,
    body: PayInvoiceReviewIn,
    actor: CurrentActor,
    session: OrgSession,
) -> PayInvoiceDetail:
    authorize(actor, INVOICE_PROCESS)
    _person(actor)
    invoice = await mark_reviewed(session, actor, invoice_id, body.note)
    return await _detail(request, session, actor, invoice)


@router.post("/invoices/{invoice_id}/reject", response_model=PayInvoiceDetail)
async def reject(
    request: Request,
    invoice_id: uuid.UUID,
    body: PayInvoiceRejectIn,
    actor: CurrentActor,
    session: OrgSession,
) -> PayInvoiceDetail:
    authorize(actor, INVOICE_PROCESS)
    _person(actor)
    invoice = await reject_invoice(session, actor, invoice_id, body.reason)
    return await _detail(request, session, actor, invoice)


# ------------------------------------------------------------------ approval


@router.post("/invoices/{invoice_id}/submit", response_model=PayInvoiceDetail)
async def submit_for_approval_route(
    request: Request,
    invoice_id: uuid.UUID,
    body: PayInvoiceSubmitIn,
    actor: CurrentActor,
    session: OrgSession,
    runtime: Runtime,
) -> Any:
    """Send a ``ready`` invoice for approval (``pay.invoice.submit``, people only).
    Refused (409, with the reasons) while the vendor is held, a bank-detail change is
    open, or the checks are stale or failed. The approval run starts after the commit,
    unless an approval rule the organisation turned on approved it."""
    authorize(actor, INVOICE_SUBMIT)
    _person(actor)
    try:
        submission = await submit_for_approval(session, actor, invoice_id, body.note)
    except NotApprovableError as exc:
        return problem_response(
            request,
            409,
            "not_approvable",
            detail=str(exc),
            reasons=exc.reasons,
        )
    await session.commit()
    if not submission.by_rule:
        try:
            await runtime.start_run(
                actor,
                workflow=WORKFLOW,
                graph=APPROVAL_GRAPH,
                input={"invoice_id": str(invoice_id)},
                entity_type=INVOICE_ENTITY,
                entity_id=str(invoice_id),
            )
        except Exception as exc:
            log.warning("pay_invoice_approval_not_started", error=type(exc).__name__)
        invoice = await get_invoice(session, invoice_id, lock=True)
        if invoice.status == InvoiceStatus.AWAITING_APPROVAL.value and invoice.approval_id is None:
            # The run did not get as far as the approval: a person looks (fail closed).
            await back_to_person(session, invoice_id, "approval_not_started", run_id=None)
        await session.commit()
    invoice = await get_invoice(session, invoice_id)
    return await _detail(request, session, actor, invoice)
