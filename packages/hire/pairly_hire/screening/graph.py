# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The ``hire_screening`` graph (version 1): one application, from its CV to a person.

::

    ingest -> wait_parse --parsed--> guardrails -> sanitize -> parse_cv -> blind -> score
                         --not parsed (fail closed)--------------------------------> checks
    score -> checks -> propose -> wait --approved--> shortlist -> END
                                       --rejected--> not_shortlisted -> END
                                       --changes_requested--> returned -> END
                                       --(no decision)--> wait

* ``ingest``      binds the screening to this run.
* ``wait_parse``  drives the CV through the virus scan and the sandboxed parse when this
                  process has those services, else waits for the worker (bounded).
* ``guardrails``  ``scan_document`` (hidden text incl. the renderer check, instructions,
                  stuffing, metadata, Unicode), then the whole-document injection
                  classifier (Hire default ``always``; a classifier that cannot run
                  flags the CV). The report is kept, encrypted.
* ``sanitize``    the only text a model may read (hidden text and instructions out);
                  its digest binds the next steps to it.
* ``parse_cv``    ``CvProfile`` through the extraction framework, every value with
                  evidence in the original CV (``pairly_hire.screening.profile``).
* ``blind``       blind redaction of the sanitised text (``pairly_hire.screening.blind``)
                  with the application's contact details and the parsed profile.
* ``score``       evidence-based scoring of the blinded text against the locked rubric
                  version pinned on the application; no evidence, no credit; the overall
                  score is computed in code. Decision record ``score``.
* ``checks``      knockouts (to a person with a suggested action, never a rejection),
                  claim consistency and AI-generated signals (information only).
                  Decision record ``shortlist_suggestion``.
* ``propose``     the ``hire.screening`` approval: a recruiter or hiring manager
                  decides. ``wait`` interrupts until they do.
* ``shortlist``   the final action through the executor (lifts blind mode);
  ``not_shortlisted`` and ``returned`` close the screening with the person's outcome.

Every application goes to a person: no path skips ``propose`` and ``wait``. Fail closed:
a step that cannot run (no parse, guardrail error, report mismatch, model down, missing
recording, missing service) records the reason on the screening and the flow goes on to
the person with it. Graph state carries ids only; each step reads what it needs.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from pydantic import ValidationError
from sqlalchemy import func, select

from pairly_core.approvals.executor import ExecutorRefusedError
from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.types import unseal
from pairly_core.decisions.service import PromptRef
from pairly_core.documents.model import NormalizedDocument
from pairly_core.documents.pipeline import parse_document
from pairly_core.extraction.guarded import GuardedInput
from pairly_core.extraction.service import save_extraction
from pairly_core.guardrails.report import GuardrailReport, text_digest
from pairly_core.guardrails.sanitize import ReportMismatchError
from pairly_core.ingestion.models import Document, DocumentStatus
from pairly_core.ingestion.scan import scan_document as virus_scan
from pairly_core.ingestion.storage import StorageError
from pairly_core.llm.types import CallContext
from pairly_core.policy_versions import PolicyError
from pairly_core.workflow.context import WorkflowContext
from pairly_core.workflow.nodes import await_decision, request_approval, run_final_action
from pairly_core.workflow.runtime import node_retry
from pairly_hire.models import HireApplication
from pairly_hire.screening import pipeline
from pairly_hire.screening.actions import SHORTLIST
from pairly_hire.screening.blind import BlindConfig, BlindedInput
from pairly_hire.screening.knockouts import check_knockouts, knockout_reasons
from pairly_hire.screening.models import HireScreening, ScreeningStatus
from pairly_hire.screening.profile import ParsedProfile
from pairly_hire.screening.service import (
    ENTITY_TYPE,
    SCREENING_ENTITY,
    WORKFLOW,
    add_reasons,
    locked_screening,
    read_details,
    record_score,
    record_suggestion,
    refresh_decision_ids,
    rubric_ref,
    screening_settings,
    touch,
    write_details,
)

log = structlog.get_logger("pairly_hire.screening.graph")

