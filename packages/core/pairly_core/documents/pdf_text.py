# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Reading order for a PDF page: lines, columns and right-to-left text.

pdfplumber's ``extract_text`` reads a page line by line across its whole width, so
two columns printed side by side come out interleaved, and Arabic drawn the way most
PDF producers draw it (shaped presentation forms, in visual order) comes out
reversed. ``lay_out`` builds the page text itself from the characters:

1. **Lines**: characters are grouped by their vertical centre; within a line they are
   ordered by x, and a space is inserted where the gap is wider than a fifth of the
   font size (when the PDF has no space glyph there).
2. **Segments**: a line is cut where the gap is wide (``max(10pt, 1.5 x size)``), so
   text in two columns on the same baseline becomes two segments.
3. **Columns**: per page, the vertical gutter that the fewest segments cross is
   found. It is used only when both sides hold at least ``MIN_COLUMN_SEGMENTS``
   segments and ``MIN_COLUMN_SHARE`` of the characters. Segments that cross it
   (a full-width heading) cut the page into bands; inside a band the left column is
   emitted top to bottom, then the right one.
4. **Right to left**: Arabic presentation forms and ligatures are normalised (NFKC,
   so a lam-alef ligature becomes two letters), and a segment holding right-to-left
   letters is put back into logical order: the visual sequence is reversed, runs of
   left-to-right text (Latin, digits) keep their own order, brackets are mirrored.

Every character keeps its box and gets the offset of its text in the page text
(``Char.offset``), so the hidden-text check still finds exactly which characters to
remove, whatever the order.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

MIN_COLUMN_SEGMENTS: Final = 5
MIN_COLUMN_SHARE: Final = 0.08
GUTTER_MIN: Final = 8.0
_MIRROR: Final = {"(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{", "<": ">", ">": "<"}
_LTR: Final = frozenset({"L", "EN", "AN"})
_RTL: Final = frozenset({"R", "AL"})


def normalise_glyph(text: str) -> str:
    """NFKC for presentation forms and ligatures (Arabic and Latin); other text as is."""
    if any(0xFB00 <= ord(c) <= 0xFDFF or 0xFE70 <= ord(c) <= 0xFEFF for c in text):
        return unicodedata.normalize("NFKC", text)
    return text


def _bidi(text: str) -> str:
    for ch in text:
        kind = unicodedata.bidirectional(ch)
        if kind:
            return kind
    return "ON"


@dataclass
class _Item:
    text: str
    char: int | None  # index into the page's raw characters, None for an inserted space


@dataclass
class Segment:
    chars: list[int]
    x0: float
    x1: float
    top: float
    bottom: float
    items: list[_Item] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.chars)


@dataclass
class PageText:
    text: str
    # Per raw character: its offset in ``text`` and its (normalised) text.
    offsets: list[int | None]
    glyphs: list[str]
    # Segments in reading order, with their (start, end) in ``text``.
    segments: list[tuple[Segment, int, int]]
    columns: bool


def _num(obj: dict[str, Any], key: str) -> float:
    return float(obj[key])


def _lines(chars: Sequence[dict[str, Any]]) -> list[list[int]]:
    order = sorted(
        range(len(chars)),
        key=lambda i: (
            (_num(chars[i], "top") + _num(chars[i], "bottom")) / 2,
            _num(chars[i], "x0"),
        ),
    )
    lines: list[list[int]] = []
    centre = 0.0
    for i in order:
        ch = chars[i]
        cy = (_num(ch, "top") + _num(ch, "bottom")) / 2
        height = max(_num(ch, "bottom") - _num(ch, "top"), 0.5)
        if lines and abs(cy - centre) <= max(1.5, 0.35 * height):
            lines[-1].append(i)
            members = lines[-1]
            centre = centre + (cy - centre) / len(members)
        else:
            lines.append([i])
            centre = cy
    return lines


def _segments(chars: Sequence[dict[str, Any]], line: list[int]) -> list[Segment]:
    ordered = sorted(line, key=lambda i: (_num(chars[i], "x0"), _num(chars[i], "top")))
    out: list[Segment] = []
    current: list[int] = []
    last_x1 = 0.0
    for i in ordered:
        ch = chars[i]
        size = float(ch.get("size") or 10.0)
        gap = _num(ch, "x0") - last_x1
        if current and gap > max(10.0, 1.5 * size) and not str(ch.get("text", "")).isspace():
            out.append(_segment(chars, current))
            current = []
        if not str(ch.get("text", "")).isspace() or not current:
            last_x1 = max(last_x1, _num(ch, "x1")) if current else _num(ch, "x1")
        current.append(i)
    if current:
        out.append(_segment(chars, current))
    return out


def _segment(chars: Sequence[dict[str, Any]], ids: list[int]) -> Segment:
    # Trailing and leading space glyphs belong to no segment's text.
    while ids and str(chars[ids[-1]].get("text", "")).isspace():
        ids = ids[:-1]
    while ids and str(chars[ids[0]].get("text", "")).isspace():
        ids = ids[1:]
    if not ids:
        return Segment([], 0, 0, 0, 0)
    return Segment(
        chars=ids,
        x0=min(_num(chars[i], "x0") for i in ids),
        x1=max(_num(chars[i], "x1") for i in ids),
        top=min(_num(chars[i], "top") for i in ids),
        bottom=max(_num(chars[i], "bottom") for i in ids),
    )


