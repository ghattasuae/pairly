# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Applications for the hiring team, the self-identification report, and Hire settings.

* ``GET /v1/hire/applications`` and ``GET /v1/hire/applications/{id}``
  (``hire.candidate.read``): submitted applications with the candidate's contact
  details (decrypted for the reader), answers, knockout results, consent, files and
  duplicate hints. Self-identification answers are never part of these responses.
  While an application is blind (screening's blind mode, until the shortlist or a stage
  that reveals identity, ``pipeline.records.hidden_identities``) the name reads
  "Candidate <reference>" and the contact details, CV and files are left out
  (``identity_hidden``).
* ``GET /v1/hire/reports/self-identification`` (``hire.selfid.read``): totals per
  category and option for one job, for bias reporting only. Counts under
  ``MIN_REPORT_COUNT`` are shown as ``null`` so nobody can be singled out. Each read is
  audited.
* ``GET /v1/hire/settings`` (``hire.job.read``) and ``PUT /v1/hire/settings``
  (``hire.settings.manage``, second factor): careers page visibility, retention of
  rejected candidates in months (default 12), the reply time in the acknowledgement.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from pairly_core.audit import actor_ref, audit_actor_action
from pairly_core.crypto.types import unseal
from pairly_core.errors import NotFoundError
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.policy_versions import get_policy
from pairly_core.rbac import ActorKind, authorize
from pairly_hire.jobs import get_job, org_settings, save_settings
from pairly_hire.models import ApplicationStatus, HireApplication, HireCandidate
from pairly_hire.permissions import CANDIDATE_READ, JOB_READ, SELFID_READ, SETTINGS_MANAGE
from pairly_hire.pipeline.records import hidden_identities, hidden_label
from pairly_hire.retention import apply_retention_months
from pairly_hire.rubric import parse_rubric
from pairly_hire.selfid.models import HireSelfIdentification
from pairly_hire.selfid.schema import CATEGORIES

router = APIRouter(tags=["hire"])

MIN_REPORT_COUNT: Final = 5
WORKFLOW: Final = "hire"


