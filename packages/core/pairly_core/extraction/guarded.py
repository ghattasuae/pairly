# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``GuardedInput``: the only document input ``extract`` accepts.

It is built from a document and the ``GuardrailReport`` of that same document
(``scan_document``), through the guardrails' ``sanitize_for_model``: every span the
guardrails removed (hidden text, instruction sentences, invisible Unicode) is absent
from ``text``, and evidence is mapped back to the original document through the
guardrails' offset map.

``to_original`` returns one contiguous original range, as the evidence locator
requires. A sanitised range that maps to several original pieces is accepted only when
nothing between the pieces was removed by a finding (the gaps are invisible formatting
characters the sanitiser always strips); the locator then still checks that the
original text matches the quote. A range that straddles removed content maps to
nothing, so it can never become evidence.

Instances can only be made with ``GuardedInput.build``. ``check(doc)`` refuses an input
built for another document (``UnguardedInputError``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

from pairly_core.documents.model import NormalizedDocument
from pairly_core.guardrails.report import GuardrailReport, text_digest
from pairly_core.guardrails.sanitize import ModelInput, sanitize_for_model

_BUILD_TOKEN = object()


class UnguardedInputError(ValueError):
    """Extraction was given input that is not the guardrails' output for this document."""


@dataclass(frozen=True, slots=True, eq=False)
class GuardedInput:
    """Sanitised model input bound to one document and its guardrail report."""

    model_input: ModelInput
    report: GuardrailReport
    document_sha256: str
    document_length: int
    _token: object = field(repr=False)

    def __post_init__(self) -> None:
        if self._token is not _BUILD_TOKEN:
            raise TypeError("use GuardedInput.build(doc, report)")

    @classmethod
    def build(cls, doc: NormalizedDocument, report: GuardrailReport) -> GuardedInput:
        """Sanitise ``doc`` with its ``report``. Raises ``ReportMismatchError`` (a
        ``ValueError``) when the report was computed on another text."""
        model_input = sanitize_for_model(doc, report)
        return cls(
            model_input=model_input,
            report=report,
            document_sha256=text_digest(doc.text),
            document_length=len(doc.text),
            _token=_BUILD_TOKEN,
        )

    # ------------------------------------------------------------------ SanitizedInput

    @property
    def text(self) -> str:
        return self.model_input.text

    @property
    def data_block(self) -> str:
        """The text wrapped in the nonce-delimited data block sent to the model."""
        return self.model_input.data_block

    def to_original(self, start: int, end: int) -> tuple[int, int] | None:
        try:
            pieces = self.model_input.to_original(start, end)
        except IndexError:
            return None
        if not pieces:
            return None
        for before, after in pairwise(pieces):
            if self._removed_between(before.end, after.start):
                return None
        return pieces[0].start, pieces[-1].end

    def _removed_between(self, lo: int, hi: int) -> bool:
        return any(span.start < hi and span.end > lo for span in self.model_input.removed)

    # ------------------------------------------------------------------ binding

    def check(self, doc: NormalizedDocument) -> None:
        """Raise ``UnguardedInputError`` unless this input was built for ``doc``."""
        digest = text_digest(doc.text)
        if (
            self.document_sha256 != digest
            or self.document_length != len(doc.text)
            or self.report.text_sha256 != digest
            or self.report.text_length != len(doc.text)
            or self.model_input.report_sha256 != digest
            or self.model_input.offsets.original_length != len(doc.text)
        ):
            raise UnguardedInputError("the guarded input belongs to another document")

    @property
    def review_reasons(self) -> tuple[str, ...]:
        """Why the guardrails want a person to look at the document (empty when clean)."""
        reasons: list[str] = []
        flagged = [f.id for f in self.report.flagged_findings()]
        if flagged:
            reasons.append("guardrails_flagged:" + ",".join(flagged[:20]))
        if self.report.errors:
            reasons.append("guardrails_error")
        return tuple(reasons)
