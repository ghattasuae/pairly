# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Screening routes for reviewers (brief 3.3.9 explainability, 3.3.10 override).

* ``GET /v1/hire/applications/{id}/screening`` (``hire.screening.read``): the latest
  screening of an application: status, blind status, the model and prompt versions of
  every model step, the locked rubric version, the overall score (computed in code),
  each criterion's score, confidence, evidence quotes with their location in the CV,
  "why not higher" and flags, the guardrail findings with a one-sentence explanation,
  claim and AI-generated flags (information only), knockouts with suggested actions,
  review reasons, overrides and decision record ids. While the screening is blind the
  response holds no name, contact details or CV file id: ``text`` is the CV with the
  blind spans replaced (``[name]``, ``[university]``...), every evidence item carries
  its offsets in that text too, and guardrail evidence is redacted the same way.
* ``POST /v1/hire/applications/{id}/screening/overrides`` (``hire.screening.override``,
  people with a second factor): new scores for one or more criteria with a written
  reason (10 to 1000 characters). Writes a decision record (a person's action that
  supersedes the newest record of the application; the reason is stored encrypted),
  audits it and recomputes the overall score from the overridden scores. Overrides are
  kept for calibration (brief 3.3.11).
* ``POST /v1/hire/applications/{id}/screening/run`` (``hire.candidate.decide``, second
  factor): a new screening, for example after "changes requested" or a failed run.
  409 while one is processing or waiting for a person.
* ``GET`` and ``PUT /v1/hire/screening/settings`` (read: ``hire.screening.read``;
  change: ``hire.screening.manage``, second factor): blind mode, whether university
  names are hidden, the gap a claim check calls unexplained (months), the injection
  classifier mode (``off``, ``sampled``, ``always``; default ``always``).

Every route is scoped to the caller's organisation (Row Level Security): another
organisation's application is a 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select

from pairly_core.audit import actor_ref, append_audit
from pairly_core.crypto.types import unseal
from pairly_core.documents.model import NormalizedDocument
from pairly_core.errors import ConflictError, NotFoundError, ServiceUnavailableError
from pairly_core.guardrails.report import Finding, GuardrailReport
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.ingestion.models import Document
from pairly_core.ingestion.storage import StorageError
from pairly_core.rbac import Actor, ActorKind, SecondFactorRequiredError, authorize
from pairly_core.workflow.models import WorkflowRun
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_hire.models import HireApplication
from pairly_hire.permissions import CANDIDATE_DECIDE
from pairly_hire.rubric import SCALE_MAX, SCALE_MIN
from pairly_hire.scoring.aggregate import overall_score
from pairly_hire.screening.blind import (
    BlindCategory,
    BlindConfig,
    BlindSources,
    DisplayText,
    display_text,
    redact_text,
)
from pairly_hire.screening.models import HireScreening, HireScreeningSettings
from pairly_hire.screening.permissions import (
    SCREENING_MANAGE,
    SCREENING_OVERRIDE,
    SCREENING_READ,
)
from pairly_hire.screening.profile import ParsedProfile
from pairly_hire.screening.service import (
    SCREENING_ENTITY,
    WORKFLOW,
    read_details,
    record_override,
    rubric_ref,
    write_details,
)
from pairly_hire.screening.start import active_screening, create_screening, start_run

router = APIRouter(tags=["hire"])

MAX_TEXT: Final = 200_000


class HireScreeningEvidence(BaseModel):
    quote: str
    start: int
    end: int
    page: int | None
    bboxes: list[dict[str, float]]
    # Offsets in ``text`` (the blinded view) while the screening is blind.
    display_start: int | None = None
    display_end: int | None = None


class HireScreeningCriterion(BaseModel):
    criterion_id: str
    name: str
    weight: int
    must_have: bool
    score: int
    model_score: int | None
    confidence: float
    evidence: list[HireScreeningEvidence]
    rejected_quotes: int
    why_not_higher: str
    flags: list[str]
    overridden: bool = False


