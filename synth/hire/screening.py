# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Scripted model responses for the screening prompts (offline tests, demo and evals).

The screening graph calls three prompts per CV: the whole-document injection
classifier (``guardrails_document_classify``), the CV parse (``core_extract`` with
``CvProfile``) and the scoring (``hire_cv_score``). ``ScriptedRouter`` stands in for
the model router: it runs the very pipeline functions the graph runs
(``pairly_hire.screening.pipeline``), keys every request exactly as ``ModelRouter``
keys it (task, prompt version and hash, provider, model, inputs, schema hash,
settings), and answers from the synthetic ground truth:

* classifier: the planted span of a paraphrased instruction attack, else no span;
* CV parse: the ground-truth profile, each value quoted from the CV line that shows it;
* scoring: the synthetic rubric label of each criterion (score 0 to 4) with its
  evidence lines, cut to the part a model reading the BLINDED text could quote (a line
  that holds a hidden university keeps its other parts), and a "why not higher" from
  the label guide's next anchor.

These are SCRIPTED test fixtures, not model output. They say nothing about how well a
real model parses or scores: any accuracy or agreement number needs a live run
(``evals/hire`` reports those as ``pending_live_run``). What they do test for real is
everything deterministic around the model: guardrails, sanitising, evidence location,
blind redaction, no evidence no credit, the overall score, claim checks, knockouts and
the graph's records.

``build_bundle()`` writes ``synth/hire/recordings/hire_screening.json`` for the demo
applications (``DEMO_APPLICATIONS``, each a synthetic CV applied to the synthetic
backend engineer job, whose rubric is locked with its label guide anchors). The tests
and a demo seed import that bundle; the router replays it in ``recorded`` mode.
``python -m synth.hire --check`` reports it stale when a prompt, a schema, a parser or
a generator changes.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel

from pairly_core.documents.docx import DocxParser
from pairly_core.documents.model import NormalizedDocument
from pairly_core.documents.ocr import ImageParser
from pairly_core.documents.pdf import PdfParser
from pairly_core.documents.registry import ParseOptions
from pairly_core.extraction.evidence import normalize
from pairly_core.guardrails.doc_classifier import DOC_CLASSIFY_TASK, segments
from pairly_core.guardrails.validation import find_quote
from pairly_core.llm.bundle import BUNDLE_FORMAT, BUNDLE_VERSION, dumps_bundle, parse_bundle
from pairly_core.llm.catalog import load_catalog, load_prices
from pairly_core.llm.errors import RecordingMissing
from pairly_core.llm.hashing import json_round_trip, request_hash, schema_hash
from pairly_core.llm.prompt_registry import PromptRegistry, core_prompt_root
from pairly_core.llm.router import USER_TEMPLATE_VERSION
from pairly_core.llm.types import CallContext, RouterResult, TaskSpec
from pairly_hire.rubric import Criterion, Rubric
from pairly_hire.scoring.credit import ScoringResult
from pairly_hire.screening import pipeline
from pairly_hire.screening.blind import BlindConfig, BlindedInput
from pairly_hire.screening.claims import ClaimConfig, Flag
from pairly_hire.screening.knockouts import KnockoutCheck, check_knockouts
from pairly_hire.screening.profile import ParsedProfile
from synth.hire.dataset import DocSpec
from synth.hire.facts import AS_OF
from synth.hire.jobs import load_jobs
from synth.hire.models import Job

PROVIDER: Final = "anthropic"
HIRE_PROMPTS: Final = Path(__file__).resolve().parents[2] / "packages" / "hire" / "pairly_hire"
BUNDLE_PATH: Final = Path(__file__).resolve().parent / "recordings" / "hire_screening.json"
OPTIONS: Final = ParseOptions(ocr_languages="eng")
SCRIPTED_CONFIDENCE: Final = 0.9
DEMO_JOB: Final = "backend_engineer"
# Synthetic CVs applied to the backend engineer job for the demo and the database tests:
# a clean PDF, a clean DOCX, a sidebar PDF whose knockout answer differs, hidden text, a
# paraphrased instruction (classifier), a claim attack, and a fairness base CV with a
# "Personal details" section.
DEMO_APPLICATIONS: Final = (
    "cv-0026",
    "cv-0029",
    "cv-0057",
    "atk-white-text-skills-pdf",
    "atk-paraphrased-en-1-pdf",
    "atk-title-inflation-backend-pdf",
    "fair-01-base",
)
SCRIPTED_NOTE: Final = "scripted from synthetic ground truth; not model output"
SCREENING_AS_OF: Final = date(AS_OF[0], AS_OF[1], 28)


