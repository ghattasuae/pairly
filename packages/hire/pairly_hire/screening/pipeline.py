# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The screening steps as plain functions, without the database.

The graph (``pairly_hire.screening.graph``) calls these with what it reads from the
database; the recordings builder (``synth.hire.screening``) and the evals
(``evals/hire``) call the very same functions, so a recorded request is exactly the
request the graph sends, and an eval measures the code that runs in production.

    report = scan(doc, filename)                          # deterministic guardrails
    report = (await classify(report, doc, router, ...)).report   # Hire default: always
    guarded = guard(doc, report)                          # hidden text, instructions out
    parsed = await parse_cv(doc, guarded, router, ...)    # CvProfile with evidence
    blinded = blind_input(guarded, contact, profile, config)
    scored = await score_cv(rubric, doc, blinded, router, ...)
    flags = claim_flags(profile, doc, guarded, as_of=..., ...)
    knockouts = check_knockouts(rubric, answers)

Every model step fails closed: it returns the reason instead of a result, and the
application still goes to a person with that reason.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Final

from pairly_core.documents.model import NormalizedDocument
from pairly_core.extraction.engine import DEFAULT_TASK, ExtractionResult, extract
from pairly_core.extraction.guarded import GuardedInput
from pairly_core.extraction.schema import compile_schema
from pairly_core.guardrails import ScanContext, scan_document
from pairly_core.guardrails.doc_classifier import (
    ClassifierMode,
    DocumentClassification,
    DocumentClassifierConfig,
    RouterDocumentClassifier,
    classify_document,
    should_classify,
)
from pairly_core.guardrails.report import GuardrailReport
from pairly_core.llm.errors import ModelUnavailable
from pairly_core.llm.types import CallContext, RouterResult
from pairly_hire.rubric import Rubric
from pairly_hire.scoring.credit import ScoringResult, credit
from pairly_hire.scoring.model import SCORE_TASK, CvScoreOutput, score_inputs
from pairly_hire.screening.blind import BlindConfig, BlindedInput, BlindSources, blind
from pairly_hire.screening.claims import ClaimConfig, Flag, all_checks
from pairly_hire.screening.profile import CvProfile, ParsedProfile, parsed_profile

if TYPE_CHECKING:
    from pairly_core.llm.router import ModelRouter

DOC_KIND: Final = "cv"
# Hire runs the whole-document injection classifier on every CV by default, and a
# classifier that cannot run flags the CV for a person (fail closed).
HIRE_CLASSIFIER: Final = DocumentClassifierConfig(mode=ClassifierMode.ALWAYS, on_failure="block")


def classifier_config(mode: str) -> DocumentClassifierConfig:
    """The Hire classifier settings for an organisation's mode (off, sampled, always)."""
    if mode == ClassifierMode.OFF.value:
        return HIRE_CLASSIFIER.model_copy(update={"mode": ClassifierMode.OFF})
    if mode == ClassifierMode.SAMPLED.value:
        return HIRE_CLASSIFIER.model_copy(
            update={"mode": ClassifierMode.SAMPLED, "sample_percent": 20.0}
        )
    return HIRE_CLASSIFIER


def scan(doc: NormalizedDocument, filename: str | None) -> GuardrailReport:
    """The deterministic guardrails (hidden text incl. the renderer check, instructions,
    stuffing, metadata, Unicode) on a parsed CV."""
    return scan_document(doc, context=ScanContext(filename=filename, doc_kind=DOC_KIND))


async def classify(
    report: GuardrailReport,
    doc: NormalizedDocument,
    router: ModelRouter | None,
    config: DocumentClassifierConfig,
    *,
    org_id: uuid.UUID,
) -> DocumentClassification | None:
    """The whole-document classifier pass, when the configuration asks for it. Without
    a router the pass cannot run: one ``classifier_unavailable`` finding (fail closed)."""
    if not should_classify(config, report):
        return None
    if router is None:
        return await classify_document(report, doc, _NoClassifier(), config, org_id=org_id)
    return await classify_document(
        report, doc, RouterDocumentClassifier(router), config, org_id=org_id
    )