def _items(chars: Sequence[dict[str, Any]], seg: Segment) -> list[_Item]:
    """Visual order (left to right), with spaces where the PDF left a gap."""
    items: list[_Item] = []
    prev_x1: float | None = None
    for i in seg.chars:
        ch = chars[i]
        text = normalise_glyph(str(ch.get("text", "")))
        size = float(ch.get("size") or 10.0)
        if (
            prev_x1 is not None
            and _num(ch, "x0") - prev_x1 > 0.2 * size
            and items
            and not items[-1].text.isspace()
            and not text.isspace()
        ):
            items.append(_Item(" ", None))
        items.append(_Item(text, i))
        prev_x1 = _num(ch, "x1")
    return items


def logical_order(items: list[_Item]) -> list[_Item]:
    """Visual left-to-right items of a right-to-left line, in logical order."""
    if not any(_bidi(it.text) in _RTL for it in items):
        return items
    kinds = [_bidi(it.text) for it in items]
    # Neutrals between two left-to-right items belong to that run ("Oracle Fusion").
    ltr = [k in _LTR for k in kinds]
    n = len(items)
    i = 0
    while i < n:
        if not ltr[i] and kinds[i] not in _RTL:
            j = i
            while j < n and not ltr[j] and kinds[j] not in _RTL:
                j += 1
            if i > 0 and j < n and ltr[i - 1] and ltr[j]:
                for k in range(i, j):
                    ltr[k] = True
            i = j
        else:
            i += 1
    # Number terminators next to digits stay with the number ("55%"); separators only
    # between two digits ("4,000") (UAX #9 rules W4 and W5).
    digit = ("EN", "AN")
    for k, kind in enumerate(kinds):
        before = k > 0 and kinds[k - 1] in digit
        after = k + 1 < n and kinds[k + 1] in digit
        if (kind == "ET" and (before or after)) or (kind in ("ES", "CS") and before and after):
            ltr[k] = True
    runs: list[tuple[bool, list[_Item]]] = []
    for item, is_ltr in zip(items, ltr, strict=True):
        if runs and runs[-1][0] == is_ltr:
            runs[-1][1].append(item)
        else:
            runs.append((is_ltr, [item]))
    out: list[_Item] = []
    for is_ltr, run in reversed(runs):
        if is_ltr:
            out.extend(run)
        else:
            for item in reversed(run):
                mirrored = _MIRROR.get(item.text, item.text)
                out.append(_Item(mirrored, item.char))
    return out


def _gutter(segments: list[Segment], width: float) -> float | None:
    inside = [s for s in segments if s.size and s.x0 >= 0 and s.x1 <= width]
    total = sum(s.size for s in inside)
    if total == 0:
        return None
    best: tuple[int, float, float] | None = None
    x = width * 0.15
    while x <= width * 0.85:
        left = [s for s in inside if s.x1 <= x - GUTTER_MIN / 2]
        right = [s for s in inside if s.x0 >= x + GUTTER_MIN / 2]
        crossing = len(inside) - len(left) - len(right)
        if (
            len(left) >= MIN_COLUMN_SEGMENTS
            and len(right) >= MIN_COLUMN_SEGMENTS
            and sum(s.size for s in left) >= MIN_COLUMN_SHARE * total
            and sum(s.size for s in right) >= MIN_COLUMN_SHARE * total
            and crossing <= 0.25 * len(inside)
        ):
            room = min(s.x0 for s in right) - max(s.x1 for s in left)
            key = (crossing, -room, x)
            if best is None or key < best:
                best = key
        x += 2.0
    return best[2] if best is not None else None


def _reading_order(segments: list[Segment], gutter: float | None) -> list[Segment]:
    ordered = sorted(segments, key=lambda s: (s.top, s.x0))
    if gutter is None:
        return ordered
    out: list[Segment] = []
    left: list[Segment] = []
    right: list[Segment] = []

    def flush() -> None:
        out.extend(left)
        out.extend(right)
        left.clear()
        right.clear()

    for seg in ordered:
        if seg.x1 <= gutter:
            left.append(seg)
        elif seg.x0 >= gutter:
            right.append(seg)
        else:
            flush()
            out.append(seg)
    flush()
    return out


def lay_out(chars: Sequence[dict[str, Any]], width: float) -> PageText:
    """The page text in reading order, and where each character sits in it."""
    segments: list[Segment] = []
    for line in _lines(chars):
        segments.extend(s for s in _segments(chars, line) if s.size)
    gutter = _gutter(segments, width)
    ordered = _reading_order(segments, gutter)
    offsets: list[int | None] = [None] * len(chars)
    glyphs = [normalise_glyph(str(ch.get("text", ""))) for ch in chars]
    parts: list[str] = []
    placed: list[tuple[Segment, int, int]] = []
    cursor = 0
    for seg in ordered:
        if parts:
            parts.append("\n")
            cursor += 1
        start = cursor
        for item in logical_order(_items(chars, seg)):
            if item.char is not None:
                offsets[item.char] = cursor
                glyphs[item.char] = item.text
            parts.append(item.text)
            cursor += len(item.text)
        placed.append((seg, start, cursor))
    return PageText("".join(parts), offsets, glyphs, placed, gutter is not None)