class HireScreeningFlag(BaseModel):
    code: str
    kind: str
    severity: str
    explanation: str
    details: dict[str, Any]


class HireScreeningFinding(BaseModel):
    id: str
    type: str
    code: str
    severity: str
    location: str | None
    removed_from_model_input: bool
    explanation: str
    evidence: str


class HireScreeningKnockout(BaseModel):
    id: str
    question: str
    expected_answer: str
    answer: str | None
    matches: bool
    route: str
    suggested_action: str | None
    suggestion: str | None


class HireScreeningOut(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    job_id: uuid.UUID
    status: str
    run_id: uuid.UUID | None
    run_status: str | None
    blind: bool
    blind_config: dict[str, Any]
    rubric_policy_id: uuid.UUID
    rubric_version: int | None
    scale: dict[str, int]
    scored: bool
    overall_score: float | None
    ai_overall_score: float | None
    criteria: list[HireScreeningCriterion]
    flags: list[HireScreeningFlag]
    guardrail_findings: list[HireScreeningFinding]
    knockouts: list[HireScreeningKnockout]
    review_reasons: list[str]
    models: dict[str, Any]
    profile: dict[str, Any] | None
    text: str | None
    document_id: uuid.UUID | None
    approval_id: uuid.UUID | None
    score_decision_id: str | None
    decision_ids: list[str]
    overrides: list[dict[str, Any]]
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None


class HireOverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scores: dict[
        Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,47}$")],
        Annotated[int, Field(ge=SCALE_MIN, le=SCALE_MAX)],
    ] = Field(min_length=1, max_length=24)
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=1000)]


class HireRunOut(BaseModel):
    screening_id: uuid.UUID
    status: str


class HireScreeningSettingsOut(BaseModel):
    blind_mode: bool
    blind_universities: bool
    gap_months: int
    classifier_mode: str


class HireScreeningSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blind_mode: bool | None = None
    blind_universities: bool | None = None
    gap_months: Annotated[int, Field(ge=1, le=60)] | None = None
    classifier_mode: Literal["off", "sampled", "always"] | None = None


# ------------------------------------------------------------------ helpers


def _step_up(actor: Actor) -> None:
    if actor.kind is ActorKind.USER and not actor.has_second_factor:
        raise SecondFactorRequiredError(
            "this action needs a sign-in with two-factor authentication"
        )