class RecordingError(RuntimeError):
    """A scripted answer does not fit its schema, or the catalog has no model."""


# ------------------------------------------------------------------ the job and the person


def screening_rubric(job: Job) -> Rubric:
    """The job's rubric with its label guide anchors (what a recruiter would lock)."""
    criteria: dict[str, Criterion] = {}
    for c in job.rubric.criteria:
        criteria[c.id] = c.model_copy(update={"anchors": tuple(job.label_guide[c.id].anchors)})
    return job.rubric.model_copy(
        update={
            "must_haves": [criteria[c.id] for c in job.rubric.must_haves],
            "nice_to_haves": [criteria[c.id] for c in job.rubric.nice_to_haves],
        }
    )


def job_by_id(job_id: str) -> Job:
    return next(j for j in load_jobs() if j.id == job_id)


def contact_for(spec: DocSpec) -> dict[str, Any]:
    """The contact details the candidate types into the apply form (as stored)."""
    c = spec.record.ground_truth.contact
    return {
        "email": c.email,
        "full_name": c.name,
        "phone": c.phone,
        "location": c.city,
        "profile_url": f"https://{c.links[0].url}" if c.links else None,
    }


def knockout_answers(spec: DocSpec, job_id: str) -> dict[str, str]:
    labels = next(lab for lab in spec.record.labels if lab.job_id == job_id)
    return dict(labels.knockout_answers)


def upload_name(spec: DocSpec) -> str:
    attack = spec.record.attack
    if attack is not None and attack.filename:
        return attack.filename
    return Path(spec.record.file).name


def parse_spec(spec: DocSpec) -> NormalizedDocument:
    """Parse with the parsers the sandbox runs (in process)."""
    with tempfile.TemporaryDirectory(prefix="pairly-synth-screening-") as tmp:
        path = Path(tmp) / Path(spec.record.file).name
        path.write_bytes(spec.render())
        if spec.record.format == "docx":
            return DocxParser().parse(path, OPTIONS)
        if spec.record.format in ("png", "jpg"):
            return ImageParser().parse(path, OPTIONS)
        return PdfParser().parse(path, OPTIONS)


# ------------------------------------------------------------------ scripted answers


def _line(spec: DocSpec, ref: str) -> str | None:
    lines = spec.text.by_ref(ref)
    return lines[0].text if lines else None


def _answer(path: str, value: Any, quote: str | None) -> dict[str, Any] | None:
    if value is None or quote is None or not str(value).strip():
        return None
    if len(quote) > 240:
        quote = str(value)
    return {
        "path": path,
        "value": str(value),
        "quote": quote,
        "confidence": SCRIPTED_CONFIDENCE,
    }


def scripted_profile(spec: DocSpec) -> dict[str, Any]:
    """``ExtractionOutput`` for the CV parse: the ground truth, quoted line by line."""
    gt = spec.record.ground_truth
    found: list[dict[str, Any] | None] = [
        _answer("contact.name", gt.contact.name, _line(spec, "contact:name")),
        _answer("contact.other_name", gt.contact.name_alt, _line(spec, "contact:name_alt")),
        _answer("contact.email", gt.contact.email, _line(spec, "contact:email")),
        _answer("contact.phone", gt.contact.phone, _line(spec, "contact:phone")),
        _answer("contact.city", gt.contact.city, _line(spec, "contact:city")),
        _answer("contact.country", gt.contact.country, _line(spec, "contact:city")),
    ]
    for i, link in enumerate(gt.contact.links):
        found.append(_answer(f"links[{i}].url", link.url, _line(spec, f"link:{link.kind}")))
    for i, job in enumerate(gt.experience):
        entry, dates = _line(spec, f"exp:{i}"), _line(spec, f"exp:{i}:dates")
        found += [
            _answer(f"experience[{i}].employer", job.employer, entry),
            _answer(f"experience[{i}].title", job.title, entry),
            _answer(f"experience[{i}].start", job.start, dates),
            _answer(f"experience[{i}].end", job.end, dates),
        ]
    for i, edu in enumerate(gt.education):
        line = _line(spec, f"edu:{i}")
        found += [
            _answer(f"education[{i}].institution", edu.institution, line),
            _answer(f"education[{i}].degree", edu.degree, line),
            _answer(f"education[{i}].field", edu.field, line),
            _answer(f"education[{i}].end_year", str(edu.end_year), line),
        ]
    skills_line = _line(spec, "skills")
    for i, skill in enumerate(gt.skills):
        found.append(_answer(f"skills[{i}].name", skill, skills_line))
    for i, cert in enumerate(gt.certifications):
        line = _line(spec, f"cert:{i}")
        found += [
            _answer(f"certifications[{i}].name", cert.name, line),
            _answer(f"certifications[{i}].issuer", cert.issuer, line),
            _answer(f"certifications[{i}].year", str(cert.year), line),
        ]
    languages_line = _line(spec, "langs")
    for i, lang in enumerate(gt.languages):
        found += [
            _answer(f"languages[{i}].language", lang.language, languages_line),
            _answer(f"languages[{i}].level", lang.level, languages_line),
        ]
    return {
        "fields": [f for f in found if f is not None],
        "confidence": SCRIPTED_CONFIDENCE,
    }


