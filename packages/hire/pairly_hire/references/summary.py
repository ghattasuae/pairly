# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The reference summary for the hiring team (brief 3.8.2).

* What the model sees (``summary_inputs``): the job title, the language and, per
  submitted reference, a label ("Referee 1") and the answers. Never the referee's name
  or address, never the CV. While the candidate is blind, every free text answer goes
  through the blind redactor first.
* The model (prompt ``hire_reference_summary/v1`` through the router; recorded for the
  demo) writes a short summary, strength and development themes and contradictions
  between referees. ``check_draft`` refuses unsafe text; a refused or failed call
  falls back to ``template_draft`` (deterministic, from the answers).
* Consistency with the CV is checked in code (``consistency.flags_for``), never by the
  model, and shown next to the summary.
* Kept per application, language and hash of the answers (encrypted); audited
  (``hire.reference.summarised``).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.types import seal
from pairly_core.llm.errors import ModelUnavailable
from pairly_core.llm.types import CallContext, TaskSpec
from pairly_hire.interviewing.kits import _problems
from pairly_hire.pipeline.hooks import application_blind, identity_redactor
from pairly_hire.pipeline.records import AppRecord, load_record
from pairly_hire.references.consistency import Flag, cv_jobs, flags_for
from pairly_hire.references.models import HireReferenceRequest, HireReferenceSummary
from pairly_hire.references.service import answers_of, for_application
from pairly_hire.sealing import read_sealed

if TYPE_CHECKING:
    from pairly_core.llm.router import ModelRouter

TASK: Final = TaskSpec(name="hire_reference_summary", prompt_version="v1")
PROMPT_VERSION: Final = f"{TASK.name}/{TASK.prompt_version}"
TEMPLATE_VERSION: Final = "template/v1"
WORKFLOW: Final = "hire"
ENTITY: Final = "hire.application"
TEXT_KEYS: Final = ("company", "candidate_title", "strengths", "development", "anything_else")

_Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=200)]
_Long = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]


class ReferenceDraft(BaseModel):
    """What the model returns."""

    model_config = ConfigDict(extra="forbid")

    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=600)]
    strengths: list[_Line] = Field(default_factory=list, max_length=3)
    development: list[_Line] = Field(default_factory=list, max_length=3)
    inconsistencies: list[_Long] = Field(default_factory=list, max_length=3)


def _sealed(model: Any, column: str, value: Any, *, org_id: uuid.UUID, row_id: object) -> Any:
    return seal(model, column, value, org_id=org_id, row_id=row_id)


def label_for(n: int, locale: str) -> str:
    return f"المرجع {n}" if locale == "ar" else f"Referee {n}"


