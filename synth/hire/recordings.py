# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Recorded responses for the whole-document injection classifier (offline tests).

The bundle (``synth/hire/recordings/guardrails_document_classify.json``) holds, for a
few synthetic CVs, the exact router request the classifier sends and a SCRIPTED answer:
the span of the planted paraphrased instruction (located in the parsed text, so its
offsets are right by construction) for the paraphrase attack CVs, and no spans for the
clean ones. These are test fixtures, not model output, and give no information about
how well a real model classifies: that needs a live run.

Requests are keyed exactly as the model router keys them (task, prompt version and
hash, provider, model, inputs, schema hash, settings), so a change to the prompt, the
schema, the parsers or the generators makes the bundle stale:
``python -m synth.hire --check`` reports it and ``python -m synth.hire manifests``
rewrites it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Final

from pairly_core.documents.docx import DocxParser
from pairly_core.documents.model import NormalizedDocument
from pairly_core.documents.pdf import PdfParser
from pairly_core.documents.registry import ParseOptions
from pairly_core.guardrails.doc_classifier import (
    DOC_CLASSIFY_TASK,
    DOC_CLASSIFY_VERSION,
    DocumentClassifierConfig,
    DocumentInjectionSpans,
    chunks,
    frame_segments,
    segments,
)
from pairly_core.guardrails.validation import find_quote
from pairly_core.llm.bundle import BUNDLE_FORMAT, BUNDLE_VERSION, dumps_bundle, parse_bundle
from pairly_core.llm.catalog import load_catalog, load_prices
from pairly_core.llm.hashing import json_round_trip, request_hash, schema_hash
from pairly_core.llm.prompt_registry import PromptRegistry, core_prompt_root
from pairly_core.llm.router import USER_TEMPLATE_VERSION
from synth.hire.dataset import DocSpec

PROVIDER: Final = "anthropic"
BUNDLE_PATH: Final = (
    Path(__file__).resolve().parent / "recordings" / "guardrails_document_classify.json"
)
# Paraphrase attacks (one planted span each) and clean controls (no spans).
DOCUMENTS: Final = (
    "atk-paraphrased-en-1-pdf",
    "atk-paraphrased-en-2-docx",
    "atk-paraphrased-en-3-pdf",
    "atk-paraphrased-ar-1-docx",
    "atk-paraphrased-ar-2-docx",
    "cv-0001",
    "cv-0006",
    "cv-0017",
)
OPTIONS: Final = ParseOptions(ocr_languages="eng")


class RecordingError(RuntimeError):
    """A planted span is not where the generator says it is."""


def parse_spec(spec: DocSpec) -> NormalizedDocument:
    with tempfile.TemporaryDirectory(prefix="pairly-synth-hire-") as tmp:
        path = Path(tmp) / Path(spec.record.file).name
        path.write_bytes(spec.render())
        if spec.record.format == "docx":
            return DocxParser().parse(path, OPTIONS)
        if spec.record.format == "pdf":
            return PdfParser().parse(path, OPTIONS)
    raise RecordingError(f"{spec.record.id}: only PDF and DOCX are recorded")


def scripted_answer(
    spec: DocSpec, doc: NormalizedDocument, group_ids: set[str], config: DocumentClassifierConfig
) -> dict[str, Any]:
    """The spans a correct classifier returns for this group of segments."""
    attack = spec.record.attack
    if attack is None or attack.caught_by != "classifier":
        return {"spans": []}
    text = doc.text
    # Whitespace-insensitive: a PDF wraps the planted line over several lines.
    hits = find_quote(text, attack.payloads[0])
    if len(hits) != 1:
        raise RecordingError(f"{spec.record.id}: the payload is not found once in the text")
    start, end = hits[0].start, hits[0].end
    quote = text[start:end]
    for seg in segments(text, config.max_chars_per_call):
        if seg.start <= start < seg.end and seg.id in group_ids:
            return {
                "spans": [
                    {
                        "segment": seg.id,
                        "start": start,
                        "end": end,
                        "quote": quote,
                        "category": "ranking_manipulation",
                        "confidence": 0.92,
                    }
                ]
            }
    return {"spans": []}


def entries_for(spec: DocSpec, config: DocumentClassifierConfig) -> list[dict[str, Any]]:
    doc = parse_spec(spec)
    prompt = PromptRegistry([core_prompt_root()]).get(DOC_CLASSIFY_TASK, DOC_CLASSIFY_VERSION)
    model = load_catalog(load_prices()).model_for(PROVIDER, prompt.tier)
    if model is None:
        raise RecordingError(f"no {PROVIDER} model for tier {prompt.tier}")
    settings = {
        "temperature": prompt.temperature if model.supports_temperature else None,
        "max_output_tokens": prompt.max_output_tokens,
        "effort": model.effort,
        "user_template": USER_TEMPLATE_VERSION,
    }
    s_hash = schema_hash(DocumentInjectionSpans)
    out: list[dict[str, Any]] = []
    groups = chunks(segments(doc.text, config.max_chars_per_call), config.max_chars_per_call)
    for group in groups[: config.max_calls]:
        inputs = json_round_trip(frame_segments(group))
        response = scripted_answer(spec, doc, {s.id for s in group}, config)
        DocumentInjectionSpans.model_validate(response)
        out.append(
            {
                "request_hash": request_hash(
                    task=prompt.task,
                    prompt_version=prompt.version,
                    prompt_hash=prompt.hash,
                    provider=PROVIDER,
                    model=model.model,
                    inputs=inputs,
                    schema_hash=s_hash,
                    settings=settings,
                ),
                "task": prompt.task,
                "prompt_version": prompt.version,
                "prompt_hash": prompt.hash,
                "provider": PROVIDER,
                "model": model.model,
                "tier": prompt.tier,
                "schema_name": DocumentInjectionSpans.__name__,
                "schema_hash": s_hash,
                "request": {"inputs": inputs, "settings": settings},
                "response": json_round_trip(response),
                "output_complete": True,
                "stop_reason": "end_turn",
                "status": "ok",
            }
        )
    return out


def build_bundle(specs: list[DocSpec]) -> str:
    config = DocumentClassifierConfig()
    by_id = {s.record.id: s for s in specs}
    entries: list[dict[str, Any]] = []
    for doc_id in DOCUMENTS:
        entries.extend(entries_for(by_id[doc_id], config))
    entries.sort(key=lambda e: e["request_hash"])
    text = dumps_bundle({"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "entries": entries})
    parse_bundle(text)  # fails closed on any entry whose hash does not match
    return text
