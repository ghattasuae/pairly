# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Decision record hooks of the extraction, risk and policy services.

Each service calls its hook right after it stored its own rows and audit event, in the
same transaction, so the record commits or rolls back with the step. Each record links
the ids the service already has (extraction, assessment, rule evaluation, model calls,
audit event). Personal data (extracted values, facts, quotes, reasons) goes into the
encrypted ``sensitive`` part; the public parts hold ids, hashes, outcomes and scores.

Replay handlers named here are implemented in ``pairly_core.decisions.replay``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType
from pairly_core.decisions.models import DecisionKind
from pairly_core.decisions.serialize import EncodingError, encode_typed
from pairly_core.decisions.service import (
    DecisionDraft,
    PolicyRef,
    StoredDecision,
    prompt_refs,
    record_decision,
)

if TYPE_CHECKING:
    from pairly_core.extraction.engine import ExtractionResult
    from pairly_core.ingestion.models import Document
    from pairly_core.policy.rag import PolicyCheckResult
    from pairly_core.policy.rules import RuleSetEvaluation
    from pairly_core.policy.service import Subject
    from pairly_core.policy_versions import PublishedPolicy
    from pairly_core.risk.combiner import CombinedRisk
    from pairly_core.risk.types import RiskContext

HANDLER_RULES: Final = "core.rules"
HANDLER_RISK: Final = "core.risk_combiner"
HANDLER_EXTRACTION: Final = "core.extraction"
HANDLER_MODELS: Final = "core.models"
UNKNOWN_SUBJECT: Final = ("unspecified", "unspecified")


def _typed_or_none(value: Any) -> tuple[Any, str | None]:
    """``encode_typed(value)``, or ``(None, reason)`` when it cannot be stored exactly."""
    try:
        return encode_typed(value), None
    except EncodingError as exc:
        return None, str(exc)[:120]


# ------------------------------------------------------------------ extraction


async def on_extraction_saved(
    session: AsyncSession,
    *,
    extraction_id: uuid.UUID,
    document: Document,
    result: ExtractionResult[Any],
    actor_type: ActorType,
    actor_id: str,
    template_key: str | None,
    audit_event_id: uuid.UUID,
) -> StoredDecision:
    call = result.model_call
    fields = result.fields
    confidences = [f.confidence for f in fields]
    return await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.EXTRACTION,
            workflow=document.workflow,
            entity_type="document",
            entity_id=str(document.id),
            actor_type=actor_type,
            actor_id=actor_id,
            replay_handler=HANDLER_EXTRACTION,
            inputs={
                "document_ids": [str(document.id)],
                "document_hashes": [document.sha256],
                "extraction_id": str(extraction_id),
                "schema": result.schema.name,
                "schema_version": result.schema.version,
                "template_key": template_key,
            },
            prompt_versions=await prompt_refs(session, call.call_ids),
            model_call_ids=call.call_ids,
            model={
                "provider": call.provider,
                "model": call.model,
                "from_recording": call.from_recording,
                "chain_id": str(call.chain_id) if call.chain_id else None,
            },
            output={
                "status": result.status.value,
                "reasons": list(result.reasons[:50]),
                "fields": [
                    {
                        "path": f.path,
                        "kind": f.kind,
                        "confidence": f.confidence,
                        "threshold": f.threshold,
                        "required": f.required,
                        "flags": [flag.value for flag in f.flags],
                    }
                    for f in fields
                ],
            },
            confidence=min(confidences) if confidences else None,
            evidence={
                "quote_locations": [
                    {"path": f.path, "page": s.page, "start": s.start, "end": s.end}
                    for f in fields
                    for s in f.spans
                ]
            },
            sensitive={
                "fields": {f.path: f.json_value for f in fields},
                "raw": {f.path: f.raw for f in fields},
                "quotes": {f.path: [s.quote for s in f.spans] for f in fields if f.spans},
            },
            audit_event_ids=(audit_event_id,),
        ),
    )


async def on_extraction_corrected(
    session: AsyncSession,
    *,
    extraction_id: uuid.UUID,
    document_id: uuid.UUID,
    workflow: str,
    correction_id: uuid.UUID,
    field_path: str,
    old_value: Any,
    new_value: Any,
    reason: str,
    actor_id: uuid.UUID,
    audit_event_id: uuid.UUID,
) -> StoredDecision:
    """A person corrected an extracted field: a new record superseding the line's head."""
    return await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.EXTRACTION_CORRECTION,
            workflow=workflow,
            entity_type="document",
            entity_id=str(document_id),
            actor_type=ActorType.USER,
            actor_id=str(actor_id),
            inputs={
                "extraction_id": str(extraction_id),
                "correction_id": str(correction_id),
                "field_path": field_path,
            },
            output={"field_path": field_path, "cleared": new_value is None},
            final_action="corrected",
            sensitive={"old_value": old_value, "new_value": new_value},
            reason=reason,
            audit_event_ids=(audit_event_id,),
        ),
    )


# ------------------------------------------------------------------ risk


