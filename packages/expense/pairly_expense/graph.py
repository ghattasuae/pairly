# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The ``expense_note`` graph (version 1): from a receipt to a person's decision.

::

    ingest -> wait_parse --parsed--> guardrails -> sanitize -> extract -> rules
                         --not parsed (fail closed)----------------------> rules
    rules -> risk -> propose -> wait --approved--> execute -> notify -> END
                                     --rejected--> rejected -> notify -> END
                                     --changes_requested--> changes -> notify -> END
                                     --(no decision)--> wait

* ``ingest``      binds the note to this run.
* ``wait_parse``  drives the receipt through the virus scan and the sandboxed parse when
                  this process has those services, else waits for the worker (bounded).
* ``guardrails``  ``scan_document`` on the parsed receipt; the report is kept on the note.
* ``sanitize``    builds the only text a model may see (hidden text and instructions
                  removed); its digest binds the next step to it.
* ``extract``     ``extract`` with the ``ExpenseReceipt`` schema through the model router
                  (small tier first, recorded responses in ``recorded`` mode).
* ``rules``       the ``expense.note`` rule set over what was read (versioned YAML).
* ``risk``        the risk assessment under the ``expense.note`` risk policy.
* ``propose``     the approval request (``expense.note`` rules choose the chain); the
                  submitter is a preparer, so they can never approve their own note.
* ``wait``        interrupts until a person decides (read from the database).
* ``execute``     the ``mark_reimbursable`` final action, through the executor.
* ``rejected``, ``changes``, ``notify``: the other outcomes and the submitter's message.

Every note goes to a person: there is no path from ``risk`` to ``execute`` that skips
``propose`` and ``wait``. Fail closed at every step: a step that cannot run (model down,
no recording, guardrail error, parse failure, missing service) records the reason on
the note and the flow continues to the human decision with that reason, never to an
automatic outcome. Graph state carries ids and the approved payload only; each step
reads what it needs from the database.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any, Final, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from pydantic import ValidationError
from sqlalchemy import select

from pairly_core.approvals.executor import ExecutorRefusedError
from pairly_core.approvals.models import ApprovalDecision
from pairly_core.audit import ActorType, append_audit
from pairly_core.documents.model import NormalizedDocument
from pairly_core.documents.pipeline import parse_document
from pairly_core.extraction.engine import extract
from pairly_core.extraction.guarded import GuardedInput
from pairly_core.extraction.service import load_extraction, save_extraction
from pairly_core.guardrails import ScanContext, scan_document
from pairly_core.guardrails.report import GuardrailReport, text_digest
from pairly_core.guardrails.sanitize import ReportMismatchError
from pairly_core.ingestion.models import Document, DocumentStatus
from pairly_core.ingestion.scan import scan_document as virus_scan
from pairly_core.ingestion.storage import StorageError
from pairly_core.llm.types import CallContext
from pairly_core.notifications.links import absolute_url, public_web_url
from pairly_core.notifications.service import Extras, Recipient, notify
from pairly_core.policy.service import Subject, evaluate_and_record
from pairly_core.policy_versions import PolicyError
from pairly_core.risk.detectors import (
    AmountOutlierDetector,
    DuplicateHashDetector,
    ExtractionConfidenceDetector,
    GuardrailFindingsDetector,
)
from pairly_core.risk.service import assess, find_hash_matches
from pairly_core.risk.types import EntityRef, RiskContext
from pairly_core.webhooks.events import publish_event
from pairly_core.workflow.context import WorkflowContext
from pairly_core.workflow.nodes import await_decision, request_approval, run_final_action
from pairly_core.workflow.runtime import node_retry
from pairly_expense.models import ExpenseNote, NoteStatus
from pairly_expense.notes import (
    ENTITY_TYPE,
    WORKFLOW,
    add_reasons,
    approval_context,
    locked_note,
    receipt_values,
    refresh_decision_ids,
    restore_result,
)
from pairly_expense.policies import APPROVAL_TYPE, POLICY_NAME, ensure_policies
from pairly_expense.schema import ExpenseReceipt
from pairly_expense.templates import DECIDED

log = structlog.get_logger("pairly_expense.graph")

