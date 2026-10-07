# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Evidence: find a model's quote in the document, deterministically.

The model only ever sees the sanitized text (guardrails remove hidden and injected
content first). A quote counts as evidence only when it is found in that sanitized text
and maps back, through the sanitizer's offset map, to a contiguous range of the original
document whose text matches the quote. A quote that exists only in content the
guardrails removed is therefore no evidence, and no value is ever given evidence that
the document does not contain.

Matching is normalised (Unicode NFKC, Arabic-Indic digits to ASCII, case folded,
typographic quotes and dashes to ASCII, runs of whitespace to one space, zero-width
characters dropped); the spans returned always point at the exact original text.

``SanitizedInput`` is the contract with the guardrails (WP1.4): ``text`` is what the
model sees, ``to_original(start, end)`` maps a half-open range of it to the original
document text (``NormalizedDocument.text``), or ``None`` when the range does not map to
one contiguous original range. The production implementation is
``pairly_core.extraction.guarded.GuardedInput`` (the guardrails' sanitised text and
offset map); ``extract`` accepts nothing else. There is deliberately no pass-through
implementation here: an identity map exists only in the tests, for the locator's own
unit tests.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pairly_core.documents.model import BBox, NormalizedDocument, Page
from pairly_core.extraction.types import SourceSpan
from pairly_core.extraction.validators import ascii_digits

PAGE_SEPARATOR = "\n\n"  # NormalizedDocument.text joins pages with it
MAX_QUOTE_CHARS = 240

_PUNCT = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
    }
)
_ZERO_WIDTH = frozenset({"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad"})


@runtime_checkable
class SanitizedInput(Protocol):
    """What the model sees, with a way back to the original document."""

    @property
    def text(self) -> str: ...

    def to_original(self, start: int, end: int) -> tuple[int, int] | None: ...


@dataclass(frozen=True, slots=True)
class Normalized:
    """A normalised string and, for each of its characters, the source index."""

    text: str
    index: tuple[int, ...]


def normalize(text: str) -> Normalized:
    out: list[str] = []
    index: list[int] = []
    pending_space = False
    for i, ch in enumerate(text):
        if ch in _ZERO_WIDTH:
            continue
        if ch.isspace():
            pending_space = bool(out)
            continue
        piece = ascii_digits(unicodedata.normalize("NFKC", ch)).translate(_PUNCT).casefold()
        for sub in piece:
            if sub.isspace():
                pending_space = bool(out)
                continue
            if pending_space:
                out.append(" ")
                index.append(i)
                pending_space = False
            out.append(sub)
            index.append(i)
    return Normalized("".join(out), tuple(index))


def find_normalized(haystack: Normalized, needle: str) -> tuple[int, int] | None:
    """First raw ``[start, end)`` range of ``haystack``'s source matching ``needle``."""
    target = normalize(needle).text
    if not target:
        return None
    pos = haystack.text.find(target)
    if pos < 0:
        return None
    return haystack.index[pos], haystack.index[pos + len(target) - 1] + 1


@dataclass(frozen=True, slots=True)
class Located:
    start: int
    end: int
    spans: tuple[SourceSpan, ...]


class EvidenceLocator:
    """Locates quotes for one document and its sanitized input."""

    def __init__(self, doc: NormalizedDocument, sanitized: SanitizedInput) -> None:
        self._doc = doc
        self._original = doc.text
        self._sanitized = sanitized
        self._norm = normalize(sanitized.text)
        starts: list[int] = []
        offset = 0
        for page in doc.pages:
            starts.append(offset)
            offset += len(page.text) + len(PAGE_SEPARATOR)
        self._page_starts = starts
        self._boxes: dict[int, list[tuple[int, int, BBox]]] = {}

    def locate(self, quote: str | None) -> Located | None:
        if quote is None or not quote.strip() or len(quote) > MAX_QUOTE_CHARS:
            return None
        found = find_normalized(self._norm, quote)
        if found is None:
            return None
        mapped = self._sanitized.to_original(*found)
        if mapped is None:
            return None
        start, end = mapped
        if not 0 <= start < end <= len(self._original):
            return None
        # The offset map must agree with the text: never trust it blindly.
        if normalize(self._original[start:end]).text != normalize(quote).text:
            return None
        return Located(start, end, tuple(self._spans(start, end)))

    # ------------------------------------------------------------------ spans

    def _spans(self, start: int, end: int) -> list[SourceSpan]:
        spans: list[SourceSpan] = []
        for page_index, page in enumerate(self._doc.pages):
            p_start = self._page_starts[page_index]
            p_end = p_start + len(page.text)
            lo, hi = max(start, p_start), min(end, p_end)
            if lo >= hi:
                continue
            spans.append(
                SourceSpan(
                    page=page.number,
                    start=lo,
                    end=hi,
                    quote=self._original[lo:hi],
                    bboxes=self._bboxes(page_index, page, lo - p_start, hi - p_start),
                )
            )
        return spans

    def _bboxes(self, page_index: int, page: Page, lo: int, hi: int) -> list[BBox]:
        items = self._boxes.get(page_index)
        if items is None:
            items = _placed_boxes(page)
            self._boxes[page_index] = items
        hits = [box for (s, e, box) in items if s < hi and e > lo]
        return _merge_lines(hits)


def _placed(
    page_text: str, pieces: Sequence[tuple[str, BBox | None]]
) -> list[tuple[int, int, BBox]]:
    """Find each piece in reading order in ``page_text``; keep those found with a box."""
    placed: list[tuple[int, int, BBox]] = []
    cursor = 0
    for text, box in pieces:
        if not text.strip():
            continue
        pos = page_text.find(text, cursor)
        if pos < 0:
            continue
        cursor = pos + len(text)
        if box is not None:
            placed.append((pos, pos + len(text), box))
    return placed


def _placed_boxes(page: Page) -> list[tuple[int, int, BBox]]:
    words = _placed(page.text, [(w.text, w.bbox) for w in page.words])
    if words:
        return words
    return _placed(page.text, [(b.text, b.bbox) for b in page.blocks])


def _merge_lines(boxes: list[BBox]) -> list[BBox]:
    """Merge boxes that sit on the same line into one box per line, in order."""
    merged: list[BBox] = []
    for box in boxes:
        if merged:
            last = merged[-1]
            overlap = min(last.bottom, box.bottom) - max(last.top, box.top)
            height = min(last.height, box.height) or 1.0
            if overlap / height >= 0.5:
                merged[-1] = BBox(
                    x0=min(last.x0, box.x0),
                    top=min(last.top, box.top),
                    x1=max(last.x1, box.x1),
                    bottom=max(last.bottom, box.bottom),
                )
                continue
        merged.append(box)
    return merged
