# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Guardrails (WP1.4): run on every document before any model sees its content.

    report = scan_document(doc, context=ScanContext(filename=..., doc_kind="cv"))
    model_input = sanitize_for_model(doc, report)       # the only text a model may see
    router.complete(..., inputs={**model_input.as_inputs(), ...})
    if report.needs_review: route to a person, showing finding.explain(locale)

* ``scan``        ``scan_document``: deterministic detectors, never a model.
* ``detectors``   hidden text, instructions (en, ar), keyword stuffing, metadata,
                  unusual Unicode; a pluggable ``DetectorRegistry``.
* ``sanitize``    ``sanitize_for_model``: removes flagged spans, wraps the text in a
                  nonce-delimited data block, maps offsets back to the original.
* ``classifier``  optional second stage on suspicious spans (fail closed). Imported
                  on its own: it depends on the model router.
* ``pii``         ``PiiRedactor`` for logs, traces and LangFuse; ``redact_log_event``.
* ``validation``  output schema, range and banned-content checks; ``quote_exists``.
* ``explain``     reviewer sentences in English and Arabic.
"""

from __future__ import annotations

from pairly_core.guardrails.base import (
    Detector,
    DetectorRegistry,
    GuardrailConfig,
    ScanContext,
    ScanInput,
)
from pairly_core.guardrails.detectors import default_registry
from pairly_core.guardrails.pii import PiiRedactor, redact_log_event
from pairly_core.guardrails.report import (
    Finding,
    FindingType,
    GuardrailReport,
    Severity,
    Span,
)
from pairly_core.guardrails.sanitize import ModelInput, OffsetMap, sanitize_for_model
from pairly_core.guardrails.scan import scan_document
from pairly_core.guardrails.validation import (
    OutputCheck,
    locate_quote,
    quote_exists,
    validate_output,
)

__all__ = [
    "Detector",
    "DetectorRegistry",
    "Finding",
    "FindingType",
    "GuardrailConfig",
    "GuardrailReport",
    "ModelInput",
    "OffsetMap",
    "OutputCheck",
    "PiiRedactor",
    "ScanContext",
    "ScanInput",
    "Severity",
    "Span",
    "default_registry",
    "locate_quote",
    "quote_exists",
    "redact_log_event",
    "sanitize_for_model",
    "scan_document",
    "validate_output",
]
