# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Split a ``NormalizedDocument`` into citable chunks: by headings, then paragraphs.

Each chunk keeps the pages it spans and the section path it sits under (for example
``3 Purchasing > 3.2 Software``), which is what a citation shows. Chunks never cross a
heading. A paragraph longer than the maximum is split at sentence ends.

Only visible body text is indexed: hidden text (DOCX ``vanish``, PDF invisible render
mode), boxes outside the page, and page headers and footers are dropped, so text a
reader cannot see can never become a cited policy requirement.

Heading detection is heuristic (the normalized model keeps style, not semantics): a
short line without a closing full stop that is numbered (``2.3 Software``, ``Section 4``,
``المادة 5``), or entirely bold, or set clearly larger than the body text.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Final

from pairly_core.documents.model import NormalizedDocument, Page, TextBlock
from pairly_core.retrieval.search import ChunkInput

TARGET_CHARS: Final = 1200
MAX_CHARS: Final = 2000
MAX_HEADING_CHARS: Final = 120
MAX_SECTION_CHARS: Final = 500

_NUMBERED_RE = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,4})\.?\s+\S")
_WORD_HEADING_RE = re.compile(
    r"^(section|article|part|chapter|schedule|appendix|annex|clause|"
    r"\u0627\u0644\u0645\u0627\u062f\u0629|\u0627\u0644\u0642\u0633\u0645|"
    r"\u0627\u0644\u0641\u0635\u0644|\u0627\u0644\u0628\u0627\u0628)\b",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"(?<=[.!?\u061f\u06d4])\s+")
_SKIP_REGIONS: Final = frozenset({"header", "footer", "comment"})


@dataclass
class Paragraph:
    text: str
    page: int
    font_size: float | None = None
    bold: bool = False
    level: int = 0  # 0 = body text, 1.. = heading level


@dataclass
class _Builder:
    chunks: list[ChunkInput] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    section: str | None = None

    def flush(self) -> None:
        if self.parts:
            self.chunks.append(
                ChunkInput(
                    text="\n".join(self.parts),
                    page_start=min(self.pages),
                    page_end=max(self.pages),
                    section=self.section,
                )
            )
        self.parts, self.pages = [], []

    def size(self) -> int:
        return sum(len(p) + 1 for p in self.parts)

    def add(self, text: str, page: int) -> None:
        self.parts.append(text)
        self.pages.append(page)


def _visible(block: TextBlock) -> bool:
    return not (
        block.style.hidden
        or block.style.invisible
        or block.off_page
        or block.region in _SKIP_REGIONS
        or not block.text.strip()
    )


def _paragraphs_of_page(page: Page) -> list[Paragraph]:
    blocks = [b for b in page.blocks if _visible(b)]
    if not blocks:
        return [
            Paragraph(text=" ".join(p.split()), page=page.number)
            for p in re.split(r"\n\s*\n", page.text)
            if p.strip()
        ]
    out: list[Paragraph] = []
    current: list[TextBlock] = []

    def close() -> None:
        if not current:
            return
        text = " ".join(" ".join(b.text.split()) for b in current).strip()
        if text:
            sizes = [b.style.font_size for b in current if b.style.font_size]
            out.append(
                Paragraph(
                    text=text,
                    page=page.number,
                    font_size=max(sizes) if sizes else None,
                    bold=all(b.style.bold for b in current),
                )
            )
        current.clear()

    for block in blocks:
        if current:
            prev = current[-1]
            if block.group is not None or prev.group is not None:
                new_paragraph = block.group != prev.group
            elif block.bbox is not None and prev.bbox is not None:
                height = max(prev.bbox.height, 1.0)
                gap = block.bbox.top - prev.bbox.bottom
                new_paragraph = (
                    gap > 0.8 * height
                    or block.style.font_size != prev.style.font_size
                    or block.style.bold != prev.style.bold
                )
            else:
                new_paragraph = True
            if new_paragraph:
                close()
        current.append(block)
    close()
    return out


def _heading_level(paragraph: Paragraph, body_size: float | None) -> int:
    text = paragraph.text
    if len(text) > MAX_HEADING_CHARS or len(text.split()) > 14:
        return 0
    if text.endswith((".", ":", ";", ",", "\u060c")) and not _NUMBERED_RE.match(text):
        return 0
    numbered = _NUMBERED_RE.match(text)
    if numbered:
        # "2.3 Software" is a heading only if it does not read like a sentence.
        if text.rstrip().endswith(".") or len(text.split()) > 10:
            return 0
        return numbered.group("num").count(".") + 1
    if _WORD_HEADING_RE.match(text):
        return 1
    larger = (
        body_size is not None
        and paragraph.font_size is not None
        and paragraph.font_size >= body_size * 1.15
    )
    if paragraph.bold or larger:
        return 1
    return 0


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_RE.split(text):
        while len(sentence) > MAX_CHARS:  # no sentence breaks: hard split
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:MAX_CHARS])
            sentence = sentence[MAX_CHARS:]
        if current and len(current) + 1 + len(sentence) > TARGET_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def paragraphs(document: NormalizedDocument) -> list[Paragraph]:
    paras = [p for page in document.pages for p in _paragraphs_of_page(page)]
    sizes = [p.font_size for p in paras if p.font_size and len(p.text) > MAX_HEADING_CHARS]
    if not sizes:
        sizes = [p.font_size for p in paras if p.font_size]
    body_size = statistics.median(sizes) if sizes else None
    for p in paras:
        p.level = _heading_level(p, body_size)
    return paras


def chunk_document(document: NormalizedDocument) -> list[ChunkInput]:
    """Chunks with page ranges and section paths, in reading order."""
    builder = _Builder()
    stack: list[tuple[int, str]] = []
    for para in paragraphs(document):
        if para.level:
            builder.flush()
            while stack and stack[-1][0] >= para.level:
                stack.pop()
            stack.append((para.level, para.text))
            builder.section = " > ".join(t for _, t in stack)[:MAX_SECTION_CHARS]
            builder.add(para.text, para.page)
            continue
        for piece in _split_long(para.text):
            if builder.parts and builder.size() + len(piece) > TARGET_CHARS:
                # Keep only a heading-only chunk open: it belongs with its first paragraph.
                heading_only = (
                    len(builder.parts) == 1 and stack and builder.parts[0] == stack[-1][1]
                )
                if not heading_only or builder.size() + len(piece) > MAX_CHARS:
                    builder.flush()
            builder.add(piece, para.page)
    builder.flush()
    return builder.chunks
