# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Text folding with an offset map back to the original string.

Detectors match patterns on a folded copy of the text, then report spans in the
original text, so evidence stays exactly locatable. Two strengths:

* ``fold_for_matching``: NFKC, lower case, invisible and control characters dropped,
  Arabic diacritics and tatweel dropped, Arabic letter variants unified (alef forms to
  bare alef, alef maqsura to ya, ta marbuta to ha), and Latin look-alikes from Cyrillic
  and Greek mapped to Latin. Used to find instructions however they are disguised.
* ``fold_for_quotes``: NFKC, invisible characters and Arabic diacritics dropped,
  quotes and dashes unified, whitespace runs collapsed to one space. Case is kept.
  Used to check that an evidence quote exists in a document.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# Bidirectional formatting controls (embeddings, overrides, isolates).
BIDI_OVERRIDES = frozenset({0x202D, 0x202E})
BIDI_EMBEDDINGS = frozenset({0x202A, 0x202B, 0x202C})
BIDI_ISOLATES = frozenset({0x2066, 0x2067, 0x2068, 0x2069})
BIDI_CONTROLS = BIDI_OVERRIDES | BIDI_EMBEDDINGS | BIDI_ISOLATES
# Marks that legitimately appear in Arabic text (kept unless something else is wrong).
DIRECTION_MARKS = frozenset({0x200E, 0x200F, 0x061C})
ZERO_WIDTH = frozenset({0x200B, 0x2060, 0xFEFF, 0x180E, 0x2061, 0x2062, 0x2063, 0x2064})
JOINERS = frozenset({0x200C, 0x200D})
SOFT_HYPHEN = 0x00AD
TAG_RANGE = range(0xE0000, 0xE0080)
VARIATION_SELECTORS = frozenset(range(0xFE00, 0xFE10)) | frozenset(range(0xE0100, 0xE01F0))

ARABIC_DIACRITICS = frozenset(range(0x064B, 0x0660)) | {0x0670, 0x06D6, 0x06D7, 0x06D8}
TATWEEL = 0x0640

INVISIBLE = (
    BIDI_CONTROLS
    | DIRECTION_MARKS
    | ZERO_WIDTH
    | JOINERS
    | {SOFT_HYPHEN}
    | frozenset(TAG_RANGE)
    | VARIATION_SELECTORS
)

_ARABIC_MAP = {
    "\u0623": "\u0627",  # alef with hamza above
    "\u0625": "\u0627",  # alef with hamza below
    "\u0622": "\u0627",  # alef with madda
    "\u0671": "\u0627",  # alef wasla
    "\u0649": "\u064a",  # alef maqsura -> ya
    "\u0629": "\u0647",  # ta marbuta -> ha
    "\u0624": "\u0648",  # waw with hamza
    "\u0626": "\u064a",  # ya with hamza
}

# Lower-case Cyrillic and Greek letters that look like Latin letters.
CONFUSABLES = {
    "\u0430": "a",
    "\u0435": "e",
    "\u043e": "o",
    "\u0440": "p",
    "\u0441": "c",
    "\u0443": "y",
    "\u0445": "x",
    "\u0456": "i",
    "\u0458": "j",
    "\u0455": "s",
    "\u0501": "d",
    "\u04bb": "h",
    "\u04cf": "l",
    "\u0261": "g",
    "\u0578": "n",
    "\u057d": "u",
    "\u03b1": "a",
    "\u03bf": "o",
    "\u03b9": "i",
    "\u03b5": "e",
    "\u03bd": "v",
    "\u03ba": "k",
    "\u03c4": "t",
    "\u03c1": "p",
    "\u03c5": "u",
    "\u03c7": "x",
}

_QUOTES = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u2032": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u2033": '"',
    "\u00ab": '"',
    "\u00bb": '"',
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2212": "-",
    "\u00a0": " ",
}


@dataclass(frozen=True, slots=True)
class Folded:
    """``text`` plus, for each of its characters, the index in the original string."""

    text: str
    index: tuple[int, ...]
    original_length: int

    def span(self, start: int, end: int) -> tuple[int, int]:
        """Original ``[start, end)`` covering folded ``[start, end)`` (end exclusive)."""
        if start >= end:
            pos = self.index[start] if start < len(self.index) else self.original_length
            return pos, pos
        return self.index[start], self.index[end - 1] + 1


def is_invisible(ch: str) -> bool:
    cp = ord(ch)
    return cp in INVISIBLE or cp in ARABIC_DIACRITICS or cp == TATWEEL