class HireApplicationSummary(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    candidate_id: uuid.UUID
    reference: str
    status: str
    candidate_name: str
    # Blind: the name is "Candidate <reference>" and contact details and files are empty.
    identity_hidden: bool
    needs_human_review: bool
    review_reasons: list[str]
    duplicate_hints: int
    submitted_at: datetime | None
    created_at: datetime


class HireApplicationList(BaseModel):
    items: list[HireApplicationSummary]


class HireKnockoutOut(BaseModel):
    id: str
    question: str
    answer: str | None
    expected_answer: str
    matches: bool
    reason: str


class HireApplicationDetail(HireApplicationSummary):
    email: str
    phone: str | None
    location: str | None
    profile_url: str | None
    rubric_policy_id: uuid.UUID
    rubric_version: int | None
    form_policy_id: uuid.UUID
    answers: dict[str, Any]
    knockouts: list[HireKnockoutOut]
    consent_version: str
    consent_at: datetime
    consent_locale: str
    cv_document_id: uuid.UUID | None
    files: dict[str, str]
    duplicate_signals: list[dict[str, Any]]
    acknowledged_at: datetime | None


class HireSelfIdReport(BaseModel):
    job_id: uuid.UUID
    responses: int | None
    min_count: int
    # category -> option -> count (null when under ``min_count``)
    categories: dict[str, dict[str, int | None]]


class HireSettingsOut(BaseModel):
    careers_enabled: bool
    rejected_retention_months: int
    response_days: int


class HireSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    careers_enabled: bool | None = None
    rejected_retention_months: Annotated[int, Field(ge=1, le=120)] | None = None
    response_days: Annotated[int, Field(ge=1, le=90)] | None = None


def _contact(app: HireApplication) -> dict[str, Any]:
    """The contact details sent with this application (``HireApplication.contact``)."""
    value = unseal(app, "contact")
    return value if isinstance(value, dict) else {}


def _summary(app: HireApplication, candidate: HireCandidate, hidden: bool) -> dict[str, Any]:
    name = (
        hidden_label(app)
        if hidden
        else str(_contact(app).get("full_name") or unseal(candidate, "full_name"))
    )
    return {
        "id": app.id,
        "job_id": app.job_id,
        "candidate_id": app.candidate_id,
        "reference": app.reference,
        "status": app.status,
        "candidate_name": name,
        "identity_hidden": hidden,
        "needs_human_review": app.needs_human_review,
        "review_reasons": list(app.review_reasons or []),
        "duplicate_hints": len(app.duplicate_signals or []),
        "submitted_at": app.submitted_at,
        "created_at": app.created_at,
    }


@router.get("/applications", response_model=HireApplicationList)
async def list_applications(
    actor: CurrentActor,
    session: OrgSession,
    job_id: Annotated[uuid.UUID | None, Query()] = None,
    needs_review: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> HireApplicationList:
    authorize(actor, CANDIDATE_READ)
    query = (
        select(HireApplication, HireCandidate)
        .join(HireCandidate, HireCandidate.id == HireApplication.candidate_id)
        .where(HireApplication.status != ApplicationStatus.INCOMPLETE.value)
        .order_by(HireApplication.submitted_at.desc(), HireApplication.id)
    )
    if job_id is not None:
        query = query.where(HireApplication.job_id == job_id)
    if needs_review is not None:
        query = query.where(HireApplication.needs_human_review.is_(needs_review))
    rows = (await session.execute(query.limit(limit))).all()
    hidden = await hidden_identities(session, [a for a, _ in rows])
    return HireApplicationList(
        items=[HireApplicationSummary(**_summary(a, c, a.id in hidden)) for a, c in rows]
    )


@router.get("/applications/{application_id}", response_model=HireApplicationDetail)
async def get_application(
    application_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> HireApplicationDetail:
    authorize(actor, CANDIDATE_READ)
    row = (
        await session.execute(
            select(HireApplication, HireCandidate)
            .join(HireCandidate, HireCandidate.id == HireApplication.candidate_id)
            .where(
                HireApplication.id == application_id,
                HireApplication.status != ApplicationStatus.INCOMPLETE.value,
            )
        )
    ).first()
    if row is None:
        raise NotFoundError("application not found")
    app, candidate = row
    stored = unseal(app, "answers") or {}
    given = stored.get("knockouts", {}) if isinstance(stored, dict) else {}
    policy = await get_policy(session, app.rubric_policy_id)
    knockouts: list[HireKnockoutOut] = []
    if policy is not None:
        for k in parse_rubric(policy.content).knockout_questions:
            answer = given.get(k.id) if isinstance(given, dict) else None
            knockouts.append(
                HireKnockoutOut(
                    id=k.id,
                    question=k.question,
                    answer=answer,
                    expected_answer=k.expected_answer,
                    matches=answer == k.expected_answer,
                    reason=k.reason,
                )
            )
    hidden = app.id in await hidden_identities(session, [app])
    contact = {} if hidden else _contact(app)
    return HireApplicationDetail(
        **_summary(app, candidate, hidden),
        email="" if hidden else str(unseal(candidate, "email") or ""),
        phone=contact.get("phone"),
        location=contact.get("location"),
        profile_url=contact.get("profile_url"),
        rubric_policy_id=app.rubric_policy_id,
        rubric_version=policy.version if policy else None,
        form_policy_id=app.form_policy_id,
        answers=dict(stored.get("questions", {})) if isinstance(stored, dict) else {},
        knockouts=knockouts,
        consent_version=app.consent_version,
        consent_at=app.consent_at,
        consent_locale=app.consent_locale,
        cv_document_id=None if hidden else app.cv_document_id,
        files={} if hidden else dict(app.files or {}),
        duplicate_signals=list(app.duplicate_signals or []),
        acknowledged_at=app.acknowledged_at,
    )


# ------------------------------------------------------------------ self-identification


@router.get("/reports/self-identification", response_model=HireSelfIdReport)
async def self_identification_report(
    actor: CurrentActor, session: OrgSession, job_id: Annotated[uuid.UUID, Query()]
) -> HireSelfIdReport:
    authorize(actor, SELFID_READ)
    await get_job(session, job_id)
    rows = list(
        await session.scalars(
            select(HireSelfIdentification).where(HireSelfIdentification.job_id == job_id)
        )
    )
    counts: dict[str, Counter[str]] = {key: Counter() for key in CATEGORIES}
    for row in rows:
        answers = unseal(row, "answers") or {}
        if not isinstance(answers, dict):
            continue
        for key, value in answers.items():
            if key in counts and isinstance(value, str):
                counts[key][value] += 1

    def shown(n: int) -> int | None:
        return n if n >= MIN_REPORT_COUNT else None

    await audit_actor_action(
        session,
        actor,
        "hire.selfid.report_viewed",
        entity_type="hire.job",
        entity_id=job_id,
        workflow=WORKFLOW,
        payload={"responses": len(rows)},
    )
    return HireSelfIdReport(
        job_id=job_id,
        responses=shown(len(rows)),
        min_count=MIN_REPORT_COUNT,
        categories={
            key: {o.id: shown(counts[key][o.id]) for o in category.options}
            for key, category in CATEGORIES.items()
        },
    )


# ------------------------------------------------------------------ settings


@router.get("/settings", response_model=HireSettingsOut)
async def get_settings(actor: CurrentActor, session: OrgSession) -> HireSettingsOut:
    authorize(actor, JOB_READ)
    current = await org_settings(session)
    return HireSettingsOut(
        careers_enabled=current.careers_enabled,
        rejected_retention_months=current.rejected_retention_months,
        response_days=current.response_days,
    )


@router.put("/settings", response_model=HireSettingsOut)
async def put_settings(
    body: HireSettingsIn, actor: CurrentActor, session: OrgSession
) -> HireSettingsOut:
    authorize(actor, SETTINGS_MANAGE)
    changes = body.model_dump(exclude_unset=True)
    kind, actor_id = actor_ref(actor)
    saved = await save_settings(
        session,
        org_id=actor.org_id,
        actor_type=kind,
        actor_id=actor_id,
        user_id=actor.id if actor.kind is ActorKind.USER else None,
        changes=changes,
    )
    if body.rejected_retention_months is not None:
        await apply_retention_months(session, saved.rejected_retention_months)
    return HireSettingsOut(
        careers_enabled=saved.careers_enabled,
        rejected_retention_months=saved.rejected_retention_months,
        response_days=saved.response_days,
    )
