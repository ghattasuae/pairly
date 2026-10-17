# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Policy checks over the tenant's own policy documents (retrieval augmented).

``check_against_policies`` retrieves the most relevant chunks of the organisation's
active policy documents (hybrid search), asks the model router for the requirements that
apply, with a verbatim quote for each, and keeps only findings whose quote really is in
the excerpt it cites. It returns a ``PolicyCheckResult``; it never raises for a model
problem.

Fail closed, in order:

* nothing relevant retrieved (or no active policy document): ``no_policy_found``. That
  is not a pass; the caller decides what an uncovered item needs.
* the model call fails, is over budget, or is not confident: ``error`` (human review).
* a finding cites an excerpt that was not given, or a quote that is not in that excerpt
  (after folding case, quotes and whitespace only): the finding is dropped, kept in
  ``rejected``, and the overall outcome is at least ``needs_review``.
* the model says nothing applies: ``no_policy_found`` with that reason.

Policy documents are written by the tenant, but their text is still data: excerpts go
to the model inside a delimited block with their own markers neutralised, and the system
prompt says to treat them as data.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.db import TenantError, current_org
from pairly_core.decisions.recorders import on_policy_checked
from pairly_core.llm.errors import ModelUnavailable
from pairly_core.llm.types import CallContext, RouterResult, TaskSpec
from pairly_core.policy.documents import POLICY_KIND, active_policy_documents
from pairly_core.policy.models import PolicyDocument, ResultSource, RuleResult
from pairly_core.policy.service import Subject
from pairly_core.retrieval.embeddings import Embedder, EmbeddingUnavailable
from pairly_core.retrieval.search import SearchHit, hybrid_search
from pairly_core.retrieval.text import fold

POLICY_CHECK_TASK: Final = TaskSpec(
    name="core_policy_check", prompt_version="v1", confidence_threshold=0.6
)
POLICY_CHECKED: Final = "core.policy.rag_checked"
MIN_QUOTE_CHARS: Final = 15
MAX_QUESTION_CHARS: Final = 2000
_MARKER_RE = re.compile(r"<(/?)(policy_excerpts|excerpt)", re.IGNORECASE)


class CheckOutcome(StrEnum):
    PASS = "pass"  # noqa: S105 (not a password)
    FAIL = "fail"
    NEEDS_REVIEW = "needs_review"
    ERROR = "error"
    NO_POLICY_FOUND = "no_policy_found"


# ------------------------------------------------------------------ model output


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyFindingOut(_Out):
    excerpt_id: Annotated[str, Field(pattern=r"^E[0-9]{1,2}$")]
    requirement: Annotated[str, Field(min_length=1, max_length=500)]
    quote: Annotated[str, Field(min_length=1, max_length=600)]
    status: Literal["satisfied", "violated", "needs_review", "unclear"]
    explanation: Annotated[str, Field(max_length=800)]


class PolicyCheckOutput(_Out):
    applicable: bool
    findings: Annotated[list[PolicyFindingOut], Field(max_length=10)]
    confidence: Annotated[float, Field(ge=0, le=1)]


class PolicyModel(Protocol):
    """What the check needs from the model router (``ModelRouter`` satisfies it)."""

    async def complete[T: BaseModel](
        self,
        *,
        task: TaskSpec,
        inputs: Mapping[str, Any],
        schema: type[T],
        org_id: uuid.UUID,
        context: CallContext | None = None,
    ) -> RouterResult[T]: ...


# ------------------------------------------------------------------ result


@dataclass(frozen=True, slots=True)
class Citation:
    policy_document_id: uuid.UUID
    name: str
    title: str
    version: int
    page_start: int | None
    page_end: int | None
    section: str | None
    chunk_id: uuid.UUID
    quote: str

    def as_json(self) -> dict[str, Any]:
        return {
            "policy_document_id": str(self.policy_document_id),
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "section": self.section,
            "chunk_id": str(self.chunk_id),
            "quote": self.quote,
        }


@dataclass(frozen=True, slots=True)
class PolicyFinding:
    requirement: str
    status: str
    explanation: str
    citation: Citation

    @property
    def outcome(self) -> CheckOutcome:
        return {
            "satisfied": CheckOutcome.PASS,
            "violated": CheckOutcome.FAIL,
        }.get(self.status, CheckOutcome.NEEDS_REVIEW)


@dataclass(frozen=True, slots=True)
class RejectedCitation:
    excerpt_id: str
    quote: str
    reason: str  # unknown_excerpt | quote_too_short | quote_not_found


