# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Run an assessment, store it, and answer "does this need a person?" for approvals.

``assess`` loads the latest risk policy for the assessment type from
``policy_versions``, runs the detectors (fail closed), combines the signals, and stores
the assessment and its signals with the exact policy version, plus an audit event, in
the caller's transaction. ``requires_human`` is the helper approvals and workflow graphs
call: it reads a stored assessment and fails closed when there is none.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.db import TenantError, current_org
from pairly_core.decisions.recorders import on_risk_assessed
from pairly_core.ingestion.models import Document
from pairly_core.policy_versions import PolicyError, PublishedPolicy, latest_policy
from pairly_core.risk.combiner import (
    POLICY_KIND,
    CombinedRisk,
    RiskPolicy,
    combine,
    parse_risk_policy,
)
from pairly_core.risk.models import RiskAssessment, RiskSignalRow
from pairly_core.risk.registry import run_detectors
from pairly_core.risk.types import DetectorTools, EntityRef, RiskContext, RiskDetector

RISK_ASSESSED = "core.risk.assessed"
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_.]{0,63}$")
_ENTITY_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9._:@/+=-]{1,128}$")


@dataclass(frozen=True, slots=True)
class HumanReview:
    """What approvals need to know: must a person look at this, and why."""

    requires_human: bool
    reasons: tuple[str, ...]


def review_decision(combined: CombinedRisk) -> HumanReview:
    return HumanReview(combined.requires_human, combined.reasons)


@dataclass(frozen=True, slots=True)
class AssessmentRecord:
    id: uuid.UUID
    combined: CombinedRisk

    @property
    def review(self) -> HumanReview:
        return review_decision(self.combined)


async def load_risk_policy(
    session: AsyncSession, assessment_type: str
) -> tuple[RiskPolicy | None, PublishedPolicy | None, str | None]:
    """The latest policy for ``assessment_type`` (policy, row, problem code)."""
    published = await latest_policy(session, POLICY_KIND, assessment_type)
    if published is None:
        return None, None, None
    try:
        policy = parse_risk_policy(published.content)
    except PolicyError:
        return None, None, "risk_policy_invalid"
    if policy.name != assessment_type:
        return None, None, "risk_policy_invalid"
    return policy, published, None


async def assess(
    session: AsyncSession,
    ctx: RiskContext,
    detectors: Sequence[RiskDetector],
    *,
    assessment_type: str,
    actor_type: ActorType,
    actor_id: str,
    tools: DetectorTools | None = None,
) -> AssessmentRecord:
    """Assess ``ctx`` with ``detectors`` under the latest policy and store the result."""
    org = current_org(session)
    if org is None:
        raise TenantError("risk assessments need a tenant context")
    if ctx.org_id != org:
        raise TenantError("the risk context belongs to another organisation")
    if not _TYPE_RE.fullmatch(assessment_type):
        raise ValueError("invalid assessment type")
    if not _ENTITY_TYPE_RE.fullmatch(ctx.entity_type) or not _ENTITY_ID_RE.fullmatch(ctx.entity_id):
        raise ValueError("invalid entity reference")

    policy, published, problem = await load_risk_policy(session, assessment_type)
    signals = await run_detectors(detectors, ctx, tools or DetectorTools())
    combined = combine(signals, policy, published)
    if problem is not None:
        combined = CombinedRisk(
            score=combined.score,
            level=combined.level,
            requires_human=True,
            reasons=(problem, *combined.reasons),
            signals=combined.signals,
            policy_version_id=None,
            policy_version=None,
            policy_hash=None,
        )

    assessment_id = uuid.uuid4()
    await session.execute(
        pg_insert(RiskAssessment).values(
            id=assessment_id,
            org_id=org,
            workflow=ctx.workflow,
            assessment_type=assessment_type,
            entity_type=ctx.entity_type,
            entity_id=ctx.entity_id,
            policy_version_id=combined.policy_version_id,
            policy_version=combined.policy_version,
            policy_hash=combined.policy_hash,
            score=combined.score,
            level=combined.level.value,
            requires_human=combined.requires_human,
            reasons=list(combined.reasons),
            created_by_type=actor_type.value,
            created_by_id=actor_id,
        )
    )
    if combined.signals:
        await session.execute(
            pg_insert(RiskSignalRow),
            [
                {
                    "org_id": org,
                    "assessment_id": assessment_id,
                    "position": position,
                    "detector": w.signal.detector,
                    "detector_version": w.signal.version,
                    "status": w.signal.status.value,
                    "score": w.signal.score,
                    "weight": w.weight,
                    "label": w.signal.label,
                    "severity": w.signal.severity.value,
                    "evidence": [e.model_dump(mode="json") for e in w.signal.evidence],
                    "explanation_key": w.signal.explanation_key,
                }
                for position, w in enumerate(combined.signals)
            ],
        )
    event = await append_audit(
        session,
        action=RISK_ASSESSED,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="risk_assessment",
        entity_id=assessment_id,
        workflow=ctx.workflow,
        payload={
            "assessment_type": assessment_type,
            "entity_type": ctx.entity_type,
            "entity_id": ctx.entity_id,
            "policy_version_id": (
                str(combined.policy_version_id) if combined.policy_version_id else None
            ),
            "policy_version": combined.policy_version,
            "level": combined.level.value,
            "requires_human": combined.requires_human,
            "reasons": list(combined.reasons[:50]),
        },
    )
    await on_risk_assessed(
        session,
        assessment_id=assessment_id,
        assessment_type=assessment_type,
        ctx=ctx,
        combined=combined,
        actor_type=actor_type,
        actor_id=actor_id,
        audit_event_id=event.id,
    )
    return AssessmentRecord(assessment_id, combined)


async def requires_human(session: AsyncSession, assessment_id: uuid.UUID) -> HumanReview:
    """Read a stored assessment. No assessment (or another org's) means a person must look."""
    row = await session.scalar(select(RiskAssessment).where(RiskAssessment.id == assessment_id))
    if row is None:
        return HumanReview(True, ("risk_assessment_missing",))
    reasons = tuple(str(r) for r in row.reasons)
    if row.policy_version_id is None and "risk_policy_missing" not in reasons:
        reasons = (*reasons, "risk_policy_missing")
    return HumanReview(bool(row.requires_human) or row.policy_version_id is None, reasons)


async def latest_review(session: AsyncSession, *, entity_type: str, entity_id: str) -> HumanReview:
    """``requires_human`` for the newest assessment of an entity (none: a person looks)."""
    row_id = await session.scalar(
        select(RiskAssessment.id)
        .where(RiskAssessment.entity_type == entity_type, RiskAssessment.entity_id == entity_id)
        .order_by(RiskAssessment.created_at.desc(), RiskAssessment.id.desc())
        .limit(1)
    )
    if row_id is None:
        return HumanReview(True, ("risk_assessment_missing",))
    return await requires_human(session, row_id)


async def find_hash_matches(
    session: AsyncSession, sha256: str, *, exclude_document_id: uuid.UUID | None = None
) -> list[EntityRef]:
    """Other documents of this organisation with the same file hash (any workflow)."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("invalid sha256")
    query = select(Document.id, Document.workflow).where(Document.sha256 == sha256)
    if exclude_document_id is not None:
        query = query.where(Document.id != exclude_document_id)
    rows = (await session.execute(query.order_by(Document.created_at).limit(50))).all()
    return [EntityRef("document", str(r.id), r.workflow) for r in rows]