_SPLIT = re.compile(r"\s*(?:[,،;|•]|\s-\s|\s\u2013\s)\s*")


def quotable(line: str, blinded_text: str) -> str | None:
    """The longest part of ``line`` a model reading ``blinded_text`` can quote."""
    haystack = normalize(blinded_text).text
    if normalize(line).text and normalize(line).text in haystack:
        return line
    parts = sorted((p.strip() for p in _SPLIT.split(line)), key=len, reverse=True)
    for part in parts:
        needle = normalize(part).text
        if len(needle) >= 4 and needle in haystack:
            return part
    return None


def _why_not_higher(criterion: Criterion, score: int) -> str:
    if score >= len(criterion.scale_anchors) - 1:
        return ""
    target = criterion.scale_anchors[score + 1].rstrip(".")
    return f"The next score needs: {target[0].lower()}{target[1:]}."


def scripted_scores(spec: DocSpec, job: Job, rubric: Rubric, blinded_text: str) -> dict[str, Any]:
    """``CvScoreOutput``: the synthetic label of every criterion, quotes cut to what the
    blinded text shows."""
    labels = next(lab for lab in spec.record.labels if lab.job_id == job.id)
    by_id = {c.id: c for c in rubric.criteria}
    criteria = []
    for label in labels.criteria:
        quotes: list[str] = []
        for line in label.evidence:
            part = quotable(line, blinded_text)
            if part is not None and part not in quotes and len(part) <= 240:
                quotes.append(part)
        criteria.append(
            {
                "criterion_id": label.criterion_id,
                "score": label.score,
                "confidence": SCRIPTED_CONFIDENCE,
                "evidence": quotes[:5],
                "why_not_higher": _why_not_higher(by_id[label.criterion_id], label.score),
            }
        )
    return {"criteria": criteria, "confidence": SCRIPTED_CONFIDENCE}


def scripted_classification(spec: DocSpec, text: str, group_ids: set[str]) -> dict[str, Any]:
    attack = spec.record.attack
    if attack is None or attack.caught_by != "classifier":
        return {"spans": []}
    hits = find_quote(text, attack.payloads[0])
    if len(hits) != 1:
        raise RecordingError(f"{spec.record.id}: the payload is not found once in the text")
    start, end = hits[0].start, hits[0].end
    for seg in segments(text, 12_000):
        if seg.start <= start < seg.end and seg.id in group_ids:
            return {
                "spans": [
                    {
                        "segment": seg.id,
                        "start": start,
                        "end": end,
                        "quote": text[start:end],
                        "category": "ranking_manipulation",
                        "confidence": 0.92,
                    }
                ]
            }
    return {"spans": []}


# ------------------------------------------------------------------ routers


def _registry() -> PromptRegistry:
    return PromptRegistry([HIRE_PROMPTS / "prompts", core_prompt_root()])


@dataclass
class _Keyed:
    prompt: Any
    model: str
    settings: dict[str, Any]
    inputs: dict[str, Any]
    s_hash: str
    r_hash: str


