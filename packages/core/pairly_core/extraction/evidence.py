# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
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
characters and soft hyphens dropped). A word cut by a line wrap ("manage-" then a line
break then "ment") also matches the whole word, with or without its hyphen. The spans
returned always point at the exact original text.

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

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol, runtime_checkable

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


@runtime_checkable
class PartlyHiddenInput(SanitizedInput, Protocol):
    """A model input with hidden parts (placeholders) that never map back."""

    def visible_ranges(self, start: int, end: int) -> list[tuple[int, int]]: ...


@dataclass(frozen=True, slots=True)
class Normalized:
    """A normalised string and, for each of its characters, the source index."""

    text: str
    index: tuple[int, ...]


_LINE_BREAKS = frozenset("\n\r\x0b\x0c\x85\u2028\u2029")
_LINE_RE = re.compile(r"[^\n\r\x0b\x0c\x85\u2028\u2029]+")
_LINE_SPLIT = re.compile(r"[\n\r\x0b\x0c\x85\u2028\u2029]+")
Wraps = Literal["space", "keep", "join"]
WRAP_VARIANTS: tuple[Wraps, ...] = ("space", "keep", "join")


def normalize(text: str, wraps: Wraps = "space") -> Normalized:
    """``text`` normalised for matching, with the source index of each character.

    ``wraps`` says what a line break inside a word becomes (a hyphen or a soft hyphen
    right before a line break): ``space`` (the default) treats it as any whitespace,
    ``keep`` joins the word and keeps the hyphen ("full-cycle"), ``join`` joins it
    without the hyphen ("management"). A soft hyphen is dropped in every variant."""
    out: list[str] = []
    index: list[int] = []
    pending_space = False
    soft_before = False  # a soft hyphen since the last visible character
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch in _ZERO_WIDTH:
            soft_before = soft_before or ch == "\u00ad"
            i += 1
            continue
        if ch.isspace():
            k = i
            has_break = False
            while k < n and (text[k].isspace() or text[k] in _ZERO_WIDTH):
                has_break = has_break or text[k] in _LINE_BREAKS
                k += 1
            # A hyphen right after a letter or digit: a word cut by the line wrap.
            hyphen = len(out) >= 2 and out[-1] == "-" and out[-2].isalnum()
            if wraps != "space" and has_break and out and k < n and (soft_before or hyphen):
                if wraps == "join" and hyphen and not soft_before:
                    out.pop()
                    index.pop()
            else:
                pending_space = bool(out)
            soft_before = False
            i = k
            continue
        soft_before = False
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
        i += 1
    return Normalized("".join(out), tuple(index))


def find_normalized(
    haystack: Normalized, needle: str, wraps: Wraps = "space"
) -> tuple[int, int] | None:
    """First raw ``[start, end)`` range of ``haystack``'s source matching ``needle``.
    ``haystack`` must have been normalised with the same ``wraps``."""
    target = normalize(needle, wraps).text
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
        self._norms: dict[Wraps, Normalized] = {}
        self._lines: list[tuple[int, int, str]] | None = None
        starts: list[int] = []
        offset = 0
        for page in doc.pages:
            starts.append(offset)
            offset += len(page.text) + len(PAGE_SEPARATOR)
        self._page_starts = starts
        self._boxes: dict[int, list[tuple[int, int, BBox]]] = {}

    def _norm(self, wraps: Wraps) -> Normalized:
        found = self._norms.get(wraps)
        if found is None:
            found = normalize(self._sanitized.text, wraps)
            self._norms[wraps] = found
        return found

    def locate(self, quote: str | None) -> Located | None:
        """The quote in the model's text, mapped to the original, or ``None``.

        Tried as-is first; then with line breaks inside words joined (a PDF line that
        wraps a hyphenated or soft-hyphenated word), keeping and then dropping the
        hyphen. The matched original text must normalise to the quote the same way."""
        if quote is None or not quote.strip() or len(quote) > MAX_QUOTE_CHARS:
            return None
        for wraps in WRAP_VARIANTS:
            located = self._locate(quote, wraps)
            if located is not None:
                return located
        return None

    def locate_lines(self, quote: str | None) -> tuple[Located, ...]:
        """For a quote of several lines that is not in the text as a whole, each of its
        lines that is (in order of the quote).

        A table read row by row puts a label and its value on different lines, often
        far apart ("Invoice No." ... "MAL/26/0854"), and the model quotes them as one
        passage. The joined quote is no evidence; a line of it can be, but only where
        its location is never a guess: it equals exactly one whole line of the model's
        text, or it occurs exactly once in that text. The caller checks that the line
        it uses holds the value and sits under or beside another line of the quote
        (``beside_or_below``), so a value is never paired with an unrelated label."""
        if quote is None or not quote.strip() or len(quote) > MAX_QUOTE_CHARS:
            return ()
        lines = [line for line in _LINE_SPLIT.split(quote) if line.strip()]
        if len(lines) < 2 or self.locate(quote) is not None:
            return ()
        haystack = self._norm("space").text
        out: list[Located] = []
        for line in lines:
            target = normalize(line).text
            if not target:
                continue
            whole = [r for r in self._text_lines() if r[2] == target]
            located: Located | None = None
            if len(whole) == 1:
                located = self._at(whole[0][0], whole[0][1], "space")
            elif not whole:
                first = haystack.find(target)
                if first >= 0 and haystack.find(target, first + 1) < 0:
                    located = self._locate(line, "space")
            if located is not None:
                out.append(located)
        return tuple(out)

    def _text_lines(self) -> list[tuple[int, int, str]]:
        """Each non-blank line of the model's text: its range (trimmed) and its
        normalised text."""
        if self._lines is None:
            text = self._sanitized.text
            found: list[tuple[int, int, str]] = []
            for match in _LINE_RE.finditer(text):
                lo, hi = match.start(), match.end()
                while lo < hi and text[lo].isspace():
                    lo += 1
                while hi > lo and text[hi - 1].isspace():
                    hi -= 1
                if lo < hi:
                    found.append((lo, hi, normalize(text[lo:hi]).text))
            self._lines = found
        return self._lines

    def _at(self, lo: int, hi: int, wraps: Wraps) -> Located | None:
        """``Located`` for model-text ``[lo, hi)``, checked against the original."""
        mapped = self._sanitized.to_original(lo, hi)
        if mapped is None:
            return None
        start, end = mapped
        if not 0 <= start < end <= len(self._original):
            return None
        if (
            normalize(self._original[start:end], wraps).text
            != normalize(self._sanitized.text[lo:hi], wraps).text
        ):
            return None
        return Located(start, end, tuple(self._spans(start, end)))

    def locate_visible(self, quote: str | None) -> tuple[Located, ...]:
        """``locate``, or for a quote that runs over hidden text (a blind placeholder
        the model copied), each visible part of it on its own.

        The whole quote must still be found, contiguous, in the model's text. Hidden
        text is never located: only the ranges the input reports as visible are mapped
        back, each trimmed of edge punctuation and checked against the original. A part
        shorter than one real word is dropped (``_real_word``: a run of at least three
        letters or digits, or a complete number; security review 2026-10, L-05), so
        "of" or a cut "20" never earns credit."""
        whole = self.locate(quote)
        if whole is not None:
            return (whole,)
        if (
            quote is None
            or not quote.strip()
            or len(quote) > MAX_QUOTE_CHARS
            or not isinstance(self._sanitized, PartlyHiddenInput)
        ):
            return ()
        model_text = self._sanitized.text
        for wraps in WRAP_VARIANTS:
            found = find_normalized(self._norm(wraps), quote, wraps)
            if found is None:
                continue
            parts: list[Located] = []
            for lo, hi in self._sanitized.visible_ranges(*found):
                lo, hi = _trim(model_text, lo, hi)
                if not _real_word(model_text, lo, hi):
                    continue
                located = self._at(lo, hi, wraps)
                if located is not None:
                    parts.append(located)
            if parts:
                return tuple(parts)
        return ()

    def _locate(self, quote: str, wraps: Wraps) -> Located | None:
        found = find_normalized(self._norm(wraps), quote, wraps)
        if found is None:
            return None
        mapped = self._sanitized.to_original(*found)
        if mapped is None:
            return None
        start, end = mapped
        if not 0 <= start < end <= len(self._original):
            return None
        # The offset map must agree with the text: never trust it blindly.
        if normalize(self._original[start:end], wraps).text != normalize(quote, wraps).text:
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