def fold_for_matching(text: str) -> Folded:
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if is_invisible(ch):
            continue
        for part in unicodedata.normalize("NFKC", ch):
            for low in part.lower():
                if is_invisible(low):
                    continue
                low = _ARABIC_MAP.get(low, low)
                low = CONFUSABLES.get(low, low)
                out.append(low)
                index.append(i)
    return Folded("".join(out), tuple(index), len(text))


def fold_for_quotes(text: str) -> Folded:
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if is_invisible(ch):
            continue
        for part in unicodedata.normalize("NFKC", ch):
            if is_invisible(part):
                continue
            part = _QUOTES.get(part, part)
            if part.isspace():
                if not out or out[-1] == " ":
                    continue
                part = " "
            out.append(part)
            index.append(i)
    if out and out[-1] == " ":
        out.pop()
        index.pop()
    return Folded("".join(out), tuple(index), len(text))


def strip_invisible(text: str) -> str:
    """``text`` without invisible formatting characters (for display in explanations)."""
    return "".join(ch for ch in text if ord(ch) not in INVISIBLE)


# ------------------------------------------------------------------ wrapped quotes

LINE_BREAKS = frozenset("\n\r\x0b\x0c\x85\u2028\u2029")


def _skip(s: str, i: int, end: int) -> int:
    while i < end and is_invisible(s[i]):
        i += 1
    return i


def _wrap_after(text: str, i: int, end: int) -> int | None:
    """When ``text[i:]`` is a whitespace run (invisible characters allowed) holding a
    line break and followed by a visible character, the index after it."""
    k = i
    has_break = False
    while k < end and (text[k].isspace() or is_invisible(text[k])):
        has_break = has_break or text[k] in LINE_BREAKS
        k += 1
    if has_break and k < end and k > i:
        return k
    return None


def match_wrapped(text: str, quote: str, start: int, end: int | None = None) -> int | None:
    """End (exclusive) of ``quote`` matched in ``text`` at exactly ``start``, or ``None``.

    The quote's characters must appear in ``text`` in order and contiguous, with three
    allowances only: a whitespace run of the quote matches any whitespace run of the
    text (spaces, tabs or line breaks); invisible characters (soft hyphens, zero-width
    characters) are ignored on both sides; and a line wrap inside a word is accepted,
    with or without its hyphen ("manage-" then a line break then "ment" matches
    "management" and "manage-ment"). Case and every other character must be equal."""
    limit = len(text) if end is None else min(end, len(text))
    q = quote.strip()
    i, j, last = start, 0, None
    while True:
        i = _skip(text, i, limit)
        j = _skip(q, j, len(q))
        if j >= len(q):
            return last
        if i >= limit:
            return None
        qc, tc = q[j], text[i]
        if qc.isspace():
            if not tc.isspace():
                return None
            while i < limit and (text[i].isspace() or is_invisible(text[i])):
                i += 1
            while j < len(q) and (q[j].isspace() or is_invisible(q[j])):
                j += 1
            continue
        # A line wrap in the text right after a soft hyphen: the word continues.
        if tc.isspace() and last is not None and chr(SOFT_HYPHEN) in text[last:i]:
            after = _wrap_after(text, i, limit)
            if after is not None:
                i = after
                continue
        if tc == "-" and qc != "-":
            after = _wrap_after(text, i + 1, limit)
            if after is None:
                return None
            i = after  # a hyphenated wrap quoted as one word
            continue
        if tc != qc:
            return None
        i += 1
        j += 1
        last = i
        if tc == "-":
            nxt = _skip(q, j, len(q))
            after = _wrap_after(text, i, limit)
            if after is not None and (nxt >= len(q) or not q[nxt].isspace()):
                i = after  # "full-" then a line break then "cycle" quoted as "full-cycle"


def find_wrapped(
    text: str, quote: str, *, starts: range | None = None, end: int | None = None
) -> list[tuple[int, int]]:
    """Every ``[start, end)`` of ``text`` matching ``quote`` (``match_wrapped``) that
    starts in ``starts`` (default: anywhere) and ends by ``end``."""
    q = quote.strip()
    head = _skip(q, 0, len(q))
    if head >= len(q):
        return []
    first = q[head]
    window = starts if starts is not None else range(len(text))
    out: list[tuple[int, int]] = []
    for s in window:
        if s >= len(text):
            break
        if text[s] != first:
            continue
        stop = match_wrapped(text, q, s, end)
        if stop is not None:
            out.append((s, stop))
    return out
