# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Arabic text for the PDF receipt: contextual shaping plus right-to-left ordering.

ReportLab draws glyphs in the order it is given and does not shape Arabic, so a line is
prepared before drawing:

1. ``arabic_reshaper`` (MIT) replaces Arabic letters with their contextual presentation
   forms (initial, medial, final, isolated) and ligatures, in logical order.
2. ``visual_order`` reorders one line for a right-to-left paragraph. It implements the
   part of the Unicode Bidirectional Algorithm (UAX #9) that receipt lines need, with
   the paragraph level fixed at 1 (RTL): strong types from ``unicodedata``, numbers
   stay left to right (W2, W7), separators between digits join the number (W4),
   neutrals take the direction of their neighbours or the paragraph's (N1, N2), then
   levels are reversed (L2) and brackets in right-to-left runs are mirrored (L4). It
   has no explicit embeddings or isolates: receipt text never contains them, and any
   such control character is dropped.

``python-bidi`` was not used: it is LGPL-3.0, and this subset is small and fully tested.
"""

from __future__ import annotations

import unicodedata
from typing import Final

import arabic_reshaper  # type: ignore[import-untyped]

_MIRROR: Final = {
    "(": ")",
    ")": "(",
    "[": "]",
    "]": "[",
    "{": "}",
    "}": "{",
    "<": ">",
    ">": "<",
    "«": "»",
    "»": "«",
}
# Explicit directional formatting characters (dropped, see the module docstring).
_EXPLICIT: Final = frozenset("‎‏‪‫‬‭‮⁦⁧⁨⁩")

_RESHAPER = arabic_reshaper.ArabicReshaper(
    configuration={"delete_harakat": False, "support_ligatures": True}
)


def has_rtl(text: str) -> bool:
    return any(unicodedata.bidirectional(c) in {"R", "AL"} for c in text)


def _classes(text: str, base: str) -> list[str]:
    """Resolved types per character: ``R``, ``L`` or ``N`` (number, left to right)."""
    raw = [unicodedata.bidirectional(c) or "ON" for c in text]
    # NSM takes the type of the previous character (W1).
    for i, kind in enumerate(raw):
        if kind == "NSM":
            raw[i] = raw[i - 1] if i else base
    kinds: list[str] = []
    for kind in raw:
        if kind in {"R", "AL"}:
            kinds.append("R")
        elif kind == "L":
            kinds.append("L")
        elif kind in {"EN", "AN"}:
            kinds.append("N")
        elif kind in {"ES", "CS", "ET"}:
            kinds.append(kind)
        else:
            kinds.append("ON")
    # W4/W5: a separator between two numbers, and terminators next to a number, join it.
    for i, kind in enumerate(kinds):
        if kind in {"ES", "CS"}:
            prev = kinds[i - 1] if i else None
            nxt = kinds[i + 1] if i + 1 < len(kinds) else None
            kinds[i] = "N" if prev == "N" and nxt == "N" else "ON"
    for i, kind in enumerate(kinds):
        if kind == "ET":
            prev = kinds[i - 1] if i else None
            nxt = kinds[i + 1] if i + 1 < len(kinds) else None
            kinds[i] = "N" if "N" in (prev, nxt) else "ON"
    # W7: a number after a left-to-right letter (or at the start of a left-to-right
    # paragraph) is left to right.
    last_strong = base
    for i, kind in enumerate(kinds):
        if kind in {"R", "L"}:
            last_strong = kind
        elif kind == "N" and last_strong == "L":
            kinds[i] = "L"
    # N1/N2: neutrals between two types of the same direction take it (numbers count
    # as right to left, per N1), otherwise they take the paragraph's direction.
    i = 0
    while i < len(kinds):
        if kinds[i] != "ON":
            i += 1
            continue
        j = i
        while j < len(kinds) and kinds[j] == "ON":
            j += 1
        before = _direction(kinds[i - 1]) if i else base
        after = _direction(kinds[j]) if j < len(kinds) else base
        side = before if before == after else base
        for k in range(i, j):
            kinds[k] = side
        i = j
    return kinds


def _direction(kind: str) -> str:
    return "L" if kind == "L" else "R"


def visual_order(text: str, *, rtl: bool = True) -> str:
    """One logical line, in the order to draw it left to right.

    ``rtl``: the paragraph direction (Arabic receipt) or left to right (English receipt
    that quotes Arabic data).
    """
    chars = [c for c in text if c not in _EXPLICIT]
    if not chars:
        return ""
    base = "R" if rtl else "L"
    kinds = _classes("".join(chars), base)
    # Levels (I1, I2): paragraph level 1 gives R = 1, L and numbers = 2; paragraph
    # level 0 gives L = 0, R = 1, numbers = 2.
    if rtl:
        levels = [1 if k == "R" else 2 for k in kinds]
    else:
        levels = [0 if k == "L" else 1 if k == "R" else 2 for k in kinds]
    glyphs = [_MIRROR.get(c, c) if lvl % 2 else c for c, lvl in zip(chars, levels, strict=True)]
    # L2: from the highest level down to 1, reverse every run at that level or higher.
    for level in range(max(levels), 0, -1):
        i = 0
        while i < len(glyphs):
            if levels[i] < level:
                i += 1
                continue
            j = i
            while j < len(glyphs) and levels[j] >= level:
                j += 1
            glyphs[i:j] = glyphs[i:j][::-1]
            levels[i:j] = levels[i:j][::-1]
            i = j
    return "".join(glyphs)


def shape(text: str, *, rtl: bool = True) -> str:
    """Shape Arabic letters and order the line for drawing left to right."""
    if not has_rtl(text):
        return text if not rtl else visual_order(text, rtl=True)
    return visual_order(str(_RESHAPER.reshape(text)), rtl=rtl)


def shape_rtl(text: str) -> str:
    return shape(text, rtl=True)