GRAPH: Final = "hire_screening"
VERSION: Final = 1
STEP_KEY: Final = "review"
APPROVAL_TYPE: Final = "hire.screening"
ACTOR_ID: Final = "pairly.hire.screening"
MAX_NORMALIZED_BYTES: Final = 64 * 1024 * 1024
PARSE_WAIT_SECONDS: Final = float(os.environ.get("PAIRLY_HIRE_PARSE_WAIT_SECONDS", "30"))
PARSE_POLL_SECONDS: Final = 1.0
_PENDING = frozenset({DocumentStatus.RECEIVED, DocumentStatus.SCANNING, DocumentStatus.CLEAN})
MAX_CONTEXT_ITEMS: Final = 40
MAX_CONTEXT_QUOTE: Final = 300
MAX_CONTEXT_BYTES: Final = 28 * 1024


class ScreeningState(TypedDict, total=False):
    screening_id: str
    application_id: str
    document_id: str
    approval_id: str
    payload: dict[str, Any]
    outcome: str


Ctx = Runtime[WorkflowContext]


# ------------------------------------------------------------------ helpers


def _ids(state: ScreeningState) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    return (
        uuid.UUID(state["screening_id"]),
        uuid.UUID(state["application_id"]),
        uuid.UUID(state["document_id"]),
    )


def _context(screening_id: uuid.UUID) -> CallContext:
    return CallContext(
        actor_id=ACTOR_ID,
        workflow=WORKFLOW,
        entity_type=SCREENING_ENTITY,
        entity_id=str(screening_id),
    )


async def _reasons(ctx: WorkflowContext, screening_id: uuid.UUID, *reasons: str) -> None:
    async with ctx.transaction() as session:
        row = await locked_screening(session, screening_id)
        add_reasons(row, reasons)
        touch(row)


async def _audit(
    session: Any, ctx: WorkflowContext, action: str, row: HireScreening, payload: dict[str, Any]
) -> None:
    await append_audit(
        session,
        action=action,
        actor_type=ActorType.SYSTEM,
        actor_id=ACTOR_ID,
        entity_type=SCREENING_ENTITY,
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={"run_id": str(ctx.run_id), "application_id": str(row.application_id), **payload},
    )


async def _document(ctx: WorkflowContext, document_id: uuid.UUID) -> Document:
    async with ctx.transaction() as session:
        doc = await session.scalar(
            select(Document)
            .where(Document.id == document_id)
            .execution_options(populate_existing=True)
        )
        if doc is None or doc.workflow != WORKFLOW:
            raise LookupError("CV document not found")
        session.expunge(doc)
        return doc


async def _normalized(ctx: WorkflowContext, document_id: uuid.UUID) -> NormalizedDocument | str:
    """The parsed CV, or the reason it cannot be read (fail closed)."""
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


async def _snapshot(
    ctx: WorkflowContext, screening_id: uuid.UUID
) -> tuple[HireScreening, dict[str, Any]]:
    async with ctx.transaction() as session:
        row = await locked_screening(session, screening_id)
        details = read_details(row)
        session.expunge(row)
    return row, details


def _report(details: dict[str, Any]) -> GuardrailReport | None:
    raw = details.get("guardrail_report")
    if not isinstance(raw, dict):
        return None
    try:
        return GuardrailReport.model_validate(raw)
    except ValidationError:
        return None


async def _guarded(
    ctx: WorkflowContext, screening_id: uuid.UUID, document_id: uuid.UUID
) -> tuple[NormalizedDocument, GuardedInput, HireScreening, dict[str, Any]] | str:
    """The parsed CV and its sanitised input, checked against the recorded digest."""
    normalized = await _normalized(ctx, document_id)
    row, details = await _snapshot(ctx, screening_id)
    report = _report(details)
    if isinstance(normalized, str):
        return normalized
    if report is None:
        return "guardrail_report_missing"
    try:
        guarded = pipeline.guard(normalized, report)
    except ReportMismatchError:
        return "guardrail_report_mismatch"
    if row.sanitized_sha256 is None or text_digest(guarded.text) != row.sanitized_sha256:
        # Not the text the sanitise step recorded: never send it to a model.
        return "sanitized_input_changed"
    return normalized, guarded, row, details


async def _application(ctx: WorkflowContext, application_id: uuid.UUID) -> HireApplication:
    async with ctx.transaction() as session:
        app = await session.scalar(
            select(HireApplication).where(HireApplication.id == application_id)
        )
        if app is None:
            raise LookupError("application not found")
        session.expunge(app)
        return app


def _contact(app: HireApplication) -> dict[str, Any]:
    value = unseal(app, "contact")
    return dict(value) if isinstance(value, dict) else {}