class _NoClassifier:
    async def classify(self, group: Any, *, org_id: uuid.UUID) -> Any:
        raise ModelUnavailable("no model router in this process")


def guard(doc: NormalizedDocument, report: GuardrailReport) -> GuardedInput:
    """The sanitised model input. Raises ``ReportMismatchError`` for another text."""
    return GuardedInput.build(doc, report)


def parse_inputs(guarded: GuardedInput) -> dict[str, Any]:
    """The router inputs of the CV parse call (``core_extract``), as ``extract`` sends
    them (no reviewer hints). Recordings are keyed on them."""
    return {"schema": compile_schema(CvProfile).describe(), "document": guarded.data_block}


async def parse_cv(
    doc: NormalizedDocument,
    guarded: GuardedInput,
    router: ModelRouter,
    *,
    org_id: uuid.UUID,
    context: CallContext | None = None,
) -> ExtractionResult[CvProfile]:
    """The structured profile (``core_extract`` with ``CvProfile``). Never raises for a
    model failure: the result then has no data and says why."""
    return await extract(
        doc, guarded, CvProfile, DEFAULT_TASK, router=router, org_id=org_id, context=context
    )


def profile_of(result: ExtractionResult[CvProfile] | None) -> ParsedProfile | None:
    return parsed_profile(result) if result is not None else None


def blind_input(
    guarded: GuardedInput,
    contact: dict[str, Any] | None,
    profile: ParsedProfile | None,
    config: BlindConfig,
) -> BlindedInput:
    """What the scoring model reads: the sanitised CV with blind spans replaced."""
    return blind(guarded, BlindSources.build(contact, profile), config)


@dataclass(frozen=True, slots=True)
class Scored:
    """A scoring call that returned a validated answer, credited."""

    result: ScoringResult
    call: RouterResult[CvScoreOutput]

    @property
    def call_ids(self) -> list[uuid.UUID]:
        ids = [a.call_id for a in self.call.attempts if a.call_id is not None]
        return ids or [self.call.call_id]

    def model_info(self) -> dict[str, Any]:
        call = self.call
        return {
            "task": call.task,
            "prompt_version": call.prompt_version,
            "prompt_hash": call.prompt_hash,
            "provider": call.provider,
            "model": call.model,
            "tier": call.tier,
            "from_recording": call.from_recording,
            "call_ids": [str(c) for c in self.call_ids],
            "cost_micros": call.total_cost_micros,
            "latency_ms": call.latency_ms,
        }


async def score_cv(
    rubric: Rubric,
    doc: NormalizedDocument,
    blinded: BlindedInput,
    router: ModelRouter,
    *,
    org_id: uuid.UUID,
    context: CallContext | None = None,
) -> Scored | str:
    """Score the blinded CV against the rubric; the reason when the model cannot."""
    if not blinded.text.strip():
        return "score_skipped:no_text"
    try:
        call = await router.complete(
            task=SCORE_TASK,
            inputs=score_inputs(rubric, blinded.data_block),
            schema=CvScoreOutput,
            org_id=org_id,
            context=context,
        )
    except ModelUnavailable as exc:
        return f"score_model_unavailable:{exc.code}"
    return Scored(credit(call.value, rubric, doc, blinded), call)


def claim_flags(
    profile: ParsedProfile | None,
    doc: NormalizedDocument,
    guarded: GuardedInput,
    *,
    as_of: date,
    jobs_with_same_cv: int,
    config: ClaimConfig,
) -> list[Flag]:
    """Claim checks and AI-generated signals (information only, never a penalty).

    The skill check looks at the original CV text outside the skills the profile read
    and outside everything the guardrails removed, so hidden text never supports a
    skill."""
    exclude: list[tuple[int, int]] = [(s.start, s.end) for s in guarded.model_input.removed]
    if profile is not None:
        exclude += [(s.start, s.end) for s in profile.skills if s.start is not None and s.end]
    return all_checks(
        profile,
        doc.text,
        exclude,
        as_of=as_of,
        jobs_with_same_cv=jobs_with_same_cv,
        config=config,
    )