async def on_risk_assessed(
    session: AsyncSession,
    *,
    assessment_id: uuid.UUID,
    assessment_type: str,
    ctx: RiskContext,
    combined: CombinedRisk,
    actor_type: ActorType,
    actor_id: str,
    audit_event_id: uuid.UUID,
) -> StoredDecision:
    policies: list[PolicyRef] = []
    if combined.policy_version_id is not None:
        policies.append(
            PolicyRef(
                id=combined.policy_version_id,
                kind="risk_policy",
                name=assessment_type,
                version=int(combined.policy_version or 0),
                hash=str(combined.policy_hash),
            )
        )
    evidence: dict[str, Any] = {
        "risk_signals": [
            {
                "detector": w.signal.detector,
                "version": w.signal.version,
                "status": w.signal.status.value,
                "score": w.signal.score,
                "weight": w.weight,
                "label": w.signal.label,
                "severity": w.signal.severity.value,
                "evidence": [e.model_dump(mode="json") for e in w.signal.evidence],
                "explanation_key": w.signal.explanation_key,
            }
            for w in combined.signals
        ]
    }
    sensitive: dict[str, Any] | None = None
    report = ctx.guardrail_report
    if report is not None:
        evidence["guardrail_findings"] = [
            f.model_dump(mode="json", exclude={"evidence"}) for f in report.findings
        ]
        # The text that triggered a finding can be personal data.
        sensitive = {"guardrail_evidence": {f.id: f.evidence for f in report.findings}}
    hashes = [ctx.document_sha256] if ctx.document_sha256 else []
    return await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.RISK_SCORE,
            workflow=ctx.workflow,
            entity_type=ctx.entity_type,
            entity_id=ctx.entity_id,
            actor_type=actor_type,
            actor_id=actor_id,
            replay_handler=HANDLER_RISK,
            inputs={
                "assessment_id": str(assessment_id),
                "assessment_type": assessment_type,
                "document_hashes": hashes,
                "hash_matches": [
                    {"entity_type": m.entity_type, "entity_id": m.entity_id}
                    for m in ctx.hash_matches
                ],
            },
            policy_versions=policies,
            output={
                "score": combined.score,
                "level": combined.level.value,
                "requires_human": combined.requires_human,
                "reasons": list(combined.reasons),
            },
            evidence=evidence,
            sensitive=sensitive,
            audit_event_ids=(audit_event_id,),
        ),
    )


# ------------------------------------------------------------------ policy


def _subject(subject: Subject, workflow_default: str = "core") -> tuple[str, str, str]:
    entity_type, entity_id = (
        (subject.type, subject.id) if subject.type and subject.id else UNKNOWN_SUBJECT
    )
    return subject.workflow or workflow_default, entity_type, entity_id


async def on_rules_evaluated(
    session: AsyncSession,
    *,
    evaluation_id: uuid.UUID,
    policy: PublishedPolicy,
    evaluation: RuleSetEvaluation,
    facts: Mapping[str, Any],
    as_of: date,
    subject: Subject,
    actor_type: ActorType,
    actor_id: str,
    audit_event_id: uuid.UUID,
) -> StoredDecision:
    workflow, entity_type, entity_id = _subject(subject)
    typed_facts, problem = _typed_or_none(dict(facts))
    counts: dict[str, int] = {}
    for result in evaluation.results:
        counts[result.outcome.value] = counts.get(result.outcome.value, 0) + 1
    return await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.POLICY_CHECK,
            workflow=workflow,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_type=actor_type,
            actor_id=actor_id,
            # Facts that cannot be stored exactly cannot be replayed exactly.
            replay_handler=HANDLER_RULES if problem is None else None,
            inputs={
                "evaluation_id": str(evaluation_id),
                "rule_set": policy.name,
                "as_of": as_of.isoformat(),
                "facts_stored": problem is None,
            },
            policy_versions=(
                PolicyRef(policy.id, policy.kind, policy.name, policy.version, policy.content_hash),
            ),
            output={
                "overall": evaluation.overall.value,
                "requires_human": evaluation.requires_human,
                "counts": counts,
            },
            evidence={
                "rule_results": [
                    {
                        "rule_id": r.rule_id,
                        "outcome": r.outcome.value,
                        "severity": r.severity.value,
                        "error_code": r.error_code,
                        "triggered": r.triggered,
                    }
                    for r in evaluation.results
                ]
            },
            sensitive={
                "facts": typed_facts,
                "reasons": {
                    r.rule_id: {"en": r.reason, "ar": r.reason_ar} for r in evaluation.results
                },
            },
            audit_event_ids=(audit_event_id,),
        ),
    )


async def on_policy_checked(
    session: AsyncSession,
    *,
    evaluation_id: uuid.UUID,
    result: PolicyCheckResult,
    facts: Any,
    subject: Subject,
    actor_type: ActorType,
    actor_id: str,
    audit_event_id: uuid.UUID,
) -> StoredDecision:
    """A RAG policy check (model reading the tenant's policy documents)."""
    workflow, entity_type, entity_id = _subject(subject)
    call_ids = [result.model_call_id] if result.model_call_id else []
    return await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.POLICY_CHECK,
            workflow=workflow,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_type=actor_type,
            actor_id=actor_id,
            replay_handler=HANDLER_MODELS,
            inputs={
                "evaluation_id": str(evaluation_id),
                "policy_documents": [str(d) for d in result.policy_documents],
                "retrieved_chunks": [str(c) for c in result.retrieved],
            },
            prompt_versions=await prompt_refs(session, call_ids),
            model_call_ids=call_ids,
            output={
                "outcome": result.outcome.value,
                "requires_human": result.requires_human,
                "findings": len(result.findings),
                "rejected_citations": len(result.rejected),
            },
            confidence=result.confidence,
            evidence={
                "citations": [
                    {
                        "status": f.status,
                        "policy_document_id": str(f.citation.policy_document_id),
                        "name": f.citation.name,
                        "version": f.citation.version,
                        "page_start": f.citation.page_start,
                        "page_end": f.citation.page_end,
                        "section": f.citation.section,
                        "chunk_id": str(f.citation.chunk_id),
                    }
                    for f in result.findings
                ]
            },
            sensitive={
                "facts": facts,
                "question": result.question,
                "findings": [
                    {
                        "requirement": f.requirement,
                        "status": f.status,
                        "explanation": f.explanation,
                        "quote": f.citation.quote,
                    }
                    for f in result.findings
                ],
            },
            audit_event_ids=(audit_event_id,),
        ),
    )