GRAPH: Final = "expense_note"
VERSION: Final = 1
STEP_KEY: Final = "approve"
FINAL_ACTION: Final = "mark_reimbursable"
ACTOR_ID: Final = "pairly.expense.graph"
ASSESSMENT_TYPE: Final = "expense.note"
DECIDED_EVENT: Final = "expense.note.decided"
# Where a decided note points: the expense page lists the submitter's notes.
NOTES_PATH: Final = "/expense"
MAX_NORMALIZED_BYTES: Final = 64 * 1024 * 1024
# How long ``wait_parse`` waits for the worker to scan and parse the receipt when this
# process cannot do it itself. Past it, the note goes to a person (fail closed).
PARSE_WAIT_SECONDS: Final = float(os.environ.get("PAIRLY_EXPENSE_PARSE_WAIT_SECONDS", "30"))
PARSE_POLL_SECONDS: Final = 1.0
_PENDING = frozenset({DocumentStatus.RECEIVED, DocumentStatus.SCANNING, DocumentStatus.CLEAN})


class NoteState(TypedDict, total=False):
    note_id: str
    document_id: str
    approval_id: str
    payload: dict[str, Any]
    outcome: str


Ctx = Runtime[WorkflowContext]


# ------------------------------------------------------------------ helpers


def _ids(state: NoteState) -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.UUID(state["note_id"]), uuid.UUID(state["document_id"])


async def _note_reasons(ctx: WorkflowContext, note_id: uuid.UUID, *reasons: str) -> None:
    async with ctx.transaction() as session:
        add_reasons(await locked_note(session, note_id), reasons)


