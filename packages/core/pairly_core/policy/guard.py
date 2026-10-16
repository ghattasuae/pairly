# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Guardrails for tenant policy documents, run before a policy document is indexed.

A policy document becomes the source of cited requirements in policy checks, so text a
reader cannot see, or text written to steer a model, must never reach a chunk:

* ``scan_document`` runs on the normalised document, as for every other document.
* Every span the guardrails remove (hidden text, instruction sentences, invisible
  Unicode) and every span of a flagged finding (medium or high) is cut out of the page
  text and the text blocks before chunking (``excluded_document``). The chunker already
  drops hidden and off-page blocks; this is on top of that.
* The findings are kept as warnings on the policy document, for admins.
* Low and medium findings never block indexing. A high ``instruction`` finding, or a
  guardrail detector that failed (fail closed: an unchecked document is never clean),
  marks the document ``needs_review``: it is not indexed and not active. An admin
  uploads a corrected file (a new version), or overrides with a written reason (two
  admins for high-severity findings, ``pairly_core.policy.documents.request_override``);
  the excluded spans stay excluded after an override.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from pairly_core.documents.model import NormalizedDocument, Page, TextBlock
from pairly_core.guardrails import (
    DetectorRegistry,
    Finding,
    FindingType,
    GuardrailReport,
    ScanContext,
    Severity,
    scan_document,
)
from pairly_core.guardrails.textindex import build_index

MAX_STORED_FINDINGS: Final = 100
MAX_EVIDENCE_CHARS: Final = 300
NEEDS_REVIEW_INSTRUCTION: Final = "guardrails_instruction"
NEEDS_REVIEW_ERROR: Final = "guardrails_error"


@dataclass(frozen=True, slots=True)
class PolicyGuardOutcome:
    report: GuardrailReport
    # The document with every excluded span cut out; what the chunker sees.
    document: NormalizedDocument
    # JSON warnings stored on the policy document (``guardrail_findings``).
    warnings: list[dict[str, Any]]
    # Why the document needs review (it is then not indexed), or None.
    review_reason: str | None


def _excluded(finding: Finding) -> bool:
    return finding.remove or finding.severity.rank >= Severity.MEDIUM.rank


def _blocks_activation(finding: Finding) -> bool:
    return finding.type is FindingType.INSTRUCTION and finding.severity is Severity.HIGH


def warning_of(finding: Finding) -> dict[str, Any]:
    evidence = finding.evidence
    if len(evidence) > MAX_EVIDENCE_CHARS:
        evidence = evidence[: MAX_EVIDENCE_CHARS - 3] + "..."
    return {
        "id": finding.id,
        "type": finding.type.value,
        "code": finding.code,
        "severity": finding.severity.value,
        "location": finding.location,
        "page": finding.page,
        "evidence": evidence,
        "excluded": _excluded(finding),
        "blocks_activation": _blocks_activation(finding),
        "explanation": finding.explain("en"),
        "explanation_ar": finding.explain("ar"),
    }


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _cut(text: str, base: int, cuts: list[tuple[int, int]]) -> str:
    """``text`` (at ``base`` in the document text) without the parts inside ``cuts``.

    A cut is replaced by a newline when it held one (paragraph structure survives),
    otherwise by a space (words on either side never merge).
    """
    out: list[str] = []
    position = 0
    for start, end in cuts:
        lo, hi = max(start - base, 0), min(end - base, len(text))
        if lo >= hi:
            continue
        if lo > position:
            out.append(text[position:lo])
        out.append("\n" if "\n" in text[lo:hi] else " ")
        position = max(position, hi)
    out.append(text[position:])
    return "".join(out)


def excluded_document(doc: NormalizedDocument, report: GuardrailReport) -> NormalizedDocument:
    """``doc`` without any span the guardrails removed or flagged."""
    cuts = _merge([(s.start, s.end) for f in report.findings if _excluded(f) for s in f.spans])
    if not cuts:
        return doc
    snippets = [
        doc.text[start:end].strip() for start, end in cuts if len(doc.text[start:end].strip()) >= 3
    ]
    index = build_index(doc)
    pages: list[Page] = []
    for page, page_index in zip(doc.pages, index.pages, strict=True):
        blocks: list[TextBlock] = []
        for block, span in zip(page.blocks, page_index.blocks, strict=True):
            if span is None:
                # Not placed in the text: drop it if it holds any excluded text.
                if any(s in block.text or block.text.strip() in s for s in snippets):
                    continue
                blocks.append(block)
                continue
            start, end = span
            if any(c_start < end and c_end > start for c_start, c_end in cuts):
                blocks.append(block.model_copy(update={"text": _cut(block.text, start, cuts)}))
            else:
                blocks.append(block)
        pages.append(
            page.model_copy(
                update={"text": _cut(page.text, page_index.base, cuts), "blocks": blocks}
            )
        )
    return doc.model_copy(update={"pages": pages})


def guard_policy_document(
    doc: NormalizedDocument,
    *,
    filename: str | None = None,
    registry: DetectorRegistry | None = None,
) -> PolicyGuardOutcome:
    report = scan_document(
        doc, context=ScanContext(filename=filename, doc_kind="document"), registry=registry
    )
    warnings = [warning_of(f) for f in report.findings[:MAX_STORED_FINDINGS]]
    review_reason: str | None = None
    if report.errors:
        review_reason = NEEDS_REVIEW_ERROR
    elif any(_blocks_activation(f) for f in report.findings):
        review_reason = NEEDS_REVIEW_INSTRUCTION
    return PolicyGuardOutcome(
        report=report,
        document=excluded_document(doc, report),
        warnings=warnings,
        review_reason=review_reason,
    )
