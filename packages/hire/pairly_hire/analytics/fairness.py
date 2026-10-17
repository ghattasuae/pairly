# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The counterfactual fairness runner (brief 3.3.4): scheduled and on demand.

For a sample of an organisation's real screened applications (the newest
``sample_size`` with an overall score), ``run_for_org``:

1. reads the CV's parsed text and its guardrail report, rebuilds the sanitised text the
   scoring model read (checked against the digest the screening stored; a CV whose
   text changed is skipped, never guessed), and the redaction sources (the form's
   contact details and the parsed profile);
2. makes the counterfactual copies (``screening.counterfactual``: swapped names, gender
   markers and nationalities, synthetic values only) and puts each one and a control
   through the same guardrails and blind redaction;
3. records per copy whether the redaction hid every inserted value and whether the
   model would read exactly the same input as the control (both measured on every run);
4. live model mode only: re-scores the control and the copy and records the difference
   in overall score, flagged above ``threshold`` points. In recorded mode the model
   answers come from recordings, so the delta is ``pending_live_run``, never a number.

Nothing here reads self-identification. ``latest_summary`` feeds the bias report;
``evals/hire`` runs the same copies over the synthetic fairness pairs.

``run_fairness_job`` (``hire_fairness_runs``, hourly): an organisation with screened
applications gets a scheduled run when its last one is older than ``EVERY_DAYS``.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.types import unseal
from pairly_core.db import set_tenant
from pairly_core.documents.model import NormalizedDocument
from pairly_core.errors import ConflictError
from pairly_core.guardrails.report import GuardrailReport, text_digest
from pairly_core.guardrails.sanitize import ReportMismatchError
from pairly_core.ingestion.models import Document, DocumentStatus
from pairly_core.ingestion.storage import StorageError
from pairly_core.jobs.registry import JobContext
from pairly_core.llm.settings import ModelMode
from pairly_core.llm.types import CallContext
from pairly_core.policy_versions import PolicyError
from pairly_core.workflow.services import GraphServices
from pairly_hire.analytics.models import HireFairnessResult, HireFairnessRun
from pairly_hire.models import HireApplication
from pairly_hire.screening import counterfactual as cf
from pairly_hire.screening import pipeline
from pairly_hire.screening.blind import BlindConfig, BlindSources
from pairly_hire.screening.models import HireScreening
from pairly_hire.screening.profile import ParsedProfile
from pairly_hire.screening.service import read_details, rubric_ref

log = structlog.get_logger("pairly_hire.analytics.fairness")

JOB_NAME: Final = "hire_fairness_runs"
JOB_ACTOR: Final = "pairly.hire.fairness"
ENTITY: Final = "hire.fairness_run"
DEFAULT_SAMPLE: Final = 10
MAX_SAMPLE: Final = 50
DEFAULT_THRESHOLD: Final = Decimal("5.00")
EVERY_DAYS: Final = 7
STALE_RUN: Final = timedelta(hours=2)
SCORED_STATUSES: Final = ("awaiting_review", "shortlisted", "not_shortlisted", "returned")
MAX_NORMALIZED_BYTES: Final = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Sample:
    screening_id: uuid.UUID
    application_id: uuid.UUID
    document_id: uuid.UUID
    rubric_policy_id: uuid.UUID
    sanitized_sha256: str | None
    blind: BlindConfig
    report: GuardrailReport | None
    sources: BlindSources


def model_mode(services: GraphServices) -> str:
    router = services.router
    if router is None or getattr(router, "mode", ModelMode.RECORDED) == ModelMode.RECORDED:
        return "recorded"
    return "live"


async def _audit(session: AsyncSession, run: HireFairnessRun, action: str, actor: str) -> None:
    await append_audit(
        session,
        action=action,
        actor_type=ActorType.SYSTEM if run.trigger == "scheduled" else ActorType.USER,
        actor_id=actor,
        entity_type=ENTITY,
        entity_id=run.id,
        workflow="hire",
        payload={
            "status": run.status,
            "mode": run.mode,
            "sample_size": run.sample_size,
            "applications": run.applications,
            "copies": run.copies,
            "flagged": run.flagged,
        },
    )


async def start_run(
    session: AsyncSession,
    *,
    trigger: str,
    started_by: str,
    mode: str,
    sample_size: int = DEFAULT_SAMPLE,
    threshold: Decimal = DEFAULT_THRESHOLD,
) -> HireFairnessRun:
    """A new ``running`` run; refused while another one runs (unless it went stale)."""
    now = datetime.now(UTC)
    busy = await session.scalar(
        select(HireFairnessRun.id).where(
            HireFairnessRun.status == "running", HireFairnessRun.started_at > now - STALE_RUN
        )
    )
    if busy is not None:
        raise ConflictError("a fairness run is already in progress")
    run = HireFairnessRun(
        id=uuid.uuid4(),
        status="running",
        trigger=trigger,
        mode=mode,
        threshold=threshold,
        sample_size=max(1, min(sample_size, MAX_SAMPLE)),
        summary={},
        started_by=started_by[:128],
        started_at=now,
    )
    session.add(run)
    await session.flush()
    await _audit(session, run, "hire.fairness.started", started_by)
    return run


