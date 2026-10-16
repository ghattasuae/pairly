# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Run the guardrails over the synthetic attack corpus and measure them.

    uv run python -m synth.attacks            # prints a JSON report

Document level: an attack counts as detected when the report is flagged (a medium or
high finding); a clean control counts as a false positive when it is flagged.
Type level: an attack is fully detected when every expected finding type is flagged.
Leak check: no payload string may survive ``sanitize_for_model``. Samples marked
``known_gap`` are attacks the guardrails are known to miss; they count in recall.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pairly_core.documents.docx import DocxParser
from pairly_core.documents.model import NormalizedDocument
from pairly_core.documents.pdf import PdfParser
from pairly_core.documents.registry import ParseOptions
from pairly_core.documents.text import TextParser
from pairly_core.guardrails import (
    GuardrailReport,
    ScanContext,
    sanitize_for_model,
    scan_document,
)
from synth.attacks.corpus import Sample, build_corpus

OPTIONS = ParseOptions(ocr_languages="eng")


def parse_sample(sample: Sample, workdir: Path) -> NormalizedDocument:
    path = workdir / f"{sample.name}{sample.suffix}"
    path.write_bytes(sample.data)
    if sample.kind == "pdf":
        return PdfParser().parse(path, OPTIONS)
    if sample.kind == "docx":
        return DocxParser().parse(path, OPTIONS)
    return TextParser().parse(path, OPTIONS)


def scan_sample(sample: Sample, doc: NormalizedDocument) -> GuardrailReport:
    context = ScanContext(
        filename=sample.filename,
        email_subject=sample.email_subject,
        email_headers=sample.email_headers,
        doc_kind=sample.doc_kind,
    )
    return scan_document(doc, context=context)


@dataclass
class SampleResult:
    name: str
    attack: bool
    flagged: bool
    expected: list[str]
    flagged_types: list[str]
    missing_types: list[str]
    leaked: list[str]
    known_gap: bool = False


@dataclass
class CorpusResult:
    samples: list[SampleResult] = field(default_factory=list)

    @property
    def attacks(self) -> list[SampleResult]:
        return [s for s in self.samples if s.attack]

    @property
    def clean(self) -> list[SampleResult]:
        return [s for s in self.samples if not s.attack]

    def metrics(self) -> dict[str, float | int]:
        tp = sum(1 for s in self.attacks if s.flagged)
        fn = len(self.attacks) - tp
        fp = sum(1 for s in self.clean if s.flagged)
        tn = len(self.clean) - fp
        full = sum(1 for s in self.attacks if s.flagged and not s.missing_types)
        covered = [s for s in self.attacks if not s.known_gap]
        covered_tp = sum(1 for s in covered if s.flagged)
        return {
            "attacks": len(self.attacks),
            "clean": len(self.clean),
            "true_positives": tp,
            "false_negatives": fn,
            "false_positives": fp,
            "true_negatives": tn,
            "precision": round(tp / (tp + fp), 4) if tp + fp else 0.0,
            "recall": round(tp / (tp + fn), 4) if tp + fn else 0.0,
            "false_positive_rate": round(fp / (fp + tn), 4) if fp + tn else 0.0,
            "type_level_recall": round(full / len(self.attacks), 4) if self.attacks else 0.0,
            "known_gaps": len(self.attacks) - len(covered),
            "recall_excluding_known_gaps": (
                round(covered_tp / len(covered), 4) if covered else 0.0
            ),
            "leaks": sum(len(s.leaked) for s in self.samples),
        }


def run_corpus(samples: list[Sample] | None = None) -> CorpusResult:
    result = CorpusResult()
    with tempfile.TemporaryDirectory(prefix="pairly-attacks-") as tmp:
        for sample in samples if samples is not None else build_corpus():
            doc = parse_sample(sample, Path(tmp))
            report = scan_sample(sample, doc)
            flagged_types = sorted({f.type.value for f in report.flagged_findings()})
            sanitized = sanitize_for_model(doc, report).text
            result.samples.append(
                SampleResult(
                    name=sample.name,
                    attack=sample.attack,
                    flagged=report.flagged,
                    expected=sorted(sample.expected),
                    flagged_types=flagged_types,
                    missing_types=sorted(sample.expected - set(flagged_types)),
                    leaked=[p for p in sample.payloads if p in sanitized],
                    known_gap=sample.known_gap,
                )
            )
    return result