def beside_or_below(label: Located, value: Located) -> bool:
    """True when the page layout puts ``value`` right of ``label`` on the same row, or
    under it in the same column within three line heights (a form or table cell).
    Without boxes (DOCX, plain text) nothing is associated."""
    if len(label.spans) != 1 or len(value.spans) != 1:
        return False
    a, b = label.spans[0], value.spans[0]
    if a.page != b.page or not a.bboxes or not b.bboxes or a.start == b.start:
        return False
    lab, val = a.bboxes[-1], b.bboxes[0]
    height = max(min(lab.height, val.height), 1.0)
    rows = min(lab.bottom, val.bottom) - max(lab.top, val.top)
    columns = min(lab.x1, val.x1) - max(lab.x0, val.x0)
    if rows >= 0.5 * height and val.x0 >= lab.x1 - 0.5 * height:
        return True  # same row, value to the right
    gap = val.top - lab.bottom
    return columns > 0 and -0.25 * height <= gap <= 3 * height


# A number written out whole: digits, with thousands or decimal separators, an optional
# sign and percent sign ("20", "4.5", "1,200", "-3", "15%").
_NUMBER = re.compile(r"[+-]?\d+(?:[.,]\d+)*%?")
MIN_WORD_CHARS: Final = 3


def _real_word(text: str, lo: int, hi: int) -> bool:
    """Whether ``text[lo:hi]`` holds at least one real word: a run of at least
    ``MIN_WORD_CHARS`` letters or digits, or is a complete number (the whole of it, not
    cut by the edge of the visible part: no letter or digit right before or after)."""
    part = text[lo:hi]
    run = 0
    for ch in part:
        run = run + 1 if ch.isalnum() else 0
        if run >= MIN_WORD_CHARS:
            return True
    if not part or _NUMBER.fullmatch(part) is None:
        return False
    before = text[lo - 1] if lo > 0 else ""
    after = text[hi] if hi < len(text) else ""
    return not before.isalnum() and not after.isalnum()


def _trim(text: str, lo: int, hi: int) -> tuple[int, int]:
    """``[lo, hi)`` without whitespace or punctuation at either end."""

    def edge(ch: str) -> bool:
        return ch.isspace() or unicodedata.category(ch).startswith("P") or ch in _ZERO_WIDTH

    while lo < hi and edge(text[lo]):
        lo += 1
    while hi > lo and edge(text[hi - 1]):
        hi -= 1
    return lo, hi


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
    # PDF characters know their offset in the page text: exact, whatever the reading
    # order (words come in visual order, which differs from the text's order when the
    # page is read column by column).
    chars = sorted(
        (c.offset, c.offset + len(c.text), c.bbox)
        for c in page.chars
        if c.offset is not None and c.text.strip() and not c.off_page
    )
    if chars:
        return [(s, e, box) for s, e, box in chars]
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