def _knockout_answers(app: HireApplication) -> dict[str, str]:
    stored = unseal(app, "answers") or {}
    knockouts = stored.get("knockouts") if isinstance(stored, dict) else None
    return {str(k): str(v) for k, v in knockouts.items()} if isinstance(knockouts, dict) else {}


def _blinded(
    guarded: GuardedInput, app: HireApplication, details: dict[str, Any], config: BlindConfig
) -> BlindedInput:
    raw = details.get("profile")
    profile = ParsedProfile.from_dict(raw) if isinstance(raw, dict) else None
    return pipeline.blind_input(guarded, _contact(app), profile, config)


# ------------------------------------------------------------------ nodes


async def ingest(state: ScreeningState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    screening_id, application_id, document_id = _ids(state)
    async with ctx.transaction() as session:
        row = await locked_screening(session, screening_id)
        if row.application_id != application_id or row.document_id != document_id:
            raise LookupError("the run's application is not the screening's application")
        if row.run_id is None:
            row.run_id = ctx.run_id
            touch(row)
            await _audit(session, ctx, "hire.screening.processing", row, {})
        elif row.run_id != ctx.run_id:
            raise LookupError("the screening belongs to another run")
    return {}


async def wait_parse(state: ScreeningState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    screening_id, _, document_id = _ids(state)
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
    await _reasons(ctx, screening_id, f"document_not_parsed:{reason}"[:200])
    return Command(goto="checks")


async def guardrails(state: ScreeningState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    screening_id, _, document_id = _ids(state)
    normalized = await _normalized(ctx, document_id)
    if isinstance(normalized, str):
        await _reasons(ctx, screening_id, normalized)
        return Command(goto="checks")
    doc = await _document(ctx, document_id)
    report = pipeline.scan(normalized, doc.original_filename)
    async with ctx.transaction() as session:
        config = await screening_settings(session)
    classifier: dict[str, Any] = {"mode": config.classifier_mode, "ran": False}
    outcome = await pipeline.classify(
        report,
        normalized,
        ctx.services.router,
        pipeline.classifier_config(config.classifier_mode),
        org_id=ctx.org_id,
    )
    if outcome is not None:
        report = outcome.report
        classifier.update(
            ran=True,
            calls=outcome.calls,
            cost_micros=outcome.cost_micros,
            spans_returned=outcome.spans_returned,
            spans_invalid=outcome.spans_invalid,
            added=outcome.added,
            error=outcome.error,
        )
    async with ctx.transaction() as session:
        row = await locked_screening(session, screening_id)
        write_details(row, guardrail_report=report.model_dump(mode="json"))
        row.model_info = {**row.model_info, "classifier": classifier}
        flagged = [f.id for f in report.flagged_findings()]
        if flagged:
            add_reasons(row, ["guardrails_flagged:" + ",".join(flagged[:20])])
        if report.errors:
            add_reasons(row, ["guardrails_error"])
        touch(row)
        await _audit(
            session,
            ctx,
            "hire.screening.guardrails_scanned",
            row,
            {
                "findings": len(report.findings),
                "flagged": flagged[:20],
                "errors": len(report.errors),
                "types": sorted({f.type.value for f in report.findings}),
                "classifier": {k: classifier[k] for k in ("mode", "ran")},
            },
        )
    return Command(goto="sanitize")


async def sanitize(state: ScreeningState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    screening_id, _, document_id = _ids(state)
    normalized = await _normalized(ctx, document_id)
    _, details = await _snapshot(ctx, screening_id)
    report = _report(details)
    if isinstance(normalized, str) or report is None:
        await _reasons(ctx, screening_id, "sanitize_failed")
        return Command(goto="checks")
    try:
        guarded = pipeline.guard(normalized, report)
    except ReportMismatchError:
        await _reasons(ctx, screening_id, "guardrail_report_mismatch")
        return Command(goto="checks")
    removed = [{"start": s.start, "end": s.end} for s in guarded.model_input.removed]
    async with ctx.transaction() as session:
        row = await locked_screening(session, screening_id)
        row.sanitized_sha256 = text_digest(guarded.text)
        row.removed_spans = removed
        touch(row)
        await _audit(
            session,
            ctx,
            "hire.screening.sanitized",
            row,
            {
                "removed_spans": len(removed),
                "removed_by": list(guarded.model_input.removed_findings[:20]),
                "sanitized_sha256": row.sanitized_sha256,
            },
        )
    return Command(goto="parse_cv")


async def parse_cv(state: ScreeningState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    screening_id, _, document_id = _ids(state)
    prepared = await _guarded(ctx, screening_id, document_id)
    if isinstance(prepared, str):
        await _reasons(ctx, screening_id, f"cv_parse_skipped:{prepared}")
        return Command(goto="checks")
    normalized, guarded, _, _ = prepared
    router = ctx.services.router
    if router is None:
        await _reasons(ctx, screening_id, "cv_parse_skipped:model_router_unavailable")
        return Command(goto="blind")
    result = await pipeline.parse_cv(
        normalized, guarded, router, org_id=ctx.org_id, context=_context(screening_id)
    )
    profile = pipeline.profile_of(result)
    call = result.model_call
    async with ctx.transaction() as session:
        extraction_id = await save_extraction(
            session, result, document_id=document_id, actor_type=ActorType.SYSTEM, actor_id=ACTOR_ID
        )
        row = await locked_screening(session, screening_id)
        row.extraction_id = extraction_id
        write_details(row, profile=profile.as_dict() if profile else None)
        row.model_info = {
            **row.model_info,
            "cv_parse": {
                "task": call.prompt_task,
                "prompt_version": call.prompt_version,
                "prompt_hash": call.prompt_hash,
                "provider": call.provider,
                "model": call.model,
                "from_recording": call.from_recording,
                "call_ids": [str(c) for c in call.call_ids],
                "status": result.status.value,
            },
        }
        if result.data is None:
            add_reasons(row, (f"cv_parse:{r}" for r in result.reasons))
        touch(row)
    return Command(goto="blind")


async def blind_step(state: ScreeningState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    screening_id, application_id, document_id = _ids(state)
    prepared = await _guarded(ctx, screening_id, document_id)
    if isinstance(prepared, str):
        await _reasons(ctx, screening_id, f"blind_skipped:{prepared}")
        return Command(goto="checks")
    _, guarded, _, details = prepared
    app = await _application(ctx, application_id)
    async with ctx.transaction() as session:
        config = await screening_settings(session)
    blinded = _blinded(guarded, app, details, config.blind)
    async with ctx.transaction() as session:
        row = await locked_screening(session, screening_id)
        row.blind = config.blind.enabled
        row.blind_config = {**config.blind.as_dict(), "categories": blinded.categories()}
        row.blinded_sha256 = blinded.digest
        row.blind_spans = [
            {"start": s, "end": e, "category": c.value} for s, e, c in blinded.original_spans()
        ]
        if details.get("profile") is None:
            # Only the application form and the patterns could be used to hide names.
            add_reasons(row, ["blind_without_profile"])
        touch(row)
        await _audit(
            session,
            ctx,
            "hire.screening.blinded",
            row,
            {"enabled": config.blind.enabled, "categories": blinded.categories()},
        )
    return Command(goto="score")


def _findings(report: GuardrailReport | None) -> list[dict[str, Any]]:
    """Guardrail findings without their evidence text (that can be personal data)."""
    return [
        f.model_dump(mode="json", exclude={"evidence"})
        for f in (report.findings if report is not None else ())
    ]


async def score(state: ScreeningState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    screening_id, application_id, document_id = _ids(state)
    prepared = await _guarded(ctx, screening_id, document_id)
    if isinstance(prepared, str):
        await _reasons(ctx, screening_id, f"score_skipped:{prepared}")
        return Command(goto="checks")
    normalized, guarded, row, details = prepared
    app = await _application(ctx, application_id)
    async with ctx.transaction() as session:
        config = await screening_settings(session)
        try:
            ref, rubric = await rubric_ref(session, app.rubric_policy_id)
        except PolicyError:
            ref = None
    if ref is None:
        await _reasons(ctx, screening_id, "score_skipped:rubric_not_locked")
        return Command(goto="checks")
    blind_config = BlindConfig(
        enabled=bool(row.blind_config.get("enabled", config.blind.enabled)),
        universities=bool(row.blind_config.get("universities", config.blind.universities)),
    )
    blinded = _blinded(guarded, app, details, blind_config)
    if row.blinded_sha256 is None or blinded.digest != row.blinded_sha256:
        await _reasons(ctx, screening_id, "score_skipped:blinded_input_changed")
        return Command(goto="checks")
    router = ctx.services.router
    if router is None:
        await _reasons(ctx, screening_id, "score_skipped:model_router_unavailable")
        return Command(goto="checks")
    scored = await pipeline.score_cv(
        rubric, normalized, blinded, router, org_id=ctx.org_id, context=_context(screening_id)
    )
    if isinstance(scored, str):
        await _reasons(ctx, screening_id, scored)
        return Command(goto="checks")
    result = scored.result
    info = scored.model_info()
    async with ctx.transaction() as session:
        row = await locked_screening(session, screening_id)
        row.overall_score = Decimal(str(result.overall))
        write_details(
            row,
            scoring={
                "criteria": [c.as_dict() for c in result.criteria],
                "overall": result.overall,
                "shares": list(result.shares),
                "rubric_version": ref.version,
                "rubric_policy_id": str(ref.id),
            },
        )
        row.model_info = {**row.model_info, "score": info}
        add_reasons(row, result.reasons)
        stored = await record_score(
            session,
            row=row,
            rubric=ref,
            criteria_public=[c.public() for c in result.criteria],
            scores=result.scores,
            overall=result.overall,
            sensitive={
                "evidence": {
                    c.criterion_id: [e.quote for e in c.evidence] for c in result.criteria
                },
                "why_not_higher": {c.criterion_id: c.why_not_higher for c in result.criteria},
                "rejected_quotes": {
                    c.criterion_id: [r.quote for r in c.rejected] for c in result.criteria
                },
            },
            prompt=PromptRef(scored.call.task, scored.call.prompt_version, scored.call.prompt_hash),
            model={k: info[k] for k in ("provider", "model", "tier", "from_recording")},
            call_ids=scored.call_ids,
            flags=[],
            findings=_findings(_report(details)),
            actor_id=ACTOR_ID,
            confidence=scored.call.confidence,
        )
        row.score_decision_id = stored.id
        touch(row)
    return Command(goto="checks")


async def _jobs_with_same_cv(ctx: WorkflowContext, document_id: uuid.UUID) -> int:
    doc = await _document(ctx, document_id)
    async with ctx.transaction() as session:
        count = await session.scalar(
            select(func.count(func.distinct(HireApplication.job_id)))
            .select_from(HireApplication)
            .join(Document, Document.id == HireApplication.cv_document_id)
            .where(Document.sha256 == doc.sha256, HireApplication.submitted_at.is_not(None))
        )
    return int(count or 0)


async def checks(state: ScreeningState, runtime: Ctx) -> Command[Any]:
    """Knockouts, claim checks and signals. Runs on every path (fail closed)."""
    ctx = runtime.context
    screening_id, application_id, document_id = _ids(state)
    app = await _application(ctx, application_id)
    answers = _knockout_answers(app)
    async with ctx.transaction() as session:
        config = await screening_settings(session)
        try:
            ref, rubric = await rubric_ref(session, app.rubric_policy_id)
        except PolicyError:
            ref = None
    knockouts = check_knockouts(rubric, answers) if ref is not None else []
    flags: list[dict[str, Any]] = []
    reasons: list[str] = knockout_reasons(knockouts)
    prepared = await _guarded(ctx, screening_id, document_id)
    if isinstance(prepared, str):
        reasons.append("claims_skipped")
    else:
        normalized, guarded, _, details = prepared
        raw = details.get("profile")
        profile = ParsedProfile.from_dict(raw) if isinstance(raw, dict) else None
        as_of = (app.submitted_at or datetime.now(UTC)).date()
        found = pipeline.claim_flags(
            profile,
            normalized,
            guarded,
            as_of=as_of if isinstance(as_of, date) else datetime.now(UTC).date(),
            jobs_with_same_cv=await _jobs_with_same_cv(ctx, document_id),
            config=config.claims,
        )
        flags = [f.as_dict() for f in found]
        if profile is None:
            reasons.append("claims_skipped:no_profile")
    async with ctx.transaction() as session:
        row = await locked_screening(session, screening_id)
        write_details(row, knockouts=[k.as_dict() for k in knockouts], flags=flags)
        row.flag_codes = sorted({f["code"] for f in flags})
        add_reasons(row, reasons)
        if ref is None:
            add_reasons(row, ["rubric_not_locked"])
        else:
            await record_suggestion(
                session,
                row=row,
                rubric=ref,
                knockouts=[k.as_dict() for k in knockouts],
                answers=answers,
                flags=flags,
                actor_id=ACTOR_ID,
            )
        await refresh_decision_ids(session, row)
        touch(row)
    return Command(goto="propose")


def _payload(row: HireScreening) -> dict[str, Any]:
    return {
        "screening_id": str(row.id),
        "application_id": str(row.application_id),
        "job_id": str(row.job_id),
        "rubric_policy_id": str(row.rubric_policy_id),
        "overall_score": str(row.overall_score) if row.overall_score is not None else None,
        "score_decision_id": row.score_decision_id,
    }


def approval_context(row: HireScreening, details: dict[str, Any]) -> dict[str, Any]:
    """What the approval inbox shows. While blind: no name, contact or CV file."""
    raw = details.get("scoring")
    scoring: dict[str, Any] = raw if isinstance(raw, dict) else {}
    criteria = [c for c in scoring.get("criteria", []) if isinstance(c, dict)]
    evidence: list[dict[str, Any]] = []
    for c in criteria:
        for e in c.get("evidence", []):
            if len(evidence) >= MAX_CONTEXT_ITEMS:
                break
            evidence.append(
                {
                    "field": c.get("criterion_id"),
                    "label": c.get("name"),
                    "quote": str(e.get("quote") or "")[:MAX_CONTEXT_QUOTE],
                    "page": e.get("page"),
                    "bboxes": list(e.get("bboxes") or [])[:8],
                }
            )
    report = _report(details)
    context: dict[str, Any] = {
        "screening_id": str(row.id),
        "application_id": str(row.application_id),
        "blind": row.blind,
        "overall_score": scoring.get("overall"),
        "criteria": [
            {"id": c.get("criterion_id"), "name": c.get("name"), "score": c.get("score")}
            for c in criteria
        ],
        "evidence": evidence,
        "guardrail_findings": [
            {
                "id": f.id,
                "type": f.type.value,
                "code": f.code,
                "severity": f.severity.value,
                "location": f.location,
                "remove": f.remove,
            }
            for f in (report.findings if report is not None else ())[:MAX_CONTEXT_ITEMS]
        ],
        "flags": [
            {"code": f.get("code"), "explanation": f.get("explanation")}
            for f in details.get("flags") or []
            if isinstance(f, dict)
        ],
        "knockouts": [
            {
                "id": k.get("id"),
                "matches": k.get("matches"),
                "suggested_action": k.get("suggested_action"),
            }
            for k in details.get("knockouts") or []
            if isinstance(k, dict)
        ],
        "review_reasons": list(row.review_reasons),
        "decision_ids": list(row.decision_ids),
    }
    if not row.blind:
        context["document_id"] = str(row.document_id)
    while (
        len(json.dumps(context, ensure_ascii=False).encode("utf-8")) > MAX_CONTEXT_BYTES
        and evidence
    ):
        evidence.pop()
    return context


async def propose(state: ScreeningState, runtime: Ctx) -> Command[Any]:
    ctx = runtime.context
    screening_id, _, _ = _ids(state)
    row, details = await _snapshot(ctx, screening_id)
    payload = _payload(row)
    try:
        approval_id = await request_approval(
            ctx,
            step_key=STEP_KEY,
            approval_type=APPROVAL_TYPE,
            title="Screening: shortlist or not",
            payload=payload,
            context=approval_context(row, details),
            entity_type=ENTITY_TYPE,
            entity_id=str(row.application_id),
        )
    except PolicyError as exc:
        async with ctx.transaction() as session:
            locked = await locked_screening(session, screening_id)
            locked.status = ScreeningStatus.FAILED.value
            locked.failure_reason = f"approval_not_possible:{type(exc).__name__}"[:200]
            touch(locked)
            await _audit(
                session, ctx, "hire.screening.failed", locked, {"reason": locked.failure_reason}
            )
        return Command(goto=END)
    async with ctx.transaction() as session:
        locked = await locked_screening(session, screening_id)
        locked.approval_id = uuid.UUID(approval_id)
        if locked.status == ScreeningStatus.PROCESSING.value:
            locked.status = ScreeningStatus.AWAITING_REVIEW.value
            await _audit(
                session,
                ctx,
                "hire.screening.awaiting_review",
                locked,
                {"approval_id": approval_id, "review_reasons": list(locked.review_reasons[:20])},
            )
        await refresh_decision_ids(session, locked)
        touch(locked)
    return Command(goto="wait", update={"approval_id": approval_id, "payload": payload})


async def wait(state: ScreeningState, runtime: Ctx) -> Command[Any]:
    outcome = await await_decision(runtime.context, state["approval_id"])
    if outcome is None:
        return Command(goto="wait")
    target = {
        "approved": "shortlist",
        "rejected": "not_shortlisted",
        "changes_requested": "returned",
    }
    return Command(goto=target[outcome], update={"outcome": outcome})


async def shortlist(state: ScreeningState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    screening_id, _, _ = _ids(state)
    try:
        await run_final_action(
            ctx,
            action=SHORTLIST,
            approval_id=state["approval_id"],
            step_key=STEP_KEY,
            payload=state["payload"],
        )
    except ExecutorRefusedError as exc:
        async with ctx.transaction() as session:
            row = await locked_screening(session, screening_id)
            row.failure_reason = f"executor_refused:{exc.reason}"[:200]
            touch(row)
            await _audit(session, ctx, "hire.screening.failed", row, {"reason": row.failure_reason})
        raise
    async with ctx.transaction() as session:
        await refresh_decision_ids(session, await locked_screening(session, screening_id))
    return {}


async def _close(
    ctx: WorkflowContext, screening_id: uuid.UUID, status: ScreeningStatus, approval_id: str
) -> None:
    async with ctx.transaction() as session:
        row = await locked_screening(session, screening_id)
        if row.status != ScreeningStatus.AWAITING_REVIEW.value:
            return
        row.status = status.value
        row.decided_at = datetime.now(UTC)
        touch(row)
        await refresh_decision_ids(session, row)
        await _audit(
            session, ctx, f"hire.screening.{status.value}", row, {"approval_id": approval_id}
        )


async def not_shortlisted(state: ScreeningState, runtime: Ctx) -> dict[str, Any]:
    await _close(
        runtime.context, _ids(state)[0], ScreeningStatus.NOT_SHORTLISTED, state["approval_id"]
    )
    return {}


async def returned(state: ScreeningState, runtime: Ctx) -> dict[str, Any]:
    await _close(runtime.context, _ids(state)[0], ScreeningStatus.RETURNED, state["approval_id"])
    return {}


# ------------------------------------------------------------------ graph


def build_graph() -> StateGraph[Any, Any, Any, Any]:
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(
        ScreeningState, context_schema=WorkflowContext
    )
    retry = node_retry()
    graph.add_node("ingest", ingest, retry_policy=retry, timeout=30)
    graph.add_node(
        "wait_parse",
        wait_parse,
        retry_policy=retry,
        timeout=PARSE_WAIT_SECONDS + 300,
        destinations=("guardrails", "checks"),
    )
    graph.add_node(
        "guardrails",
        guardrails,
        retry_policy=retry,
        timeout=300,
        destinations=("sanitize", "checks"),
    )
    graph.add_node(
        "sanitize", sanitize, retry_policy=retry, timeout=60, destinations=("parse_cv", "checks")
    )
    graph.add_node(
        "parse_cv", parse_cv, retry_policy=retry, timeout=300, destinations=("blind", "checks")
    )
    graph.add_node(
        "blind", blind_step, retry_policy=retry, timeout=60, destinations=("score", "checks")
    )
    graph.add_node("score", score, retry_policy=retry, timeout=300, destinations=("checks",))
    graph.add_node("checks", checks, retry_policy=retry, timeout=60, destinations=("propose",))
    graph.add_node("propose", propose, retry_policy=retry, timeout=60, destinations=("wait", END))
    graph.add_node("wait", wait, destinations=("wait", "shortlist", "not_shortlisted", "returned"))
    graph.add_node("shortlist", shortlist, retry_policy=retry, timeout=60)
    graph.add_node("not_shortlisted", not_shortlisted, retry_policy=retry, timeout=30)
    graph.add_node("returned", returned, retry_policy=retry, timeout=30)
    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "wait_parse")
    graph.add_edge("shortlist", END)
    graph.add_edge("not_shortlisted", END)
    graph.add_edge("returned", END)
    return graph
