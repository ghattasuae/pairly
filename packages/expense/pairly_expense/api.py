# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Expense routes, mounted by the API under ``/v1/expense`` (``plugin.api_router``).

* ``POST /v1/expense/notes``       multipart/form-data: one ``file`` part (the receipt)
  and an optional ``note`` text field. Needs ``expense.note.submit`` and
  ``expense.document.upload``; people only. The receipt goes through the same intake as
  ``/v1/documents`` (streamed, hashed, sniffed, size and type checked, deduplicated),
  the note is stored, and the ``expense_note`` run starts after the response. 202 with
  the note. Safe to retry with the same ``Idempotency-Key``.
* ``GET  /v1/expense/notes``       newest first. Reviewers (``expense.note.approve``,
  ``expense.extraction.correct``, or the core-wide ``core.decision.read``) see every
  note; everyone else with ``expense.note.read`` sees their own.
* ``GET  /v1/expense/notes/{id}``  one note with what was read from the receipt (values,
  confidence, flags and evidence spans), the guardrail findings, the rule results, the
  risk assessment, the approval and the decision record ids. Someone else's note is a
  404 for a person who may only see their own. Guardrail evidence text and risk signals
  are for reviewers.

Every write is audited in the request's transaction (``expense.note.submitted``).
Response models are named ``Expense...``: OpenAPI schema names are global, and a
plugin's model must never rename a core one.
"""

from __future__ import annotations

import re
import tempfile
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from pairly_api.idempotency import idempotent
from pairly_api.problems import problem_response
from pairly_api.routes.approvals import Runtime
from pairly_api.uploads import (
    MULTIPART_OVERHEAD,
    UnsupportedBodyError,
    enqueue_scan,
    get_services,
    receive_upload_with_fields,
    rejected_response,
)
from pairly_core.approvals.models import Approval, ApprovalDecision, ApprovalStep
from pairly_core.audit import audit_actor_action
from pairly_core.documents.permissions import authorize_document
from pairly_core.errors import BadRequestError, ForbiddenError, NotFoundError
from pairly_core.extraction.service import load_extraction
from pairly_core.guardrails.report import GuardrailReport
from pairly_core.identity import Context, CurrentActor, OrgSession
from pairly_core.ingestion.models import Document, DocumentSource
from pairly_core.ingestion.service import (
    AuditBy,
    RejectReason,
    TooLargeError,
    ingest_spooled,
    record_rejection,
)
from pairly_core.policy.service import results_for_evaluation
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_core.risk.models import RiskAssessment, RiskSignalRow
from pairly_core.workflow.models import WorkflowRun
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_expense.graph import GRAPH
from pairly_expense.models import MAX_NOTE, ExpenseNote, NoteStatus
from pairly_expense.notes import ENTITY_TYPE, WORKFLOW
from pairly_expense.permissions import NOTE_READ, NOTE_SUBMIT, SEE_ALL

log = structlog.get_logger("pairly_expense.api")

router = APIRouter(prefix="/notes", tags=["expense"])

NOTE_FIELD: Final = "note"
SUBMITTED: Final = "expense.note.submitted"
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ------------------------------------------------------------------ schemas


class ExpenseNoteSummary(BaseModel):
    id: uuid.UUID
    status: str
    submitted_by: uuid.UUID
    document_id: uuid.UUID
    run_id: uuid.UUID | None
    approval_id: uuid.UUID | None
    merchant: str | None
    expense_date: date | None
    amount: Decimal | None
    currency: str | None
    category: str | None
    reimbursable: bool
    review_reasons: list[str]
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None

    @classmethod
    def of(cls, note: ExpenseNote) -> ExpenseNoteSummary:
        return cls(
            id=note.id,
            status=note.status,
            submitted_by=note.submitted_by,
            document_id=note.document_id,
            run_id=note.run_id,
            approval_id=note.approval_id,
            merchant=note.merchant,
            expense_date=note.expense_date,
            amount=note.amount,
            currency=note.currency,
            category=note.category,
            reimbursable=note.reimbursable,
            review_reasons=list(note.review_reasons),
            created_at=note.created_at,
            updated_at=note.updated_at,
            decided_at=note.decided_at,
        )


class ExpenseNoteList(BaseModel):
    items: list[ExpenseNoteSummary]


class ExpenseSpanOut(BaseModel):
    page: int | None
    start: int
    end: int
    quote: str


class ExpenseFieldOut(BaseModel):
    path: str
    kind: str
    value: Any
    extracted_value: Any
    corrected: bool
    confidence: float
    threshold: float
    required: bool
    flags: list[str]
    evidence: list[ExpenseSpanOut]


class ExpenseExtractionOut(BaseModel):
    id: uuid.UUID
    status: str
    reasons: list[str]
    schema_name: str
    schema_version: str
    prompt_version: str
    model_provider: str | None
    model_name: str | None
    from_recording: bool
    fields: list[ExpenseFieldOut]


class ExpenseFindingOut(BaseModel):
    id: str
    type: str
    code: str
    severity: str
    location: str
    page: int | None
    removed_from_model_input: bool
    explanation: str
    # The text that triggered the finding: reviewers only.
    evidence: str | None


class ExpenseGuardrailsOut(BaseModel):
    flagged: bool
    errors: list[str]
    findings: list[ExpenseFindingOut]
    removed_spans: int


class ExpenseRuleOut(BaseModel):
    rule_id: str
    outcome: str
    severity: str | None
    reason: str
    reason_ar: str | None
    policy_version: int | None


class ExpenseSignalOut(BaseModel):
    detector: str
    status: str
    score: float
    weight: float
    label: str
    severity: str
    explanation_key: str


class ExpenseRiskOut(BaseModel):
    id: uuid.UUID
    score: float
    level: str
    requires_human: bool
    reasons: list[str]
    policy_version: int | None
    # Reviewers only.
    signals: list[ExpenseSignalOut] | None


class ExpenseStepOut(BaseModel):
    position: int
    name: str
    mode: str
    status: str


class ExpenseApprovalDecisionOut(BaseModel):
    step: str
    decision: str
    decided_by: uuid.UUID
    channel: str
    decided_at: datetime


class ExpenseApprovalOut(BaseModel):
    id: uuid.UUID
    status: str
    chain: str
    title: str
    steps: list[ExpenseStepOut]
    decisions: list[ExpenseApprovalDecisionOut]


class ExpenseDocumentRef(BaseModel):
    id: uuid.UUID
    status: str
    media_type: str
    original_filename: str | None
    sha256: str


class ExpenseNoteDetail(ExpenseNoteSummary):
    note: str
    amount_aed: Decimal | None
    failure_reason: str | None
    run_status: str | None
    document: ExpenseDocumentRef
    extraction: ExpenseExtractionOut | None
    guardrails: ExpenseGuardrailsOut | None
    rules: list[ExpenseRuleOut]
    risk: ExpenseRiskOut | None
    approval: ExpenseApprovalOut | None
    decision_ids: list[str]


# ------------------------------------------------------------------ helpers


def _reviewer(actor: Actor) -> bool:
    return any(actor.has(p) for p in SEE_ALL)


def _clean_note(raw: str | None) -> str:
    text = _CONTROL_RE.sub("", raw or "").strip()
    if len(text) > MAX_NOTE:
        raise BadRequestError(f"the note is longer than {MAX_NOTE} characters")
    return text


async def _start(
    runtime: WorkflowRuntime, actor: Actor, note_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    """Start the run after the response. A failure leaves the note ``processing`` with
    no run (visible in the list), never in an approved state."""
    try:
        await runtime.start_run(
            actor,
            workflow=WORKFLOW,
            graph=GRAPH,
            input={"note_id": str(note_id), "document_id": str(document_id)},
            entity_type=ENTITY_TYPE,
            entity_id=str(note_id),
        )
    except Exception as exc:
        log.error("expense_run_not_started", note_id=str(note_id), error=type(exc).__name__)


async def _visible_note(session: OrgSession, actor: Actor, note_id: uuid.UUID) -> ExpenseNote:
    authorize(actor, NOTE_READ)
    note = await session.scalar(select(ExpenseNote).where(ExpenseNote.id == note_id))
    if note is None or (not _reviewer(actor) and note.submitted_by != actor.id):
        raise NotFoundError("expense note not found")
    return note


# ------------------------------------------------------------------ routes


@router.post(
    "",
    status_code=202,
    response_model=ExpenseNoteSummary,
    dependencies=[Depends(idempotent, scope="function")],
    responses={
        400: {"description": "Malformed body, empty file or a note that is too long"},
        413: {"description": "Receipt larger than the workflow allows"},
        415: {"description": "Receipt type not allowed, or the body is not multipart"},
    },
)
async def submit_note(
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
    ctx: Context,
    runtime: Runtime,
    background: BackgroundTasks,
) -> Any:
    authorize(actor, NOTE_SUBMIT)
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can submit an expense note")
    authorize_document(actor, ctx.registry, workflow=WORKFLOW, action="upload")
    services = get_services(request)
    policy = services.settings.policy_for(WORKFLOW)
    by = AuditBy.from_actor(actor)

    declared = request.headers.get("content-length")
    if (
        declared is not None
        and declared.isdigit()
        and int(declared) > policy.max_bytes + MULTIPART_OVERHEAD
    ):
        await record_rejection(
            session,
            by=by,
            workflow=WORKFLOW,
            source=DocumentSource.UPLOAD,
            reason=RejectReason.TOO_LARGE,
        )
        return rejected_response(request, RejectReason.TOO_LARGE, WORKFLOW)

    # Release the database connection while the body streams in.
    await session.commit()
    with tempfile.TemporaryDirectory(prefix="pairly-expense-", dir=services.tmp_dir) as tmp:
        try:
            received = await receive_upload_with_fields(
                request,
                Path(tmp),
                policy.max_bytes,
                services.settings.upload_timeout_seconds,
                text_fields=frozenset({NOTE_FIELD}),
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
                by=by,
                workflow=WORKFLOW,
                source=DocumentSource.UPLOAD,
                reason=RejectReason.TOO_LARGE,
            )
            return rejected_response(request, RejectReason.TOO_LARGE, WORKFLOW)
        text = _clean_note(received.fields.get(NOTE_FIELD))
        outcome = await ingest_spooled(
            session,
            services.store,
            spooled=received.file,
            workflow=WORKFLOW,
            source=DocumentSource.UPLOAD,
            filename=received.filename,
            policy=policy,
            by=by,
            submitted_by=actor.id,
        )
    if outcome.reason is not None or outcome.document is None:
        return rejected_response(request, outcome.reason or RejectReason.EMPTY, WORKFLOW)
    document = outcome.document
    note = ExpenseNote(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        submitted_by=actor.id,
        note=text,
        status=NoteStatus.PROCESSING.value,
        document_id=document.id,
        review_reasons=[],
        removed_spans=[],
        decision_ids=[],
    )
    session.add(note)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        SUBMITTED,
        entity_type=ENTITY_TYPE,
        entity_id=note.id,
        workflow=WORKFLOW,
        payload={
            "document_id": str(document.id),
            "receipt_seen_before": not outcome.created,
            "has_note": bool(text),
        },
    )
    await session.refresh(note)
    body = ExpenseNoteSummary.of(note)
    # The run must see the note: commit before it starts (after the response).
    await session.commit()
    if outcome.created and runtime.services.scanner is None:
        # No scanner here: the worker scans and parses; the run waits for it.
        background.add_task(enqueue_scan, services.queue, actor.org_id, document.id)
    background.add_task(_start, runtime, actor, note.id, document.id)
    return JSONResponse(body.model_dump(mode="json"), status_code=202)


@router.get("", response_model=ExpenseNoteList)
async def list_notes(
    actor: CurrentActor,
    session: OrgSession,
    status: Annotated[NoteStatus | None, Query()] = None,
    mine: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ExpenseNoteList:
    authorize(actor, NOTE_READ)
    query = select(ExpenseNote).order_by(ExpenseNote.created_at.desc(), ExpenseNote.id)
    if mine or not _reviewer(actor):
        query = query.where(ExpenseNote.submitted_by == actor.id)
    if status is not None:
        query = query.where(ExpenseNote.status == status.value)
    rows = await session.scalars(query.limit(limit))
    return ExpenseNoteList(items=[ExpenseNoteSummary.of(n) for n in rows])


async def _extraction(session: OrgSession, note: ExpenseNote) -> ExpenseExtractionOut | None:
    if note.extraction_id is None:
        return None
    stored = await load_extraction(session, note.extraction_id)
    if stored is None:
        return None
    current = stored.current_values()
    corrected = {c.field_path for c in stored.corrections}
    row = stored.row
    return ExpenseExtractionOut(
        id=row.id,
        status=row.status,
        reasons=[str(r) for r in row.reasons],
        schema_name=row.schema_name,
        schema_version=row.schema_version,
        prompt_version=row.prompt_version,
        model_provider=row.model_provider,
        model_name=row.model_name,
        from_recording=row.from_recording,
        fields=[
            ExpenseFieldOut(
                path=f.path,
                kind=f.kind,
                value=current.get(f.path),
                extracted_value=f.value,
                corrected=f.path in corrected,
                confidence=f.confidence,
                threshold=f.threshold,
                required=f.required,
                flags=list(f.flags),
                evidence=[
                    ExpenseSpanOut(
                        page=s.get("page"),
                        start=int(s.get("start", 0)),
                        end=int(s.get("end", 0)),
                        quote=str(s.get("quote", "")),
                    )
                    for s in f.spans
                ],
            )
            for f in stored.fields
        ],
    )


def _guardrails(note: ExpenseNote, reviewer: bool) -> ExpenseGuardrailsOut | None:
    if note.guardrail_report is None:
        return None
    report = GuardrailReport.model_validate(note.guardrail_report)
    return ExpenseGuardrailsOut(
        flagged=report.flagged,
        errors=list(report.errors),
        removed_spans=len(note.removed_spans),
        findings=[
            ExpenseFindingOut(
                id=f.id,
                type=f.type.value,
                code=f.code,
                severity=f.severity.value,
                location=f.location,
                page=f.page,
                removed_from_model_input=f.remove,
                explanation=f.explain("en"),
                evidence=f.evidence if reviewer else None,
            )
            for f in report.findings
        ],
    )


async def _risk(session: OrgSession, note: ExpenseNote, reviewer: bool) -> ExpenseRiskOut | None:
    if note.risk_assessment_id is None:
        return None
    row = await session.scalar(
        select(RiskAssessment).where(RiskAssessment.id == note.risk_assessment_id)
    )
    if row is None:
        return None
    signals: list[ExpenseSignalOut] | None = None
    if reviewer:
        rows = await session.scalars(
            select(RiskSignalRow)
            .where(RiskSignalRow.assessment_id == row.id)
            .order_by(RiskSignalRow.position)
        )
        signals = [
            ExpenseSignalOut(
                detector=s.detector,
                status=s.status,
                score=s.score,
                weight=s.weight,
                label=s.label,
                severity=s.severity,
                explanation_key=s.explanation_key,
            )
            for s in rows
        ]
    return ExpenseRiskOut(
        id=row.id,
        score=row.score,
        level=row.level,
        requires_human=row.requires_human,
        reasons=[str(r) for r in row.reasons],
        policy_version=row.policy_version,
        signals=signals,
    )


async def _approval(session: OrgSession, note: ExpenseNote) -> ExpenseApprovalOut | None:
    if note.approval_id is None:
        return None
    approval = await session.scalar(select(Approval).where(Approval.id == note.approval_id))
    if approval is None:
        return None
    steps = list(
        await session.scalars(
            select(ApprovalStep)
            .where(ApprovalStep.approval_id == approval.id)
            .order_by(ApprovalStep.position)
        )
    )
    names = {s.id: s.name for s in steps}
    decisions = await session.scalars(
        select(ApprovalDecision)
        .where(ApprovalDecision.approval_id == approval.id)
        .order_by(ApprovalDecision.created_at, ApprovalDecision.id)
    )
    return ExpenseApprovalOut(
        id=approval.id,
        status=approval.status,
        chain=approval.chain_name,
        title=approval.title,
        steps=[
            ExpenseStepOut(position=s.position, name=s.name, mode=s.mode, status=s.status)
            for s in steps
        ],
        decisions=[
            ExpenseApprovalDecisionOut(
                step=names.get(d.step_id, ""),
                decision=d.decision,
                decided_by=d.actor_user_id,
                channel=d.channel,
                decided_at=d.created_at,
            )
            for d in decisions
        ],
    )


@router.get("/{note_id}", response_model=ExpenseNoteDetail)
async def get_note(
    note_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> ExpenseNoteDetail:
    note = await _visible_note(session, actor, note_id)
    reviewer = _reviewer(actor)
    document = await session.scalar(select(Document).where(Document.id == note.document_id))
    if document is None:  # pragma: no cover - same organisation, RESTRICT foreign key
        raise NotFoundError("expense note not found")
    run_status = (
        await session.scalar(select(WorkflowRun.status).where(WorkflowRun.id == note.run_id))
        if note.run_id is not None
        else None
    )
    rule_rows = (
        await results_for_evaluation(session, note.rules_evaluation_id)
        if note.rules_evaluation_id is not None
        else []
    )
    summary = ExpenseNoteSummary.of(note)
    return ExpenseNoteDetail(
        **summary.model_dump(),
        note=note.note,
        amount_aed=note.amount_aed,
        failure_reason=note.failure_reason,
        run_status=run_status,
        document=ExpenseDocumentRef(
            id=document.id,
            status=document.status,
            media_type=document.media_type,
            original_filename=document.original_filename,
            sha256=document.sha256,
        ),
        extraction=await _extraction(session, note),
        guardrails=_guardrails(note, reviewer),
        rules=[
            ExpenseRuleOut(
                rule_id=r.rule_id,
                outcome=r.outcome,
                severity=r.severity,
                reason=r.reason,
                reason_ar=r.reason_ar,
                policy_version=r.policy_version,
            )
            for r in rule_rows
        ],
        risk=await _risk(session, note, reviewer),
        approval=await _approval(session, note),
        decision_ids=list(note.decision_ids),
    )
