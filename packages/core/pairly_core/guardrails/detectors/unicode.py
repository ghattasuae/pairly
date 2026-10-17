# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Unusual Unicode: characters that change what text looks like or carry hidden data.

* Bidi overrides (U+202D, U+202E) reverse how text is displayed: high. Embeddings and
  isolates are common in mixed Arabic and English text: low.
* Zero-width characters (U+200B, U+2060, U+FEFF inside text, ...) inside or between
  Latin words split keywords and instructions so filters miss them: medium. ZWJ and
  ZWNJ are only reported next to Latin letters: one between two Latin letters, or a
  run of two or more (mixed with ZWSP or word joiners) touching a Latin letter, as in
  padded keywords ("Python<ZWJ><ZWJ> AWS"). A ZWNJ inside an Arabic or Persian word,
  or a ZWJ in an emoji sequence, is ordinary text and is left alone.
* Tag characters (U+E0000 to U+E007F) are invisible and can spell out a whole hidden
  ASCII message: high. The message is decoded and checked for instructions.
* Long runs of variation selectors can smuggle bytes: high from 8 in a row.
* Words that mix Latin with Cyrillic or Greek look-alike letters: medium.

Every invisible character found here is removed from model input; the visible
look-alike words are not (they are reported).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator

from pairly_core.guardrails.base import ScanInput, evidence
from pairly_core.guardrails.detectors.instructions import find_instructions, strongest
from pairly_core.guardrails.report import Finding, FindingType, Severity, Span
from pairly_core.guardrails.textnorm import (
    BIDI_EMBEDDINGS,
    BIDI_ISOLATES,
    BIDI_OVERRIDES,
    JOINERS,
    TAG_RANGE,
    VARIATION_SELECTORS,
    ZERO_WIDTH,
)

_WORD = re.compile(r"\w+")
_LATIN_ADJACENT = re.compile(r"[A-Za-z]")


def _script(ch: str) -> str | None:
    if not ch.isalpha():
        return None
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return None
    for script in ("LATIN", "CYRILLIC", "GREEK"):
        if name.startswith(script):
            return script
    return "OTHER"


def _runs(text: str, members: frozenset[int] | range) -> Iterator[tuple[int, int]]:
    i, n = 0, len(text)
    while i < n:
        if ord(text[i]) in members:
            j = i
            while j < n and ord(text[j]) in members:
                j += 1
            yield i, j
            i = j
        else:
            i += 1


def _visible(text: str) -> str:
    """Invisible characters spelled out (``<U+202E>``), line breaks as spaces."""
    return "".join(
        " " if c in "\n\r\t" else (f"<U+{ord(c):04X}>" if not c.isprintable() else c) for c in text
    )


class UnicodeDetector:
    name = "unicode"

    def _finding(
        self,
        scan: ScanInput,
        code: str,
        severity: Severity,
        start: int,
        end: int,
        shown: str,
        *,
        remove: bool = True,
        **details: str | int | float | bool,
    ) -> Finding:
        return Finding(
            id="",
            type=FindingType.UNUSUAL_UNICODE,
            code=code,
            severity=severity,
            detector=self.name,
            evidence=evidence(shown),
            page=scan.index.page_of(start),
            spans=(Span(start=start, end=end),),
            remove=remove,
            details=details,
        )

    def _context(self, text: str, start: int, end: int) -> str:
        return _visible(text[max(0, start - 20) : min(len(text), end + 20)])

    def detect(self, scan: ScanInput) -> Iterable[Finding]:
        text = scan.text
        # Runs of two or more mixed zero-width characters attached to a Latin word.
        padded: list[tuple[int, int]] = []
        for start, end in _runs(text, ZERO_WIDTH | JOINERS):
            if end - start < 2:
                continue
            before = text[start - 1] if start > 0 else ""
            after = text[end] if end < len(text) else ""
            if _LATIN_ADJACENT.fullmatch(before) or _LATIN_ADJACENT.fullmatch(after):
                padded.append((start, end))
                yield self._finding(
                    scan,
                    "zero_width",
                    Severity.MEDIUM,
                    start,
                    end,
                    self._context(text, start, end),
                    count=end - start,
                )

        def handled(start: int, end: int) -> bool:
            return any(s <= start and end <= e for s, e in padded)

        for start, end in _runs(text, BIDI_OVERRIDES):
            yield self._finding(
                scan, "bidi_override", Severity.HIGH, start, end, self._context(text, start, end)
            )
        for start, end in _runs(text, BIDI_EMBEDDINGS | BIDI_ISOLATES):
            yield self._finding(
                scan, "bidi_control", Severity.LOW, start, end, self._context(text, start, end)
            )
        for start, end in _runs(text, ZERO_WIDTH):
            if handled(start, end):
                continue
            if start == 0 and text[0] == "\ufeff" and end == 1:
                continue  # a byte order mark at the very start is harmless
            inside = (
                start > 0
                and end < len(text)
                and not text[start - 1].isspace()
                and not text[end].isspace()
            )
            yield self._finding(
                scan,
                "zero_width",
                Severity.MEDIUM if inside else Severity.LOW,
                start,
                end,
                self._context(text, start, end),
            )
        for start, end in _runs(text, JOINERS):
            if handled(start, end):
                continue
            if (
                start > 0
                and end < len(text)
                and _LATIN_ADJACENT.fullmatch(text[start - 1])
                and _LATIN_ADJACENT.fullmatch(text[end])
            ):
                yield self._finding(
                    scan, "zero_width", Severity.MEDIUM, start, end, self._context(text, start, end)
                )
        for start, end in _runs(text, TAG_RANGE):
            decoded = "".join(
                chr(ord(c) - 0xE0000) for c in text[start:end] if 0xE0020 <= ord(c) <= 0xE007E
            )
            yield self._finding(
                scan,
                "tag_characters",
                Severity.HIGH,
                start,
                end,
                f"hidden message: {decoded}" if decoded else self._context(text, start, end),
                decoded_chars=len(decoded),
            )
            best = strongest(find_instructions(decoded)) if decoded else None
            if best is not None and best.severity.rank >= Severity.MEDIUM.rank:
                yield Finding(
                    id="",
                    type=FindingType.INSTRUCTION,
                    code="hidden_message",
                    severity=Severity.HIGH,
                    detector=self.name,
                    evidence=evidence(decoded),
                    location="unicode_tags",
                    page=scan.index.page_of(start),
                    spans=(Span(start=start, end=end),),
                    remove=True,
                    details={"codes": best.code},
                )
        for start, end in _runs(text, VARIATION_SELECTORS):
            if end - start >= 8:
                yield self._finding(
                    scan,
                    "variation_selectors",
                    Severity.HIGH,
                    start,
                    end,
                    self._context(text, start, end),
                    count=end - start,
                )
        for match in _WORD.finditer(text):
            scripts = {_script(c) for c in match.group()} - {None, "OTHER"}
            if "LATIN" in scripts and len(scripts) > 1:
                yield self._finding(
                    scan,
                    "homoglyph",
                    Severity.MEDIUM,
                    match.start(),
                    match.end(),
                    match.group(),
                    remove=False,
                    scripts=",".join(sorted(s for s in scripts if s)),
                )