def summary_inputs(
    *, job_title: str, locale: str, references: list[tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    return {
        "job_title": job_title,
        "language": "ar" if locale == "ar" else "en",
        "references": [{"label": label, "answers": answers} for label, answers in references],
    }


def check_draft(draft: ReferenceDraft) -> list[str]:
    problems = _problems("summary", draft.summary)
    for name, items in (
        ("strengths", draft.strengths),
        ("development", draft.development),
        ("inconsistencies", draft.inconsistencies),
    ):
        for i, item in enumerate(items):
            problems += _problems(f"{name}[{i}]", item)
    return sorted(set(problems))


_REHIRE_EN: Final = {"yes": "yes", "no": "no", "unsure": "unsure"}
_REHIRE_AR: Final = {"yes": "نعم", "no": "لا", "unsure": "غير متأكد"}


def _first(text: Any, limit: int = 200) -> str | None:
    if not isinstance(text, str) or len(text.strip()) < 3:
        return None
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 3].rstrip() + "..."


def template_draft(inputs: dict[str, Any]) -> ReferenceDraft:
    """The summary from the answers alone (no model): also the demo's scripted answer."""
    ar = inputs.get("language") == "ar"
    refs = list(inputs.get("references", []))
    ratings = [
        (r["label"], r["answers"].get("reliability"))
        for r in refs
        if isinstance(r["answers"].get("reliability"), int)
    ]
    rehire = _REHIRE_AR if ar else _REHIRE_EN
    tally = ", ".join(
        f"{r['label']}: {rehire.get(str(r['answers'].get('would_rehire')), '-')}" for r in refs
    )
    if ar:
        summary = f"أرسل {len(refs)} من المراجع إجاباتهم. العمل معه مرة أخرى: {tally}."
        if ratings:
            avg = round(sum(v for _, v in ratings) / len(ratings), 1)
            summary += f" متوسط الالتزام {avg} من 5."
    else:
        summary = f"{len(refs)} referees answered. Would work with them again: {tally}."
        if ratings:
            avg = round(sum(v for _, v in ratings) / len(ratings), 1)
            summary += f" Average reliability {avg} of 5."
    strengths = [
        f"{r['label']}: {line}" for r in refs if (line := _first(r["answers"].get("strengths")))
    ]
    development = [
        f"{r['label']}: {line}" for r in refs if (line := _first(r["answers"].get("development")))
    ]
    inconsistencies: list[str] = []
    if ratings:
        low = min(ratings, key=lambda x: x[1])
        high = max(ratings, key=lambda x: x[1])
        if high[1] - low[1] >= 3:
            inconsistencies.append(
                f"تتراوح تقييمات الالتزام من {low[1]} ({low[0]}) إلى {high[1]} ({high[0]})."
                if ar
                else f"Reliability ratings range from {low[1]} ({low[0]}) to {high[1]} ({high[0]})."
            )
    answers = {str(r["answers"].get("would_rehire")) for r in refs}
    if {"yes", "no"} <= answers:
        inconsistencies.append(
            "تختلف المراجع حول العمل معه مرة أخرى."
            if ar
            else "The referees differ on working with the candidate again."
        )
    return ReferenceDraft(
        summary=summary[:600],
        strengths=[s[:200] for s in strengths[:3]],
        development=[d[:200] for d in development[:3]],
        inconsistencies=inconsistencies[:3],
    )


@dataclass(frozen=True, slots=True)
class ReferenceItem:
    request: HireReferenceRequest
    label: str


@dataclass(frozen=True, slots=True)
class ReferenceSummary:
    record: AppRecord
    locale: str
    items: list[ReferenceItem]
    submitted: int
    draft: ReferenceDraft | None
    flags: list[Flag]
    source: str
    prompt_version: str
    from_recording: bool
    identity_hidden: bool
    # The answers as shown (redacted while blind), per request id.
    shown: dict[uuid.UUID, dict[str, Any]]


def _hash(rows: list[tuple[uuid.UUID, dict[str, Any]]]) -> str:
    raw = json.dumps(
        [[str(i), a] for i, a in rows], sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def build_summary(
    session: AsyncSession,
    application_id: uuid.UUID,
    locale: str,
    *,
    router: ModelRouter | None,
    actor_id: uuid.UUID | None,
) -> ReferenceSummary:
    locale = "ar" if locale == "ar" else "en"
    record = await load_record(session, application_id)
    blind = await application_blind(session, record.app.id)
    hidden = record.identity_hidden(blind)
    redact = (
        await identity_redactor(session, record.app.id, record.identity_contacts())
        if hidden
        else None
    )
    rows = await for_application(session, application_id)
    items: list[ReferenceItem] = []
    shown: dict[uuid.UUID, dict[str, Any]] = {}
    submitted: list[tuple[str, dict[str, Any], uuid.UUID]] = []
    for n, ref in enumerate(rows, start=1):
        label = label_for(n, locale)
        items.append(ReferenceItem(ref, label))
        if ref.status != "submitted" or ref.erased_at is not None:
            continue
        answers = answers_of(ref)
        if redact is not None:
            answers = {
                k: (redact(v) if k in TEXT_KEYS and isinstance(v, str) else v)
                for k, v in answers.items()
            }
        shown[ref.id] = answers
        submitted.append((label, answers, ref.id))
    empty = ReferenceSummary(
        record=record,
        locale=locale,
        items=items,
        submitted=len(submitted),
        draft=None,
        flags=[],
        source="none",
        prompt_version="",
        from_recording=False,
        identity_hidden=hidden,
        shown=shown,
    )
    if not submitted:
        return empty
    pairs = [(label, answers) for label, answers, _ in submitted]
    flags = flags_for(pairs, await cv_jobs(session, record.app.id))
    title = record.job.title_ar if locale == "ar" and record.job.title_ar else record.job.title
    inputs = summary_inputs(job_title=title, locale=locale, references=pairs)
    digest = _hash([(rid, answers) for _, answers, rid in submitted])
    existing = await session.scalar(
        select(HireReferenceSummary).where(
            HireReferenceSummary.application_id == record.app.id,
            HireReferenceSummary.locale == locale,
            HireReferenceSummary.answers_hash == digest,
        )
    )
    if existing is not None:
        try:
            kept = ReferenceDraft.model_validate(read_sealed(existing, "content"))
        except ValueError:
            kept = None
        if kept is not None:
            return replace(
                empty,
                draft=kept,
                flags=flags,
                source=existing.source,
                prompt_version=existing.prompt_version,
                from_recording=existing.from_recording,
            )
    draft = template_draft(inputs)
    result = None
    if router is not None:
        try:
            result = await router.complete(
                task=TASK,
                inputs=inputs,
                schema=ReferenceDraft,
                org_id=record.app.org_id,
                context=CallContext(
                    actor_id=str(actor_id) if actor_id else None,
                    workflow=WORKFLOW,
                    entity_type=ENTITY,
                    entity_id=str(record.app.id),
                ),
            )
        except ModelUnavailable:
            result = None
    if result is None or check_draft(result.value):
        # Nothing usable from the model: the summary from the answers, kept nowhere.
        return replace(
            empty,
            draft=draft,
            flags=flags,
            source="template",
            prompt_version=TEMPLATE_VERSION,
        )
    draft = result.value
    row = HireReferenceSummary(
        id=uuid.uuid4(),
        org_id=record.app.org_id,
        application_id=record.app.id,
        locale=locale,
        answers_hash=digest,
        request_ids=[rid for _, _, rid in submitted],
        source="model",
        prompt_version=PROMPT_VERSION,
        model_call_id=result.call_id,
        from_recording=result.from_recording,
    )
    row.content = _sealed(
        HireReferenceSummary,
        "content",
        draft.model_dump(mode="json"),
        org_id=row.org_id,
        row_id=row.id,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
            await append_audit(
                session,
                action="hire.reference.summarised",
                actor_type=ActorType.USER if actor_id else ActorType.SYSTEM,
                actor_id=str(actor_id) if actor_id else "pairly.hire.references",
                entity_type=ENTITY,
                entity_id=record.app.id,
                workflow=WORKFLOW,
                payload={
                    "summary_id": str(row.id),
                    "locale": locale,
                    "references": [str(rid) for _, _, rid in submitted],
                    "prompt_version": PROMPT_VERSION,
                    "model_call_id": str(result.call_id),
                    "from_recording": result.from_recording,
                },
            )
    except IntegrityError:
        pass  # kept by a concurrent reader; this one shows the same answer
    return replace(
        empty,
        draft=draft,
        flags=flags,
        source="model",
        prompt_version=PROMPT_VERSION,
        from_recording=result.from_recording,
    )