async def _samples(session: AsyncSession, size: int) -> list[Sample]:
    rows = list(
        await session.scalars(
            select(HireScreening)
            .where(
                HireScreening.status.in_(SCORED_STATUSES),
                HireScreening.overall_score.is_not(None),
            )
            .order_by(HireScreening.created_at.desc())
            .limit(size * 3)
        )
    )
    seen: set[uuid.UUID] = set()
    out: list[Sample] = []
    for row in rows:
        if row.application_id in seen or len(out) >= size:
            continue
        seen.add(row.application_id)
        app = await session.scalar(
            select(HireApplication).where(HireApplication.id == row.application_id)
        )
        if app is None:
            continue
        details = read_details(row)
        contact = unseal(app, "contact")
        raw_profile = details.get("profile")
        profile = ParsedProfile.from_dict(raw_profile) if isinstance(raw_profile, dict) else None
        raw_report = details.get("guardrail_report")
        try:
            report = (
                GuardrailReport.model_validate(raw_report) if isinstance(raw_report, dict) else None
            )
        except ValidationError:
            report = None
        config = row.blind_config or {}
        out.append(
            Sample(
                screening_id=row.id,
                application_id=row.application_id,
                document_id=row.document_id,
                rubric_policy_id=row.rubric_policy_id,
                sanitized_sha256=row.sanitized_sha256,
                blind=BlindConfig(
                    enabled=bool(config.get("enabled", row.blind)),
                    universities=bool(config.get("universities", True)),
                ),
                report=report,
                sources=cf.sources_for(contact if isinstance(contact, dict) else {}, profile),
            )
        )
    return out


async def _sanitized_text(
    session: AsyncSession, services: GraphServices, sample: Sample
) -> str | None:
    """The text the scoring model read, or None when it cannot be rebuilt exactly."""
    store = services.store
    if store is None or sample.report is None:
        return None
    doc = await session.scalar(select(Document).where(Document.id == sample.document_id))
    if doc is None or doc.status != DocumentStatus.PARSED or not doc.parse_result_key:
        return None
    try:
        raw = await store.get_bytes(doc.parse_result_key, max_bytes=MAX_NORMALIZED_BYTES)
        normalized = NormalizedDocument.model_validate_json(raw)
        guarded = pipeline.guard(normalized, sample.report)
    except (StorageError, ValidationError, ValueError, ReportMismatchError):
        return None
    if sample.sanitized_sha256 is None or text_digest(guarded.text) != sample.sanitized_sha256:
        return None
    return guarded.text


