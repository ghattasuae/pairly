# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``scan_document``: the deterministic guardrail pass every document goes through
before any model sees its content.

It runs every registered detector on the normalised document (and its envelope: file
name, email subject and headers) and returns a ``GuardrailReport``. It never calls a
model. A detector that raises is recorded in ``report.errors`` and the report then
``needs_review`` (fail closed); the other detectors still run.
"""

from __future__ import annotations

import structlog

from pairly_core.documents.model import NormalizedDocument
from pairly_core.guardrails.base import DetectorRegistry, GuardrailConfig, ScanContext, ScanInput
from pairly_core.guardrails.detectors import default_registry
from pairly_core.guardrails.report import Finding, GuardrailReport, text_digest
from pairly_core.guardrails.textindex import build_index

log = structlog.get_logger("pairly_core.guardrails")

_DEFAULT_REGISTRY: DetectorRegistry | None = None


def _default() -> DetectorRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = default_registry()
    return _DEFAULT_REGISTRY


def _order(finding: Finding) -> tuple[int, int, str, str]:
    start = finding.spans[0].start if finding.spans else -1
    return (start, -finding.severity.rank, finding.type.value, finding.code)


def scan_document(
    doc: NormalizedDocument,
    *,
    context: ScanContext | None = None,
    config: GuardrailConfig | None = None,
    registry: DetectorRegistry | None = None,
) -> GuardrailReport:
    text = doc.text
    scan = ScanInput(
        doc=doc,
        text=text,
        index=build_index(doc),
        config=config or GuardrailConfig(),
        context=context or ScanContext(),
    )
    findings: list[Finding] = []
    errors: list[str] = []
    for detector in registry or _default():
        try:
            findings.extend(detector.detect(scan))
        except Exception as exc:
            errors.append(f"{detector.name}: {type(exc).__name__}")
            log.error(
                "guardrails.detector_failed", detector=detector.name, error=type(exc).__name__
            )
    for finding in findings:
        for span in finding.spans:
            if not 0 <= span.start <= span.end <= len(text):
                errors.append(f"{finding.detector}: span out of range")
    findings.sort(key=_order)
    numbered = tuple(f.model_copy(update={"id": f"g{i + 1}"}) for i, f in enumerate(findings))
    return GuardrailReport(
        findings=numbered,
        errors=tuple(dict.fromkeys(errors)),
        text_sha256=text_digest(text),
        text_length=len(text),
    )
