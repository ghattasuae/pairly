# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Instruction-like content in the document text, its metadata and its envelope.

``InstructionDetector`` matches the English and Arabic patterns of
``pairly_core.guardrails.patterns`` on the folded document text (so zero-width
characters, look-alike letters and Arabic diacritics do not hide anything) and reports
the whole sentence around each match, which is what gets removed from model input.

``MetadataDetector`` runs the same patterns on PDF Info and XMP, DOCX core, app and
custom properties, the file name and the email subject and headers. None of these
reach a model; an instruction there is a sign of manipulation all the same. Long free
text in file properties is reported as information.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from pairly_core.guardrails.base import ScanInput, evidence, sentence_bounds
from pairly_core.guardrails.patterns import PATTERNS, InstructionPattern
from pairly_core.guardrails.report import Finding, FindingType, Severity, Span
from pairly_core.guardrails.textnorm import BIDI_OVERRIDES, TAG_RANGE, fold_for_matching


@dataclass(frozen=True, slots=True)
class Hit:
    code: str
    severity: Severity
    start: int
    end: int
    lang: str


def find_instructions(text: str, patterns: Iterable[InstructionPattern] = PATTERNS) -> list[Hit]:
    """Every pattern match in ``text``, as spans of the original (unfolded) text."""
    folded = fold_for_matching(text)
    hits: list[Hit] = []
    for pattern in patterns:
        for match in pattern.regex.finditer(folded.text):
            if match.end() <= match.start():
                continue
            start, end = folded.span(match.start(), match.end())
            hits.append(Hit(pattern.code, pattern.severity, start, end, pattern.lang))
    hits.sort(key=lambda h: (h.start, -h.severity.rank))
    return hits


def strongest(hits: Iterable[Hit]) -> Hit | None:
    best: Hit | None = None
    for hit in hits:
        if best is None or hit.severity.rank > best.severity.rank:
            best = hit
    return best


class InstructionDetector:
    name = "instruction"

    def detect(self, scan: ScanInput) -> Iterable[Finding]:
        text = scan.text
        groups: list[tuple[int, int, list[Hit]]] = []
        for hit in find_instructions(text):
            start, end = sentence_bounds(text, hit.start, hit.end)
            if groups and start < groups[-1][1]:
                prev_start, prev_end, members = groups[-1]
                groups[-1] = (min(prev_start, start), max(prev_end, end), [*members, hit])
            else:
                groups.append((start, end, [hit]))
        for start, end, members in groups:
            best = strongest(members)
            assert best is not None  # noqa: S101  (groups are never empty)
            codes = sorted({h.code for h in members})
            yield Finding(
                id="",
                type=FindingType.INSTRUCTION,
                code=best.code,
                severity=best.severity,
                detector=self.name,
                evidence=evidence(text[start:end]),
                page=scan.index.page_of(start),
                spans=(Span(start=start, end=end),),
                remove=best.severity.rank >= Severity.MEDIUM.rank,
                details={
                    "codes": ",".join(codes),
                    "languages": ",".join(sorted({h.lang for h in members})),
                    "matches": len(members),
                },
            )


_XML_TAG = re.compile(r"<[^>]{0,2000}>")
# File properties that hold free text (as opposed to dates, counts and tool names).
_FREE_TEXT_KEYS = re.compile(
    r"^(?:pdf:(?:title|subject|keywords|author|creator)|docx:(?:title|subject|keywords|"
    r"comments|category|description|content_status|author|last_modified_by)|"
    r"docx:custom:.*|xmp)$",
    re.IGNORECASE,
)


def metadata_text(key: str, value: str) -> str:
    """The human-readable text of a metadata value (XMP loses its XML tags)."""
    if key == "xmp":
        return " ".join(_XML_TAG.sub(" ", value).split())
    return value


def _instruction_findings(
    detector: str, value: str, location: str, code: str, floor: Severity
) -> Iterator[Finding]:
    hits = find_instructions(value)
    best = strongest(hits)
    if best is None or best.severity.rank < Severity.MEDIUM.rank:
        return
    severity = best.severity if best.severity.rank >= floor.rank else floor
    start, end = sentence_bounds(value, best.start, best.end)
    yield Finding(
        id="",
        type=FindingType.METADATA_INJECTION,
        code=code,
        severity=severity,
        detector=detector,
        evidence=evidence(value[start:end]),
        location=location,
        details={"codes": ",".join(sorted({h.code for h in hits}))},
    )


def _disguised(detector: str, value: str, location: str) -> Iterator[Finding]:
    """Direction overrides or tag characters in a name or property (RLO file names)."""
    codes = {ord(c) for c in value}
    if codes & BIDI_OVERRIDES:
        code = "bidi_override"
    elif any(c in TAG_RANGE for c in codes):
        code = "tag_characters"
    else:
        return
    shown = "".join(f"<U+{ord(c):04X}>" if not c.isprintable() else c for c in value)
    yield Finding(
        id="",
        type=FindingType.UNUSUAL_UNICODE,
        code=code,
        severity=Severity.HIGH,
        detector=detector,
        evidence=evidence(shown),
        location=location,
    )


class MetadataDetector:
    name = "metadata"

    def detect(self, scan: ScanInput) -> Iterable[Finding]:
        cfg = scan.config
        for key, raw in sorted(scan.doc.metadata.items()):
            value = metadata_text(key, raw)
            if not value.strip():
                continue
            location = f"metadata:{key}"
            yield from _disguised(self.name, value, location)
            found = list(
                _instruction_findings(
                    self.name, value, location, "metadata_instruction", Severity.MEDIUM
                )
            )
            yield from found
            if (
                not found
                and _FREE_TEXT_KEYS.match(key)
                and len(value.strip()) > cfg.metadata_text_chars
            ):
                yield Finding(
                    id="",
                    type=FindingType.METADATA_INJECTION,
                    code="metadata_text",
                    severity=Severity.LOW,
                    detector=self.name,
                    evidence=evidence(value.strip()),
                    location=location,
                    details={"chars": len(value.strip())},
                )
        ctx = scan.context
        if ctx.filename:
            yield from _disguised(self.name, ctx.filename, "filename")
            # File names use separators instead of spaces: read them as words.
            words = " ".join(re.split(r"[\s_.+-]+", ctx.filename))
            yield from _instruction_findings(
                self.name, words, "filename", "filename_instruction", Severity.MEDIUM
            )
        if ctx.email_subject:
            yield from _disguised(self.name, ctx.email_subject, "email:subject")
            yield from _instruction_findings(
                self.name, ctx.email_subject, "email:subject", "email_instruction", Severity.MEDIUM
            )
        for header, value in sorted(ctx.email_headers.items()):
            location = f"email:header:{header[:64]}"
            yield from _disguised(self.name, value, location)
            yield from _instruction_findings(
                self.name, value, location, "email_instruction", Severity.MEDIUM
            )
