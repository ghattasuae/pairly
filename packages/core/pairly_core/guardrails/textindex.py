# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Where each block and character of a ``NormalizedDocument`` sits in ``doc.text``.

``doc.text`` joins page texts with a blank line. Every parser builds a page's text from
its blocks in order (PDF and OCR: lines joined by newlines; DOCX: runs of a paragraph
concatenated, paragraphs joined by newlines; comments and footnotes are not part of
the text), so each block is found by a forward search from the end of the previous
one. PDF characters carry their offset in the page text (``Char.offset``, recorded by
the parser, which also handles columns and right-to-left text); each is checked
against the text before use. Without recorded offsets, characters are aligned inside
their line: the characters whose centre lies in the line's box, in reading order (x),
matched one by one against the line text.

Anything that cannot be placed exactly gets ``None``. Callers fail closed on that: the
hidden-text detector then removes the whole line, or every occurrence of the hidden
string, rather than risk leaving hidden text in model input.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from pairly_core.documents.model import NormalizedDocument, Page

PAGE_SEPARATOR = "\n\n"
_TOLERANCE = 0.5


@dataclass(frozen=True, slots=True)
class PageIndex:
    number: int
    # Offset of the page's text in ``doc.text``.
    base: int
    length: int
    # Per block: (start, end) in ``doc.text``, or None.
    blocks: tuple[tuple[int, int] | None, ...]
    # Per PDF character: its offset in ``doc.text``, or None.
    chars: tuple[int | None, ...]
    # Per PDF character: index of the block (line) it belongs to, or None.
    char_blocks: tuple[int | None, ...]


@dataclass(frozen=True, slots=True)
class TextIndex:
    text: str
    pages: tuple[PageIndex, ...]

    def page_of(self, offset: int) -> int | None:
        for page in self.pages:
            if page.base <= offset <= page.base + page.length:
                return page.number
        return None


def _block_spans(page: Page, base: int) -> list[tuple[int, int] | None]:
    spans: list[tuple[int, int] | None] = []
    cursor = 0
    for block in page.blocks:
        if not block.text or block.region in {"comment", "footnote"}:
            spans.append(None)
            continue
        found = page.text.find(block.text, cursor)
        if found < 0:
            spans.append(None)
            continue
        spans.append((base + found, base + found + len(block.text)))
        cursor = found + len(block.text)
    return spans


def _recorded(
    page: Page, spans: list[tuple[int, int] | None], text: str, base: int
) -> tuple[list[int | None], list[int | None]] | None:
    """Offsets the parser recorded (``Char.offset``), checked against the text."""
    if not page.chars or all(ch.offset is None for ch in page.chars):
        return None
    starts = [(s[0], s[1], bi) for bi, s in enumerate(spans) if s is not None]
    offsets: list[int | None] = [None] * len(page.chars)
    owners: list[int | None] = [None] * len(page.chars)
    for ci, ch in enumerate(page.chars):
        if ch.offset is None or not ch.text:
            continue
        at = base + ch.offset
        if not text.startswith(ch.text, at) or ch.offset + len(ch.text) > len(page.text):
            continue  # fail closed: an offset that does not match is not used
        offsets[ci] = at
        for start, end, bi in starts:
            if start <= at < end:
                owners[ci] = bi
                break
    return offsets, owners


def _align_chars(
    page: Page, spans: list[tuple[int, int] | None], text: str, base: int = 0
) -> tuple[list[int | None], list[int | None]]:
    """Map PDF chars to offsets: recorded ones, else via the line whose box holds them."""
    recorded = _recorded(page, spans, text, base)
    if recorded is not None:
        return recorded
    offsets: list[int | None] = [None] * len(page.chars)
    owners: list[int | None] = [None] * len(page.chars)
    lines = [
        (b.bbox.top - _TOLERANCE, b.bbox.bottom + _TOLERANCE, b.bbox, i)
        for i, b in enumerate(page.blocks)
        if b.bbox is not None
    ]
    if not lines or not page.chars:
        return offsets, owners
    lines.sort(key=lambda item: item[0])
    tops = [item[0] for item in lines]
    members: dict[int, list[int]] = {}
    for ci, ch in enumerate(page.chars):
        cx = (ch.bbox.x0 + ch.bbox.x1) / 2
        cy = (ch.bbox.top + ch.bbox.bottom) / 2
        hi = bisect.bisect_right(tops, cy)
        for top, bottom, box, bi in reversed(lines[:hi]):
            if cy > bottom:
                # Lines are sorted by top only; a taller earlier line may still hold it.
                continue
            if top <= cy <= bottom and box.x0 - _TOLERANCE <= cx <= box.x1 + _TOLERANCE:
                members.setdefault(bi, []).append(ci)
                owners[ci] = bi
                break
    for bi, char_ids in members.items():
        span = spans[bi]
        if span is None:
            continue
        start, end = span
        char_ids.sort(key=lambda ci: page.chars[ci].bbox.x0)
        cursor = start
        for ci in char_ids:
            glyph = page.chars[ci].text
            if not glyph:
                continue
            probe = cursor
            while probe < end and text[probe].isspace() and not glyph.isspace():
                probe += 1
            if text.startswith(glyph, probe) and probe + len(glyph) <= end:
                offsets[ci] = probe
                cursor = probe + len(glyph)
    return offsets, owners


def build_index(doc: NormalizedDocument) -> TextIndex:
    text = doc.text
    pages: list[PageIndex] = []
    base = 0
    for page in doc.pages:
        spans = _block_spans(page, base)
        offsets, owners = _align_chars(page, spans, text, base)
        pages.append(
            PageIndex(
                number=page.number,
                base=base,
                length=len(page.text),
                blocks=tuple(spans),
                chars=tuple(offsets),
                char_blocks=tuple(owners),
            )
        )
        base += len(page.text) + len(PAGE_SEPARATOR)
    return TextIndex(text=text, pages=tuple(pages))