@dataclass(frozen=True, slots=True)
class PolicyCheckResult:
    outcome: CheckOutcome
    reason: str
    question: str
    findings: tuple[PolicyFinding, ...] = ()
    rejected: tuple[RejectedCitation, ...] = ()
    retrieved: tuple[uuid.UUID, ...] = ()
    model_call_id: uuid.UUID | None = None
    prompt_version: str | None = None
    confidence: float | None = None
    policy_documents: tuple[uuid.UUID, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.outcome is CheckOutcome.PASS

    @property
    def requires_human(self) -> bool:
        return self.outcome in {CheckOutcome.FAIL, CheckOutcome.NEEDS_REVIEW, CheckOutcome.ERROR}


# ------------------------------------------------------------------ helpers


def neutralize(text: str) -> str:
    """Stop excerpt text from closing or opening the delimiting tags."""
    return _MARKER_RE.sub(lambda m: "\u2039" + m.group(1) + m.group(2), text)


def _attr(value: object) -> str:
    return str(value).replace('"', "'").replace("<", "\u2039").replace("\n", " ")[:200]


def render_excerpts(hits: Sequence[SearchHit], docs: Mapping[uuid.UUID, PolicyDocument]) -> str:
    parts = ["<policy_excerpts>"]
    for i, hit in enumerate(hits, start=1):
        doc = docs[hit.source_id]
        pages = (
            f"{hit.page_start}"
            if hit.page_start == hit.page_end
            else f"{hit.page_start}-{hit.page_end}"
        )
        parts.append(
            f'<excerpt id="E{i}" document="{_attr(doc.title)}" version="{doc.version}" '
            f'page="{pages}" section="{_attr(hit.section or "")}">'
        )
        parts.append(neutralize(hit.text))
        parts.append("</excerpt>")
    parts.append("</policy_excerpts>")
    return "\n".join(parts)


def json_safe(value: Any, depth: int = 0) -> Any:
    """Facts as JSON (exact numbers as strings). Deep structures are cut at 8 levels."""
    if depth > 8:
        return None
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k)[:128]: json_safe(v, depth + 1) for k, v in list(value.items())[:200]}
    if isinstance(value, list | tuple):
        return [json_safe(v, depth + 1) for v in list(value)[:200]]
    return str(value)[:1000]


def verify_quote(quote: str, chunk_text: str) -> str | None:
    """``None`` when the quote is verbatim in the chunk, else the rejection reason."""
    needle = fold(quote)
    if len(needle) < MIN_QUOTE_CHARS:
        return "quote_too_short"
    if needle not in fold(chunk_text):
        return "quote_not_found"
    return None


def _overall(findings: Sequence[PolicyFinding], rejected: bool) -> CheckOutcome:
    outcomes = {f.outcome for f in findings}
    if CheckOutcome.FAIL in outcomes:
        return CheckOutcome.FAIL
    if rejected or CheckOutcome.NEEDS_REVIEW in outcomes or not findings:
        return CheckOutcome.NEEDS_REVIEW
    return CheckOutcome.PASS


# ------------------------------------------------------------------ the check


async def check_against_policies(
    session: AsyncSession,
    *,
    facts: Mapping[str, Any],
    question: str,
    org_id: uuid.UUID,
    model: PolicyModel,
    embedder: Embedder,
    names: Sequence[str] | None = None,
    context: CallContext | None = None,
    limit: int = 6,
) -> PolicyCheckResult:
    """Check ``facts`` against the organisation's policy documents. Never raises for a
    retrieval or model failure: those come back as ``error``."""
    if current_org(session) != org_id:
        raise TenantError("the session is not bound to this organisation")
    question = question.strip()[:MAX_QUESTION_CHARS]
    docs = {d.id: d for d in await active_policy_documents(session, names=names)}
    if not docs:
        return PolicyCheckResult(
            CheckOutcome.NO_POLICY_FOUND, "No indexed policy document is active.", question
        )
    try:
        hits = await hybrid_search(
            session,
            kind=POLICY_KIND,
            query=question,
            source_ids=list(docs),
            embedder=embedder,
            limit=limit,
        )
    except EmbeddingUnavailable:
        return PolicyCheckResult(
            CheckOutcome.ERROR, "Policy search is unavailable. A person must review this.", question
        )
    if not hits:
        return PolicyCheckResult(
            CheckOutcome.NO_POLICY_FOUND,
            "No relevant policy text was found for this question.",
            question,
            policy_documents=tuple(docs),
        )
    retrieved = tuple(h.chunk_id for h in hits)
    inputs = {
        "question": question,
        "facts": json_safe(facts),
        "policy_excerpts": render_excerpts(hits, docs),
    }
    try:
        result = await model.complete(
            task=POLICY_CHECK_TASK,
            inputs=inputs,
            schema=PolicyCheckOutput,
            org_id=org_id,
            context=context,
        )
    except ModelUnavailable:
        return PolicyCheckResult(
            CheckOutcome.ERROR,
            "The policy check could not run. A person must review this.",
            question,
            retrieved=retrieved,
            policy_documents=tuple(docs),
        )
    output = result.value
    by_id = {f"E{i}": hit for i, hit in enumerate(hits, start=1)}
    findings: list[PolicyFinding] = []
    rejected: list[RejectedCitation] = []
    for item in output.findings:
        hit = by_id.get(item.excerpt_id)
        reason = "unknown_excerpt" if hit is None else verify_quote(item.quote, hit.text)
        if hit is None or reason is not None:
            rejected.append(RejectedCitation(item.excerpt_id, item.quote[:600], reason or ""))
            continue
        doc = docs[hit.source_id]
        findings.append(
            PolicyFinding(
                requirement=item.requirement,
                status=item.status,
                explanation=item.explanation,
                citation=Citation(
                    policy_document_id=doc.id,
                    name=doc.name,
                    title=doc.title,
                    version=doc.version,
                    page_start=hit.page_start,
                    page_end=hit.page_end,
                    section=hit.section,
                    chunk_id=hit.chunk_id,
                    quote=item.quote,
                ),
            )
        )

    def make(outcome: CheckOutcome, reason: str) -> PolicyCheckResult:
        return PolicyCheckResult(
            outcome,
            reason,
            question,
            findings=tuple(findings),
            rejected=tuple(rejected),
            retrieved=retrieved,
            model_call_id=result.call_id,
            prompt_version=result.prompt_version,
            confidence=result.confidence,
            policy_documents=tuple(docs),
        )

    if not output.applicable and not output.findings:
        return make(
            CheckOutcome.NO_POLICY_FOUND,
            "The retrieved policy text has no requirement for this question.",
        )
    if not output.applicable:
        return make(
            CheckOutcome.NEEDS_REVIEW,
            "The policy check gave an inconsistent answer. A person must review this.",
        )
    overall = _overall(findings, bool(rejected))
    if rejected and not findings:
        reason = "No citation could be verified in the policy text. A person must review this."
    elif rejected:
        reason = "Some citations could not be verified in the policy text."
    else:
        reason = {
            CheckOutcome.PASS: "Every applicable policy requirement is met.",
            CheckOutcome.FAIL: "A policy requirement is not met.",
            CheckOutcome.NEEDS_REVIEW: "A policy requirement needs a person to check it.",
        }[overall]
    return make(overall, reason)