def _key(task: TaskSpec, inputs: Mapping[str, Any], schema: type[BaseModel]) -> _Keyed:
    prompt = _registry().get(task.name, task.prompt_version)
    if prompt.output_schema != schema.__name__:
        raise RecordingError(f"{task.name}: prompt expects {prompt.output_schema}")
    model = load_catalog(load_prices()).model_for(PROVIDER, prompt.tier)
    if model is None:
        raise RecordingError(f"no {PROVIDER} model for tier {prompt.tier}")
    settings = {
        "temperature": prompt.temperature if model.supports_temperature else None,
        "max_output_tokens": prompt.max_output_tokens,
        "effort": model.effort,
        "user_template": USER_TEMPLATE_VERSION,
    }
    clean = json_round_trip(dict(inputs))
    s_hash = schema_hash(schema)
    r_hash = request_hash(
        task=prompt.task,
        prompt_version=prompt.version,
        prompt_hash=prompt.hash,
        provider=PROVIDER,
        model=model.model,
        inputs=clean,
        schema_hash=s_hash,
        settings=settings,
    )
    return _Keyed(prompt, model.model, settings, clean, s_hash, r_hash)


def _result[T: BaseModel](keyed: _Keyed, value: T, started: float) -> RouterResult[T]:
    confidence = getattr(value, "confidence", None)
    return RouterResult(
        value=value,
        call_id=uuid.uuid4(),
        chain_id=uuid.uuid4(),
        task=keyed.prompt.task,
        prompt_version=keyed.prompt.version,
        prompt_hash=keyed.prompt.hash,
        provider=PROVIDER,
        model=keyed.model,
        tier=keyed.prompt.tier,
        request_hash=keyed.r_hash,
        input_tokens=0,
        output_tokens=0,
        cost_micros=0,
        total_cost_micros=0,
        latency_ms=int((time.monotonic() - started) * 1000),
        confidence=confidence if isinstance(confidence, float) else None,
        from_recording=True,
    )


@dataclass
class ScriptedRouter:
    """Answers the screening prompts from the synthetic ground truth and records each
    request and answer as a bundle entry (``entries``)."""

    spec: DocSpec
    job: Job
    rubric: Rubric
    doc_text: str
    entries: list[dict[str, Any]] = field(default_factory=list)

    async def complete[T: BaseModel](
        self,
        *,
        task: TaskSpec,
        inputs: Mapping[str, Any],
        schema: type[T],
        org_id: uuid.UUID,
        context: CallContext | None = None,
    ) -> RouterResult[T]:
        started = time.monotonic()
        keyed = _key(task, inputs, schema)
        if task.name == DOC_CLASSIFY_TASK:
            ids = {str(s["id"]) for s in inputs.get("segments", [])}
            response = scripted_classification(self.spec, self.doc_text, ids)
        elif task.name == "core_extract":
            response = scripted_profile(self.spec)
        elif task.name == "hire_cv_score":
            response = scripted_scores(self.spec, self.job, self.rubric, _blinded(inputs))
        else:  # pragma: no cover - only the screening prompts are scripted
            raise RecordingError(f"no script for {task.name}")
        value = schema.model_validate(response)
        self.entries.append(_entry(keyed, schema, json_round_trip(response)))
        return _result(keyed, value, started)


def _blinded(inputs: Mapping[str, Any]) -> str:
    block = str(inputs.get("document", ""))
    match = re.search(r'<document_data nonce="[0-9a-f]+">\n(.*)\n</document_data', block, re.S)
    return match.group(1) if match else block


def _entry(keyed: _Keyed, schema: type[BaseModel], response: Any) -> dict[str, Any]:
    return {
        "request_hash": keyed.r_hash,
        "task": keyed.prompt.task,
        "prompt_version": keyed.prompt.version,
        "prompt_hash": keyed.prompt.hash,
        "provider": PROVIDER,
        "model": keyed.model,
        "tier": keyed.prompt.tier,
        "schema_name": schema.__name__,
        "schema_hash": keyed.s_hash,
        "request": {"inputs": keyed.inputs, "settings": keyed.settings},
        "response": response,
        "output_complete": True,
        "stop_reason": "end_turn",
        "status": "ok",
    }


