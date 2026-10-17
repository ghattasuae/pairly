# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""What the checks read from a parsed document (``NormalizedDocument``).

* fonts: base families (subset prefixes and weights dropped: ``ABCDEF+Times-Bold`` is
  ``Times``);
* accents: colours of filled areas other than white;
* overprinted characters: two different characters drawn at (almost) the same place,
  which ordinary invoice software never does but pasting a new figure over an old one
  does;
* whiteout digits: digits whose centre lies under a small white filled box.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Final

from pairly_core.documents.model import BBox, Char, NormalizedDocument, Page
from pairly_pay.checks.types import DocumentFacts

MAX_CHARS_PER_PAGE: Final = 20_000
_SUBSET: Final = re.compile(r"^[A-Z]{6}\+")
_WEIGHT: Final = re.compile(
    r"[-,]?(Bold|Italic|Oblique|BoldItalic|BoldOblique|Roman|Regular|Medium|Light|MT|PS)+$",
    re.I,
)
_WHITE: Final = frozenset({"#ffffff", "#fefefe", "#fdfdfd"})
GRID: Final = 3.0


def font_family(name: str | None) -> str | None:
    if not name:
        return None
    base = _SUBSET.sub("", name)
    previous = None
    while previous != base:
        previous = base
        base = _WEIGHT.sub("", base)
    return base or None


def _iou(a: BBox, b: BBox) -> float:
    x0, x1 = max(a.x0, b.x0), min(a.x1, b.x1)
    top, bottom = max(a.top, b.top), min(a.bottom, b.bottom)
    if x1 <= x0 or bottom <= top:
        return 0.0
    inter = (x1 - x0) * (bottom - top)
    union = a.width * a.height + b.width * b.height - inter
    return inter / union if union > 0 else 0.0


def _overprinted(chars: list[Char]) -> int:
    cells: dict[tuple[int, int], list[Char]] = defaultdict(list)
    for char in chars:
        if not char.text.strip() or char.off_page:
            continue
        cells[(int(char.bbox.x0 // GRID), int(char.bbox.top // GRID))].append(char)
    count = 0
    for (cx, cy), here in cells.items():
        neighbours = [
            other
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for other in cells.get((cx + dx, cy + dy), [])
        ]
        for char in here:
            if any(
                other is not char and other.text != char.text and _iou(char.bbox, other.bbox) > 0.5
                for other in neighbours
            ):
                count += 1
    return count


def _whiteout(page: Page) -> int:
    area = max(page.width * page.height, 1.0)
    boxes = [
        f.bbox
        for f in page.fills
        if f.color.lower() in _WHITE and f.bbox.width * f.bbox.height < 0.05 * area
    ]
    if not boxes:
        return 0
    count = 0
    for char in page.chars:
        if not char.text.isdigit():
            continue
        x = (char.bbox.x0 + char.bbox.x1) / 2
        y = (char.bbox.top + char.bbox.bottom) / 2
        if any(b.x0 <= x <= b.x1 and b.top <= y <= b.bottom for b in boxes):
            count += 1
    return count


def document_facts(doc: NormalizedDocument, sha256: str | None) -> DocumentFacts:
    fonts: set[str] = set()
    accents: set[str] = set()
    overprinted = 0
    whiteout = 0
    images = 0
    for page in doc.pages:
        chars = page.chars[:MAX_CHARS_PER_PAGE]
        for char in chars:
            family = font_family(char.style.font_name)
            if family:
                fonts.add(family)
        for block in page.blocks:
            family = font_family(block.style.font_name)
            if family:
                fonts.add(family)
        for fill in page.fills:
            color = fill.color.lower()
            if color not in _WHITE:
                accents.add(color)
        overprinted += _overprinted(chars)
        whiteout += _whiteout(page)
        images += page.image_count
    first = doc.pages[0] if doc.pages else None
    size = (
        f"{round(first.width)}x{round(first.height)}{first.unit}"
        if first is not None and first.width > 0
        else None
    )
    return DocumentFacts(
        media_type=doc.media_type,
        sha256=sha256,
        metadata={k: v for k, v in doc.metadata.items() if k not in {"xmp", "einvoice:data"}},
        text=doc.text,
        fonts=tuple(sorted(fonts)),
        accents=tuple(sorted(accents)),
        image_count=images,
        page_size=size,
        overprinted=overprinted,
        whiteout_digits=whiteout,
    )