async def _audit(
    ctx: WorkflowContext, session: Any, action: str, note_id: uuid.UUID, payload: dict[str, Any]
) -> None:
    await append_audit(
        session,
        action=action,
        actor_type=ActorType.SYSTEM,
        actor_id=ACTOR_ID,
        entity_type=ENTITY_TYPE,
        entity_id=note_id,
        workflow=WORKFLOW,
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
            raise LookupError("receipt document not found")
        session.expunge(doc)
        return doc


async def _normalized(ctx: WorkflowContext, document_id: uuid.UUID) -> NormalizedDocument | str:
    """The parsed receipt, or the reason it cannot be read (fail closed)."""
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


async def _report(ctx: WorkflowContext, note_id: uuid.UUID) -> tuple[ExpenseNote, Any]:
    async with ctx.transaction() as session:
        note = await locked_note(session, note_id)
        session.expunge(note)
    report = (
        GuardrailReport.model_validate(note.guardrail_report) if note.guardrail_report else None
    )
    return note, report


# ------------------------------------------------------------------ nodes


async def ingest(state: NoteState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    note_id, document_id = _ids(state)
    async with ctx.transaction() as session:
        note = await locked_note(session, note_id)
        if note.document_id != document_id:
            raise LookupError("the run's receipt is not the note's receipt")
        if note.run_id is None:
            note.run_id = ctx.run_id
            note.updated_at = datetime.now(UTC)
            await _audit(ctx, session, "expense.note.processing", note_id, {})
        elif note.run_id != ctx.run_id:
            raise LookupError("the note belongs to another run")
    return {}


async def wait_parse(state: NoteState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    note_id, document_id = _ids(state)
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
    # Without the services here, the worker scans and parses: wait for it, bounded.
    deadline = asyncio.get_running_loop().time() + PARSE_WAIT_SECONDS
    while doc.status in _PENDING and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(PARSE_POLL_SECONDS)
        doc = await _document(ctx, document_id)
    if doc.status == DocumentStatus.PARSED:
        return Command(goto="guardrails")
    reason = doc.status_reason or doc.status
    await _note_reasons(ctx, note_id, f"document_not_parsed:{reason}"[:200])
    return Command(goto="rules")


async def guardrails(state: NoteState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    note_id, document_id = _ids(state)
    normalized = await _normalized(ctx, document_id)
    if isinstance(normalized, str):
        await _note_reasons(ctx, note_id, normalized)
        return Command(goto="rules")
    doc = await _document(ctx, document_id)
    report = scan_document(normalized, context=ScanContext(filename=doc.original_filename))
    async with ctx.transaction() as session:
        note = await locked_note(session, note_id)
        note.guardrail_report = report.model_dump(mode="json")
        flagged = [f.id for f in report.flagged_findings()]
        if flagged:
            add_reasons(note, ["guardrails_flagged:" + ",".join(flagged[:20])])
        if report.errors:
            add_reasons(note, ["guardrails_error"])
        note.updated_at = datetime.now(UTC)
        await _audit(
            ctx,
            session,
            "expense.note.guardrails_scanned",
            note_id,
            {
                "findings": len(report.findings),
                "flagged": flagged[:20],
                "errors": len(report.errors),
                "types": sorted({f.type.value for f in report.findings}),
            },
        )
    return Command(goto="sanitize")


async def sanitize(state: NoteState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    note_id, document_id = _ids(state)
    normalized = await _normalized(ctx, document_id)
    _, report = await _report(ctx, note_id)
    if isinstance(normalized, str) or report is None:
        await _note_reasons(ctx, note_id, "sanitize_failed")
        return Command(goto="rules")
    try:
        guarded = GuardedInput.build(normalized, report)
    except ReportMismatchError:
        await _note_reasons(ctx, note_id, "guardrail_report_mismatch")
        return Command(goto="rules")
    removed = [{"start": s.start, "end": s.end} for s in guarded.model_input.removed]
    async with ctx.transaction() as session:
        note = await locked_note(session, note_id)
        note.sanitized_sha256 = text_digest(guarded.text)
        note.removed_spans = removed
        note.updated_at = datetime.now(UTC)
        await _audit(
            ctx,
            session,
            "expense.note.sanitized",
            note_id,
            {
                "removed_spans": len(removed),
                "removed_by": list(guarded.model_input.removed_findings[:20]),
                "sanitized_sha256": note.sanitized_sha256,
            },
        )
    return Command(goto="extract")


async def extract_fields(state: NoteState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    note_id, document_id = _ids(state)
    normalized = await _normalized(ctx, document_id)
    note, report = await _report(ctx, note_id)
    if isinstance(normalized, str) or report is None:
        await _note_reasons(ctx, note_id, "extraction_skipped:no_document")
        return Command(goto="rules")
    guarded = GuardedInput.build(normalized, report)
    if text_digest(guarded.text) != note.sanitized_sha256:
        # Not the text the sanitise step recorded: never send it to a model.
        await _note_reasons(ctx, note_id, "sanitized_input_changed")
        return Command(goto="rules")
    router = ctx.services.router
    if router is None:
        await _note_reasons(ctx, note_id, "model_router_unavailable")
        return Command(goto="rules")
    result = await extract(
        normalized,
        guarded,
        ExpenseReceipt,
        router=router,
        org_id=ctx.org_id,
        context=CallContext(
            actor_id=ACTOR_ID, workflow=WORKFLOW, entity_type=ENTITY_TYPE, entity_id=str(note_id)
        ),
    )
    async with ctx.transaction() as session:
        extraction_id = await save_extraction(
            session,
            result,
            document_id=document_id,
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR_ID,
        )
        stored = await load_extraction(session, extraction_id)
        values = receipt_values(stored)
        row = await locked_note(session, note_id)
        row.extraction_id = extraction_id
        row.merchant = values.merchant
        row.expense_date = values.date
        row.amount = values.amount
        row.currency = values.currency
        row.amount_aed = values.amount_aed
        row.category = values.category
        add_reasons(row, (f"extraction:{r}" for r in result.reasons))
        row.updated_at = datetime.now(UTC)
        await refresh_decision_ids(session, row)
    return Command(goto="rules")


async def rules(state: NoteState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    note_id, _ = _ids(state)
    async with ctx.transaction() as session:
        await ensure_policies(session, actor_type=ActorType.SYSTEM, actor_id=ACTOR_ID)
        note = await locked_note(session, note_id)
        stored = await load_extraction(session, note.extraction_id) if note.extraction_id else None
        values = receipt_values(stored)
        recorded = await evaluate_and_record(
            session,
            name=POLICY_NAME,
            facts=values.facts(),
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR_ID,
            subject=Subject(type=ENTITY_TYPE, id=str(note_id), workflow=WORKFLOW),
        )
        note.rules_evaluation_id = recorded.evaluation_id
        add_reasons(
            note,
            (
                f"rule:{r.rule_id}:{r.outcome.value}"
                for r in recorded.evaluation.results
                if r.outcome.value != "pass"
            ),
        )
        note.updated_at = datetime.now(UTC)
        await refresh_decision_ids(session, note)
    return {}


async def risk(state: NoteState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    note_id, document_id = _ids(state)
    normalized = await _normalized(ctx, document_id)
    note, report = await _report(ctx, note_id)
    doc = await _document(ctx, document_id)
    async with ctx.transaction() as session:
        stored = await load_extraction(session, note.extraction_id) if note.extraction_id else None
        matches = await find_hash_matches(session, doc.sha256, exclude_document_id=doc.id)
        # The same receipt on another note (uploads of the same bytes share a document).
        others = await session.scalars(
            select(ExpenseNote.id).where(
                ExpenseNote.document_id == doc.id, ExpenseNote.id != note_id
            )
        )
        matches += [EntityRef(ENTITY_TYPE, str(other), WORKFLOW) for other in others]
        history = await session.scalars(
            select(ExpenseNote.amount_aed)
            .where(
                ExpenseNote.submitted_by == note.submitted_by,
                ExpenseNote.id != note_id,
                ExpenseNote.amount_aed.is_not(None),
                ExpenseNote.status == NoteStatus.APPROVED.value,
            )
            .order_by(ExpenseNote.created_at.desc())
            .limit(50)
        )
        context = RiskContext(
            org_id=ctx.org_id,
            workflow=WORKFLOW,
            entity_type=ENTITY_TYPE,
            entity_id=str(note_id),
            document=normalized if isinstance(normalized, NormalizedDocument) else None,
            document_sha256=doc.sha256,
            extraction=restore_result(stored) if stored is not None else None,
            guardrail_report=report if isinstance(normalized, NormalizedDocument) else None,
            hash_matches=matches,
            amount=note.amount_aed,
            amount_history=[a for a in history if a is not None],
        )
        record = await assess(
            session,
            context,
            (
                GuardrailFindingsDetector(),
                ExtractionConfidenceDetector(),
                DuplicateHashDetector(),
                AmountOutlierDetector(),
            ),
            assessment_type=ASSESSMENT_TYPE,
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR_ID,
        )
        row = await locked_note(session, note_id)
        row.risk_assessment_id = record.id
        add_reasons(row, (f"risk:{r}" for r in record.combined.reasons))
        row.updated_at = datetime.now(UTC)
        await refresh_decision_ids(session, row)
    return {}


def _payload(note: ExpenseNote, sha256: str) -> dict[str, Any]:
    """What the approvers approve. The executor re-hashes it before the final action."""
    return {
        "note_id": str(note.id),
        "merchant": note.merchant,
        "date": note.expense_date.isoformat() if note.expense_date else None,
        "amount": str(note.amount) if note.amount is not None else None,
        "currency": note.currency,
        "amount_aed": str(note.amount_aed) if note.amount_aed is not None else None,
        "category": note.category,
        "receipt_sha256": sha256,
    }


def _title(note: ExpenseNote) -> str:
    who = note.merchant or "Unread receipt"
    if note.amount is not None and note.currency:
        return f"{who}, {note.amount} {note.currency}"
    return f"{who}, amount not read"


async def propose(state: NoteState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    note_id, document_id = _ids(state)
    note, report = await _report(ctx, note_id)
    doc = await _document(ctx, document_id)
    payload = _payload(note, doc.sha256)
    async with ctx.transaction() as session:
        stored = await load_extraction(session, note.extraction_id) if note.extraction_id else None
    try:
        approval_id = await request_approval(
            ctx,
            step_key=STEP_KEY,
            approval_type=APPROVAL_TYPE,
            title=_title(note),
            payload=payload,
            context=approval_context(note, stored, report),
            entity_type=ENTITY_TYPE,
            entity_id=str(note_id),
        )
    except PolicyError as exc:
        # Nobody may approve (or no chain applies): the note cannot go on. Fail closed.
        async with ctx.transaction() as session:
            row = await locked_note(session, note_id)
            row.status = NoteStatus.FAILED.value
            row.failure_reason = f"approval_not_possible:{type(exc).__name__}"[:200]
            row.updated_at = datetime.now(UTC)
            await _audit(
                ctx, session, "expense.note.failed", note_id, {"reason": row.failure_reason}
            )
        return Command(goto=END)
    async with ctx.transaction() as session:
        row = await locked_note(session, note_id)
        row.approval_id = uuid.UUID(approval_id)
        if row.status == NoteStatus.PROCESSING.value:
            row.status = NoteStatus.AWAITING_APPROVAL.value
            await _audit(
                ctx,
                session,
                "expense.note.awaiting_approval",
                note_id,
                {"approval_id": approval_id, "review_reasons": list(row.review_reasons[:20])},
            )
        row.updated_at = datetime.now(UTC)
        await refresh_decision_ids(session, row)
    return Command(goto="wait", update={"approval_id": approval_id, "payload": payload})


async def wait(state: NoteState, runtime: Ctx) -> Command[Any]:
    outcome = await await_decision(runtime.context, state["approval_id"])
    if outcome is None:
        return Command(goto="wait")
    target = {"approved": "execute", "rejected": "rejected", "changes_requested": "changes"}
    return Command(goto=target[outcome], update={"outcome": outcome})


async def execute(state: NoteState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    note_id, _ = _ids(state)
    try:
        await run_final_action(
            ctx,
            action=FINAL_ACTION,
            approval_id=state["approval_id"],
            step_key=STEP_KEY,
            payload=state["payload"],
        )
    except ExecutorRefusedError as exc:
        async with ctx.transaction() as session:
            row = await locked_note(session, note_id)
            row.status = NoteStatus.FAILED.value
            row.failure_reason = f"executor_refused:{exc.reason}"[:200]
            row.updated_at = datetime.now(UTC)
            await _audit(
                ctx, session, "expense.note.failed", note_id, {"reason": row.failure_reason}
            )
        raise
    async with ctx.transaction() as session:
        await refresh_decision_ids(session, await locked_note(session, note_id))
    return {}


async def _close(
    ctx: WorkflowContext, note_id: uuid.UUID, status: NoteStatus, approval_id: str
) -> None:
    async with ctx.transaction() as session:
        row = await locked_note(session, note_id)
        if row.status != NoteStatus.AWAITING_APPROVAL.value:
            return
        row.status = status.value
        row.decided_at = row.updated_at = datetime.now(UTC)
        await refresh_decision_ids(session, row)
        await _audit(
            ctx, session, f"expense.note.{status.value}", note_id, {"approval_id": approval_id}
        )


async def rejected(state: NoteState, runtime: Ctx) -> dict[str, Any]:
    await _close(runtime.context, _ids(state)[0], NoteStatus.REJECTED, state["approval_id"])
    return {}


async def changes(state: NoteState, runtime: Ctx) -> dict[str, Any]:
    await _close(
        runtime.context, _ids(state)[0], NoteStatus.CHANGES_REQUESTED, state["approval_id"]
    )
    return {}


async def notify_submitter(state: NoteState, runtime: Ctx) -> dict[str, Any]:
    """Tell the submitter, and webhook subscribers (``expense.note.decided``). A failure
    here is logged, never undoes the decision."""
    ctx = runtime.context
    note_id, _ = _ids(state)
    outcome = state.get("outcome", "")
    try:
        async with ctx.transaction() as session:
            note = await locked_note(session, note_id)
            reason = await session.scalar(
                select(ApprovalDecision.reason)
                .where(ApprovalDecision.approval_id == uuid.UUID(state["approval_id"]))
                .order_by(ApprovalDecision.created_at.desc())
                .limit(1)
            )
            # The expense page lists the submitter's notes (there is no page per note).
            url = absolute_url(public_web_url(), NOTES_PATH)
            variables: dict[str, Any] = {"title": _title(note), "outcome": outcome}
            if reason:
                variables["reason"] = reason[:500]
            if url:
                variables["note_url"] = url
            await notify(
                session,
                recipient=Recipient.user(note.submitted_by),
                template_key=DECIDED,
                variables=variables,
                dedupe_key=f"expense:{note_id}:{outcome}",
                channels=["email", "in_app"],
                extras=Extras(link=NOTES_PATH),
                workflow=WORKFLOW,
            )
            await publish_event(
                session,
                DECIDED_EVENT,
                {"note_id": str(note_id), "outcome": outcome, "status": note.status},
            )
    except Exception as exc:
        log.warning("expense_notify_failed", note_id=str(note_id), error=type(exc).__name__)
    return {}


# ------------------------------------------------------------------ graph


def build_graph() -> StateGraph[Any, Any, Any, Any]:
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(NoteState, context_schema=WorkflowContext)
    retry = node_retry()
    graph.add_node("ingest", ingest, retry_policy=retry, timeout=30)
    graph.add_node(
        "wait_parse",
        wait_parse,
        retry_policy=retry,
        timeout=PARSE_WAIT_SECONDS + 300,
        destinations=("guardrails", "rules"),
    )
    graph.add_node(
        "guardrails",
        guardrails,
        retry_policy=retry,
        timeout=120,
        destinations=("sanitize", "rules"),
    )
    graph.add_node(
        "sanitize", sanitize, retry_policy=retry, timeout=60, destinations=("extract", "rules")
    )
    graph.add_node(
        "extract", extract_fields, retry_policy=retry, timeout=300, destinations=("rules",)
    )
    graph.add_node("rules", rules, retry_policy=retry, timeout=60)
    graph.add_node("risk", risk, retry_policy=retry, timeout=120)
    graph.add_node("propose", propose, retry_policy=retry, timeout=60, destinations=("wait", END))
    graph.add_node("wait", wait, destinations=("wait", "execute", "rejected", "changes"))
    graph.add_node("execute", execute, retry_policy=retry, timeout=60)
    graph.add_node("rejected", rejected, retry_policy=retry, timeout=30)
    graph.add_node("changes", changes, retry_policy=retry, timeout=30)
    graph.add_node("notify", notify_submitter, timeout=60)
    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "wait_parse")
    graph.add_edge("rules", "risk")
    graph.add_edge("risk", "propose")
    graph.add_edge("execute", "notify")
    graph.add_edge("rejected", "notify")
    graph.add_edge("changes", "notify")
    graph.add_edge("notify", END)
    return graph