@dataclass
class BundleRouter:
    """Replays bundle entries by request hash, like ``ModelRouter`` in ``recorded``
    mode (a missing recording raises ``RecordingMissing``, fail closed)."""

    entries: Mapping[str, dict[str, Any]]
    used: set[str] = field(default_factory=set)

    @classmethod
    def of(cls, entries: Iterable[dict[str, Any]]) -> BundleRouter:
        return cls({e["request_hash"]: e for e in entries})

    async def complete[T: BaseModel](
        self,
        *,
        task: TaskSpec,
        inputs: Mapping[str, Any],
        schema: type[T],
        org_id: uuid.UUID,
        context: CallContext | None = None,
    ) -> RouterResult[T]:
        started = time.monotonic()
        keyed = _key(task, inputs, schema)
        entry = self.entries.get(keyed.r_hash)
        if entry is None:
            raise RecordingMissing(f"no recording for {task.name}")
        self.used.add(keyed.r_hash)
        return _result(keyed, schema.model_validate(entry["response"]), started)


# ------------------------------------------------------------------ one CV through the pipeline


@dataclass
class Case:
    """One CV screened offline with every intermediate result (for evals and tests)."""

    spec: DocSpec
    doc: NormalizedDocument
    report: Any
    guarded: Any
    profile: ParsedProfile | None
    blinded: BlindedInput | None
    scoring: ScoringResult | None
    score_reason: str | None
    flags: list[Flag]
    knockouts: list[KnockoutCheck]
    timings: dict[str, float]
    entries: list[dict[str, Any]]


async def screen(
    spec: DocSpec,
    job: Job,
    *,
    router: Any = None,
    blind_config: BlindConfig | None = None,
    classify: bool = True,
    doc: NormalizedDocument | None = None,
    contact: dict[str, Any] | None = None,
) -> Case:
    """Run the screening pipeline on one synthetic CV, as the graph does. Without a
    ``router`` the scripted router answers (and records the entries)."""
    org = uuid.UUID(int=0)
    timings: dict[str, float] = {}
    clock = time.monotonic()
    if doc is None:
        doc = parse_spec(spec)
    timings["parse"] = time.monotonic() - clock
    rubric = screening_rubric(job)
    # Duck-typed stand-ins for ``ModelRouter`` (only ``complete`` is called).
    active: Any = router or ScriptedRouter(spec, job, rubric, doc.text)
    clock = time.monotonic()
    report = pipeline.scan(doc, upload_name(spec))
    timings["guardrails"] = time.monotonic() - clock
    if classify:
        outcome = await pipeline.classify(report, doc, active, pipeline.HIRE_CLASSIFIER, org_id=org)
        if outcome is not None:
            report = outcome.report
    clock = time.monotonic()
    guarded = pipeline.guard(doc, report)
    timings["sanitize"] = time.monotonic() - clock
    parsed = await pipeline.parse_cv(doc, guarded, active, org_id=org)
    profile = pipeline.profile_of(parsed)
    clock = time.monotonic()
    blinded = pipeline.blind_input(
        guarded,
        contact if contact is not None else contact_for(spec),
        profile,
        blind_config or BlindConfig(),
    )
    timings["blind"] = time.monotonic() - clock
    scored = await pipeline.score_cv(rubric, doc, blinded, active, org_id=org)
    clock = time.monotonic()
    flags = pipeline.claim_flags(
        profile,
        doc,
        guarded,
        as_of=SCREENING_AS_OF,
        jobs_with_same_cv=1,
        config=ClaimConfig(),
    )
    knockouts = check_knockouts(rubric, knockout_answers(spec, job.id))
    timings["checks"] = time.monotonic() - clock
    return Case(
        spec=spec,
        doc=doc,
        report=report,
        guarded=guarded,
        profile=profile,
        blinded=blinded,
        scoring=scored.result if not isinstance(scored, str) else None,
        score_reason=scored if isinstance(scored, str) else None,
        flags=flags,
        knockouts=knockouts,
        timings=timings,
        entries=list(active.entries) if isinstance(active, ScriptedRouter) else [],
    )


def bundle_text(entries: Iterable[dict[str, Any]]) -> str:
    unique = {e["request_hash"]: e for e in entries}
    ordered = sorted(unique.values(), key=lambda e: e["request_hash"])
    text = dumps_bundle({"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "entries": ordered})
    parse_bundle(text)  # fails closed on any entry whose hash does not match
    return text


def build_bundle(specs: list[DocSpec]) -> str:
    by_id = {s.record.id: s for s in specs}
    job = job_by_id(DEMO_JOB)
    entries: list[dict[str, Any]] = []
    for doc_id in DEMO_APPLICATIONS:
        case = asyncio.run(screen(by_id[doc_id], job))
        entries += case.entries
    return bundle_text(entries)


def recordings_text() -> str:
    return BUNDLE_PATH.read_text(encoding="utf-8")