def _people_only(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise SecondFactorRequiredError("only a signed-in person can do this")


async def _application(session: OrgSession, application_id: uuid.UUID) -> HireApplication:
    app = await session.scalar(select(HireApplication).where(HireApplication.id == application_id))
    if app is None or app.submitted_at is None:
        raise NotFoundError("application not found")
    return app


async def _latest(session: OrgSession, application_id: uuid.UUID) -> HireScreening:
    row = await session.scalar(
        select(HireScreening)
        .where(HireScreening.application_id == application_id)
        .order_by(HireScreening.created_at.desc())
        .limit(1)
    )
    if row is None:
        raise NotFoundError("this application has no screening yet")
    return row


def _float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _sources(app: HireApplication, details: dict[str, Any]) -> BlindSources:
    contact = unseal(app, "contact")
    raw = details.get("profile")
    profile = ParsedProfile.from_dict(raw) if isinstance(raw, dict) else None
    return BlindSources.build(contact if isinstance(contact, dict) else None, profile)


def _blind_profile(profile: dict[str, Any] | None, config: BlindConfig) -> dict[str, Any] | None:
    """The parsed profile without what blind mode hides."""
    if profile is None:
        return None
    out = dict(profile)
    out["contact"] = None
    out["links"] = []
    if config.universities:
        out["institutions"] = []
        out["education"] = [
            {k: v for k, v in e.items() if k != "institution"}
            for e in profile.get("education") or []
            if isinstance(e, dict)
        ]
    return out


async def _original_text(request: Request, session: OrgSession, row: HireScreening) -> str | None:
    """The parsed CV text (for the reviewer's view), or ``None`` when unavailable."""
    runtime = getattr(request.app.state, "workflow_runtime", None)
    store = runtime.services.store if isinstance(runtime, WorkflowRuntime) else None
    doc = await session.scalar(select(Document).where(Document.id == row.document_id))
    if store is None or doc is None or not doc.parse_result_key:
        return None
    try:
        raw = await store.get_bytes(doc.parse_result_key, max_bytes=64 * 1024 * 1024)
        return NormalizedDocument.model_validate_json(raw).text[:MAX_TEXT]
    except (StorageError, ValueError):
        return None


def _finding_out(
    finding: Finding, blind: bool, sources: BlindSources, config: BlindConfig
) -> HireScreeningFinding:
    evidence = finding.evidence
    if blind:
        evidence = redact_text(evidence, sources, config)
    return HireScreeningFinding(
        id=finding.id,
        type=finding.type.value,
        code=finding.code,
        severity=finding.severity.value,
        location=finding.location,
        removed_from_model_input=finding.remove,
        explanation=finding.explain("en", "cv"),
        evidence=evidence,
    )


async def screening_view(
    request: Request, session: OrgSession, app: HireApplication, row: HireScreening
) -> HireScreeningOut:
    details = read_details(row)
    config = BlindConfig(
        enabled=bool(row.blind_config.get("enabled", True)),
        universities=bool(row.blind_config.get("universities", True)),
    )
    blind = row.blind
    sources = _sources(app, details)
    original = await _original_text(request, session, row)
    shown: DisplayText | None = None
    text: str | None = original
    if blind and original is not None:
        spans = [
            (int(s["start"]), int(s["end"]), _category(s["category"]))
            for s in row.blind_spans
            if isinstance(s, dict)
        ]
        shown = display_text(original, spans)
        text = shown.text
    elif blind:
        text = None
    scoring_raw = details.get("scoring")
    scoring: dict[str, Any] = scoring_raw if isinstance(scoring_raw, dict) else {}
    override_raw = details.get("override")
    override: dict[str, Any] = override_raw if isinstance(override_raw, dict) else {}
    overridden_scores: dict[str, int] = dict(override.get("scores") or {})
    criteria: list[HireScreeningCriterion] = []
    for c in scoring.get("criteria", []):
        if not isinstance(c, dict):
            continue
        evidence = []
        for e in c.get("evidence", []):
            start, end = int(e["start"]), int(e["end"])
            display = shown.range(start, end) if shown is not None else (None, None)
            evidence.append(
                HireScreeningEvidence(
                    quote=str(e.get("quote", "")),
                    start=start,
                    end=end,
                    page=e.get("page"),
                    bboxes=list(e.get("bboxes") or []) if not blind else [],
                    display_start=display[0],
                    display_end=display[1],
                )
            )
        cid = str(c["criterion_id"])
        criteria.append(
            HireScreeningCriterion(
                criterion_id=cid,
                name=str(c.get("name", cid)),
                weight=int(c.get("weight", 1)),
                must_have=bool(c.get("must_have")),
                score=(
                    overridden_scores[cid] if cid in overridden_scores else int(c.get("score", 0))
                ),
                model_score=c.get("model_score"),
                confidence=float(c.get("confidence", 0.0)),
                evidence=evidence,
                rejected_quotes=len(c.get("rejected", [])),
                why_not_higher=str(c.get("why_not_higher", "")),
                flags=list(c.get("flags", [])),
                overridden=cid in overridden_scores,
            )
        )
    report_raw = details.get("guardrail_report")
    report = GuardrailReport.model_validate(report_raw) if isinstance(report_raw, dict) else None
    findings = [
        _finding_out(f, blind, sources, config)
        for f in (report.findings if report is not None else ())
    ]
    flags = [
        HireScreeningFlag(
            code=str(f.get("code")),
            kind=str(f.get("kind")),
            severity=str(f.get("severity", "info")),
            explanation=(
                redact_text(str(f.get("explanation", "")), sources, config)
                if blind
                else str(f.get("explanation", ""))
            ),
            details=dict(f.get("details") or {}),
        )
        for f in details.get("flags") or []
        if isinstance(f, dict)
    ]
    knockouts = [HireScreeningKnockout(**k) for k in details.get("knockouts") or []]
    profile_raw = details.get("profile")
    profile = profile_raw if isinstance(profile_raw, dict) else None
    run_status = None
    if row.run_id is not None:
        run_status = await session.scalar(
            select(WorkflowRun.status).where(WorkflowRun.id == row.run_id)
        )
    return HireScreeningOut(
        id=row.id,
        application_id=row.application_id,
        job_id=row.job_id,
        status=row.status,
        run_id=row.run_id,
        run_status=run_status,
        blind=blind,
        blind_config=dict(row.blind_config),
        rubric_policy_id=row.rubric_policy_id,
        rubric_version=scoring.get("rubric_version"),
        scale={"min": SCALE_MIN, "max": SCALE_MAX},
        scored=bool(scoring),
        overall_score=_float(row.overall_score),
        ai_overall_score=scoring.get("overall"),
        criteria=criteria,
        flags=flags,
        guardrail_findings=findings,
        knockouts=knockouts,
        review_reasons=list(row.review_reasons),
        models=dict(row.model_info),
        profile=_blind_profile(profile, config) if blind else profile,
        text=text,
        document_id=None if blind else row.document_id,
        approval_id=row.approval_id,
        score_decision_id=row.score_decision_id,
        decision_ids=list(row.decision_ids),
        overrides=list(details.get("overrides") or []),
        failure_reason=row.failure_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
        decided_at=row.decided_at,
    )


def _category(value: Any) -> BlindCategory:
    try:
        return BlindCategory(str(value))
    except ValueError:
        return BlindCategory.NAME


# ------------------------------------------------------------------ routes


@router.get("/applications/{application_id}/screening", response_model=HireScreeningOut)
async def get_screening(
    application_id: uuid.UUID, request: Request, actor: CurrentActor, session: OrgSession
) -> HireScreeningOut:
    authorize(actor, SCREENING_READ)
    app = await _application(session, application_id)
    return await screening_view(request, session, app, await _latest(session, application_id))


@router.post("/applications/{application_id}/screening/overrides", response_model=HireScreeningOut)
async def override_scores(
    application_id: uuid.UUID,
    body: HireOverrideIn,
    request: Request,
    actor: CurrentActor,
    session: OrgSession,
) -> HireScreeningOut:
    authorize(actor, SCREENING_OVERRIDE)
    _people_only(actor)
    _step_up(actor)
    app = await _application(session, application_id)
    latest = await _latest(session, application_id)
    row = await session.scalar(
        select(HireScreening).where(HireScreening.id == latest.id).with_for_update()
    )
    if row is None:  # pragma: no cover - read just above
        raise NotFoundError("this application has no screening yet")
    details = read_details(row)
    scoring = details.get("scoring")
    if not isinstance(scoring, dict) or row.score_decision_id is None:
        raise ConflictError("this screening has no scores to override")
    ref, rubric = await rubric_ref(session, row.rubric_policy_id)
    known = {c.id for c in rubric.criteria}
    unknown = sorted(set(body.scores) - known)
    if unknown:
        raise ConflictError(f"the rubric has no criterion {unknown[0]}")
    previous = details.get("override") if isinstance(details.get("override"), dict) else {}
    current: dict[str, int] = {
        str(c["criterion_id"]): int(c["score"]) for c in scoring.get("criteria", [])
    }
    current.update({k: int(v) for k, v in dict(previous or {}).get("scores", {}).items()})
    changed = {
        cid: {"from": current.get(cid), "to": score}
        for cid, score in body.scores.items()
        if current.get(cid) != score
    }
    if not changed:
        raise ConflictError("the scores are already these")
    current.update(body.scores)
    overall = overall_score(rubric, current)
    kind, actor_id = actor_ref(actor)
    stored = await record_override(
        session,
        row=row,
        rubric=ref,
        scores=current,
        overall=overall,
        changed=changed,
        reason=body.reason,
        actor_type=kind,
        actor_id=actor_id,
    )
    now = datetime.now(UTC)
    overrides = list(details.get("overrides") or [])
    overrides.append(
        {
            "decision_id": stored.id,
            "by": actor_id,
            "at": now.isoformat(),
            "changed": changed,
            "overall": overall,
        }
    )
    overridden = {
        k: v
        for k, v in current.items()
        if k in body.scores or k in (previous or {}).get("scores", {})
    }
    write_details(
        row,
        override={"scores": overridden, "overall": overall, "decision_id": stored.id},
        overrides=overrides,
    )
    row.overall_score = Decimal(str(overall))
    row.decision_ids = [*row.decision_ids, stored.id]
    row.updated_at = now
    await append_audit(
        session,
        action="hire.screening.overridden",
        actor_type=kind,
        actor_id=actor_id,
        entity_type=SCREENING_ENTITY,
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={
            "decision_id": stored.id,
            "criteria": sorted(changed),
            "overall": str(overall),
        },
    )
    await session.flush()
    return await screening_view(request, session, app, row)


@router.post(
    "/applications/{application_id}/screening/run", status_code=202, response_model=HireRunOut
)
async def run_screening(
    application_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    actor: CurrentActor,
    session: OrgSession,
) -> HireRunOut:
    authorize(actor, CANDIDATE_DECIDE)
    _people_only(actor)
    runtime = getattr(request.app.state, "workflow_runtime", None)
    if not isinstance(runtime, WorkflowRuntime):
        raise ServiceUnavailableError("screening is not available here")
    await _application(session, application_id)
    if await active_screening(session, application_id) is not None:
        raise ConflictError("a screening of this application is already running or waiting")
    kind, actor_id = actor_ref(actor)
    try:
        row, _ = await create_screening(session, application_id, requested_by=(kind, actor_id))
    except ValueError as exc:
        raise ConflictError(str(exc)) from None
    await session.commit()
    session.expunge(row)
    background.add_task(start_run, runtime, row)
    return HireRunOut(screening_id=row.id, status=row.status)


async def _settings_row(session: OrgSession) -> HireScreeningSettings | None:
    return await session.scalar(select(HireScreeningSettings))


def _settings_out(row: HireScreeningSettings | None) -> HireScreeningSettingsOut:
    if row is None:
        return HireScreeningSettingsOut(
            blind_mode=True, blind_universities=True, gap_months=6, classifier_mode="always"
        )
    return HireScreeningSettingsOut(
        blind_mode=row.blind_mode,
        blind_universities=row.blind_universities,
        gap_months=row.gap_months,
        classifier_mode=row.classifier_mode,
    )


@router.get("/screening/settings", response_model=HireScreeningSettingsOut)
async def get_screening_settings(
    actor: CurrentActor, session: OrgSession
) -> HireScreeningSettingsOut:
    authorize(actor, SCREENING_READ)
    return _settings_out(await _settings_row(session))


@router.put("/screening/settings", response_model=HireScreeningSettingsOut)
async def put_screening_settings(
    body: HireScreeningSettingsIn, actor: CurrentActor, session: OrgSession
) -> HireScreeningSettingsOut:
    authorize(actor, SCREENING_MANAGE)
    _people_only(actor)
    row = await _settings_row(session)
    if row is None:
        row = HireScreeningSettings(id=uuid.uuid4(), org_id=actor.org_id)
        session.add(row)
    changes = body.model_dump(exclude_none=True)
    for key, value in changes.items():
        setattr(row, key, value)
    row.updated_by = actor.id
    row.updated_at = datetime.now(UTC)
    await session.flush()
    kind, actor_id = actor_ref(actor)
    await append_audit(
        session,
        action="hire.screening.settings_changed",
        actor_type=kind,
        actor_id=actor_id,
        entity_type="hire.screening_settings",
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={"changed": changes},
    )
    return _settings_out(row)