async def record_policy_check(
    session: AsyncSession,
    result: PolicyCheckResult,
    *,
    facts: Mapping[str, Any],
    actor_type: ActorType,
    actor_id: str,
    subject: Subject | None = None,
) -> uuid.UUID:
    """Store the check in ``rule_results`` (one row per finding, or one summary row)."""
    org = current_org(session)
    if org is None:
        raise TenantError("policy results need a tenant context")
    subject = subject or Subject()
    evaluation_id = uuid.uuid4()
    safe_facts = json_safe(facts)
    base = {
        "org_id": org,
        "evaluation_id": evaluation_id,
        "source": ResultSource.RAG.value,
        "policy_version_id": None,
        "policy_version": None,
        "severity": None,
        "reason_ar": None,
        "error_code": None,
        "facts": safe_facts,
        "model_call_id": result.model_call_id,
        "workflow": subject.workflow,
        "subject_type": subject.type,
        "subject_id": subject.id,
        "created_by_type": actor_type.value,
        "created_by_id": actor_id,
    }
    rows: list[dict[str, Any]] = []
    for i, finding in enumerate(result.findings, start=1):
        rows.append(
            base
            | {
                "policy_name": finding.citation.name,
                "policy_version": finding.citation.version,
                "rule_id": f"finding_{i}",
                "outcome": finding.outcome.value,
                "reason": f"{finding.requirement}: {finding.explanation}"[:4000],
                "evidence": [finding.citation.as_json()],
            }
        )
    rows.append(
        base
        | {
            "policy_name": "policy_documents",
            "rule_id": "policy_check",
            "outcome": result.outcome.value,
            "reason": result.reason,
            "error_code": result.outcome.value if result.outcome is CheckOutcome.ERROR else None,
            "evidence": [
                {"question": result.question},
                {
                    "rejected": [
                        {"excerpt_id": r.excerpt_id, "quote": r.quote, "reason": r.reason}
                        for r in result.rejected
                    ]
                },
                {"retrieved": [str(c) for c in result.retrieved]},
            ],
        }
    )
    await session.execute(insert(RuleResult), rows)
    event = await append_audit(
        session,
        action=POLICY_CHECKED,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type=subject.type,
        entity_id=subject.id,
        workflow=subject.workflow,
        payload={
            "evaluation_id": str(evaluation_id),
            "outcome": result.outcome.value,
            "findings": len(result.findings),
            "rejected": len(result.rejected),
            "model_call_id": str(result.model_call_id) if result.model_call_id else None,
            "policy_documents": [str(d) for d in result.policy_documents],
        },
    )
    await on_policy_checked(
        session,
        evaluation_id=evaluation_id,
        result=result,
        facts=safe_facts,
        subject=subject,
        actor_type=actor_type,
        actor_id=actor_id,
        audit_event_id=event.id,
    )
    return evaluation_id
