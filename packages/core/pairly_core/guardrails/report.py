# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Guardrail findings and the report ``scan_document`` returns.

Spans are half-open ``[start, end)`` character offsets into ``NormalizedDocument.text``
(pages joined by a blank line), the same text a reviewer's document viewer shows.

Severity:

* ``high``    clear manipulation (hidden text, a direct instruction to the model).
* ``medium``  likely manipulation; the document is flagged for a person.
* ``low``     information only (a mention, a stray control character). Not flagged.

A report is ``flagged`` when any finding is medium or high, and ``needs_review`` when
it is flagged or a detector failed (fail closed: an unchecked document is never clean).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pairly_core.documents.model import BBox
from pairly_core.guardrails.explain import DocKind, Locale, explain

GUARDRAILS_VERSION = "1"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 1, "medium": 2, "high": 3}[self.value]


class FindingType(StrEnum):
    HIDDEN_TEXT = "hidden_text"
    INSTRUCTION = "instruction"
    KEYWORD_STUFFING = "keyword_stuffing"
    METADATA_INJECTION = "metadata_injection"
    UNUSUAL_UNICODE = "unusual_unicode"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Span(_Model):
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class ClassifierVerdict(_Model):
    """What the optional second-stage classifier said about a finding's text."""

    status: Literal["injection", "benign", "error"]
    confidence: float | None = None
    model: str | None = None
    call_id: str | None = None


DetailValue = str | int | float | bool


class Finding(_Model):
    id: str
    type: FindingType
    code: str
    severity: Severity
    detector: str
    # Exact text that triggered the finding (truncated), for the reviewer.
    evidence: str
    # "body", "header", "footer", "textbox", "table", "metadata:<key>", "filename",
    # "email:subject", "email:header:<name>", "unicode_tags".
    location: str = "body"
    page: int | None = None
    bbox: BBox | None = None
    spans: tuple[Span, ...] = ()
    # Remove these spans from any text a model sees.
    remove: bool = False
    score: float | None = None
    details: Mapping[str, DetailValue] = Field(default_factory=dict)
    classifier: ClassifierVerdict | None = None

    @property
    def explanation_key(self) -> str:
        return f"guardrails.{self.type.value}.{self.code}"

    def explain(self, locale: Locale = "en", doc_kind: DocKind = "document") -> str:
        """One plain-language sentence for the reviewer, in English or Arabic."""
        return explain(self.type.value, self.code, self.evidence, locale, doc_kind)


class GuardrailReport(_Model):
    version: str = GUARDRAILS_VERSION
    findings: tuple[Finding, ...] = ()
    # Detectors that raised; any error means the document needs a person.
    errors: tuple[str, ...] = ()
    # Binds the report to the exact text it was computed on.
    text_sha256: str
    text_length: int

    @property
    def flagged(self) -> bool:
        return any(f.severity.rank >= Severity.MEDIUM.rank for f in self.findings)

    @property
    def needs_review(self) -> bool:
        return self.flagged or bool(self.errors)

    @property
    def max_severity(self) -> Severity | None:
        if not self.findings:
            return None
        return max((f.severity for f in self.findings), key=lambda s: s.rank)

    def of_type(self, kind: FindingType) -> list[Finding]:
        return [f for f in self.findings if f.type is kind]

    def flagged_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity.rank >= Severity.MEDIUM.rank]


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()