def summarize(
    run: HireFairnessRun, results: list[HireFairnessResult], skipped: Counter[str]
) -> dict[str, Any]:
    by_dimension: dict[str, dict[str, Any]] = {}
    for dimension in cf.DIMENSIONS:
        rows = [r for r in results if r.dimension == dimension]
        deltas = [float(r.delta) for r in rows if r.delta is not None]
        by_dimension[dimension] = {
            "copies": len(rows),
            "redaction_complete": sum(1 for r in rows if r.redaction_complete),
            "input_identical": sum(1 for r in rows if r.input_identical),
            "max_delta": max(deltas) if deltas else None,
            "flagged": sum(1 for r in rows if r.flagged),
        }
    scored = [r for r in results if r.model_status == "scored"]
    return {
        "run_id": str(run.id),
        "status": run.status,
        "mode": run.mode,
        "trigger": run.trigger,
        "threshold": float(run.threshold),
        "applications": run.applications,
        "pairs": len(results),
        "flagged": run.flagged,
        "redaction_complete": sum(1 for r in results if r.redaction_complete),
        "input_identical": sum(1 for r in results if r.input_identical),
        "model_deltas": "measured" if scored else cf.PENDING,
        "scored": len(scored),
        "by_dimension": by_dimension,
        "skipped": dict(skipped),
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


async def execute_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    services: GraphServices,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
) -> dict[str, Any]:
    """Run the checks of a started run and close it (completed, or failed with why)."""
    session = sessionmaker()
    set_tenant(session, org_id, None)
    async with session, session.begin():
        run = await session.scalar(select(HireFairnessRun).where(HireFairnessRun.id == run_id))
        if run is None or run.status != "running":
            return {}
        size, threshold, mode = run.sample_size, run.threshold, run.mode
        samples = await _samples(session, size)
    live = mode == "live"
    results: list[HireFairnessResult] = []
    skipped: Counter[str] = Counter()
    applications = 0
    try:
        for sample in samples:
            session = sessionmaker()
            set_tenant(session, org_id, None)
            async with session, session.begin():
                text = await _sanitized_text(session, services, sample)
                rubric = None
                if text is not None and live:
                    try:
                        rubric = (await rubric_ref(session, sample.rubric_policy_id))[1]
                    except PolicyError:
                        skipped["rubric_missing"] += 1
                        continue
            if text is None:
                skipped["text_unavailable"] += 1
                continue
            names = list(sample.sources.names)
            swaps = cf.make_swaps(text, names, list(sample.sources.emails), sample.application_id)
            if not swaps:
                skipped["nothing_to_swap"] += 1
                continue
            applications += 1
            context = CallContext(
                actor_id=JOB_ACTOR,
                workflow="hire",
                entity_type=ENTITY,
                entity_id=str(run_id),
            )
            control_score: float | None = None
            for swap in swaps:
                check = cf.check_copy(text, sample.sources, sample.blind, swap)
                score = (
                    await cf.score_pair(
                        check,
                        rubric,
                        services.router,
                        live=True,
                        org_id=org_id,
                        context=context,
                        control_score=control_score,
                    )
                    if live and rubric is not None
                    else cf.PairScore(cf.PENDING)
                )
                if score.control is not None:
                    control_score = score.control
                delta = score.delta
                results.append(
                    HireFairnessResult(
                        id=uuid.uuid4(),
                        org_id=org_id,
                        run_id=run_id,
                        application_id=sample.application_id,
                        screening_id=sample.screening_id,
                        dimension=swap.dimension,
                        inserted=list(check.inserted),
                        leaked=list(check.leaked),
                        redaction_complete=check.redaction_complete,
                        input_identical=check.input_identical,
                        blind=sample.blind.enabled,
                        model_status=score.status,
                        control_score=Decimal(str(score.control))
                        if score.control is not None
                        else None,
                        copy_score=Decimal(str(score.copy)) if score.copy is not None else None,
                        delta=Decimal(str(delta)) if delta is not None else None,
                        flagged=(Decimal(str(delta)) > threshold) if delta is not None else None,
                    )
                )
    except Exception as exc:
        log.warning("hire_fairness_run_failed", run_id=str(run_id), error=type(exc).__name__)
        session = sessionmaker()
        set_tenant(session, org_id, None)
        async with session, session.begin():
            run = await session.scalar(
                select(HireFairnessRun).where(HireFairnessRun.id == run_id).with_for_update()
            )
            if run is not None and run.status == "running":
                run.status = "failed"
                run.failure_reason = type(exc).__name__[:200]
                run.finished_at = datetime.now(UTC)
                await _audit(session, run, "hire.fairness.failed", run.started_by)
        raise
    session = sessionmaker()
    set_tenant(session, org_id, None)
    async with session, session.begin():
        run = await session.scalar(
            select(HireFairnessRun).where(HireFairnessRun.id == run_id).with_for_update()
        )
        if run is None or run.status != "running":
            return {}
        for row in results:
            session.add(row)
        run.status = "completed"
        run.applications = applications
        run.copies = len(results)
        run.flagged = sum(1 for r in results if r.flagged)
        run.finished_at = datetime.now(UTC)
        run.summary = summarize(run, results, skipped)
        await session.flush()
        await _audit(session, run, "hire.fairness.completed", run.started_by)
        return dict(run.summary)


async def latest_summary(session: AsyncSession) -> dict[str, Any] | None:
    """The newest completed run's summary (for the bias report), or None."""
    run = await session.scalar(
        select(HireFairnessRun)
        .where(HireFairnessRun.status == "completed")
        .order_by(HireFairnessRun.started_at.desc())
        .limit(1)
    )
    return dict(run.summary) if run is not None else None


async def _due(session: AsyncSession, now: datetime) -> bool:
    has_scored = await session.scalar(
        select(HireScreening.id).where(HireScreening.overall_score.is_not(None)).limit(1)
    )
    if has_scored is None:
        return False
    last = await session.scalar(
        select(HireFairnessRun.started_at)
        .where(HireFairnessRun.trigger == "scheduled")
        .order_by(HireFairnessRun.started_at.desc())
        .limit(1)
    )
    return last is None or last < now - timedelta(days=EVERY_DAYS)


async def run_fairness_job(ctx: JobContext) -> dict[str, Any]:
    """``hire_fairness_runs``: hourly; a scheduled run per organisation every week."""
    from pairly_hire.pipeline.rediscovery import org_ids

    services = ctx.runtime.services
    totals: dict[str, Any] = {"orgs": 0, "runs": 0, "failed": 0}
    now = datetime.now(UTC)
    for org_id in await org_ids(ctx):
        totals["orgs"] += 1
        session = ctx.sessionmaker()
        set_tenant(session, org_id, None)
        try:
            async with session, session.begin():
                if not await _due(session, now):
                    continue
                run = await start_run(
                    session, trigger="scheduled", started_by=JOB_ACTOR, mode=model_mode(services)
                )
                run_id = run.id
            await execute_run(ctx.sessionmaker, services, org_id, run_id)
            totals["runs"] += 1
        except Exception as exc:
            log.warning("hire_fairness_job_failed", org_id=str(org_id), error=type(exc).__name__)
            totals["failed"] += 1
    return totals
