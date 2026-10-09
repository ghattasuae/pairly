# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``NormalizedDocument``: the one shape every parser produces.

Coordinates: origin at the top-left of the page, x to the right, y down. PDF pages use
points (1/72 inch), images use pixels; ``Page.unit`` says which. DOCX has no layout,
so its blocks carry no ``bbox`` and the single logical page has the section size.

Everything a later check needs to spot hidden or misleading content is kept as
observed, not interpreted: per character and per word font name, size, fill and stroke
colour, whether a box lies outside the page, the DOCX ``vanish`` (hidden) attribute,
and raw metadata (PDF Info, XMP, DOCX core and custom properties). The guardrails
(WP1.4) decide what is suspicious.

Colours are normalised to ``#rrggbb`` when the colour space is gray, RGB or CMYK; a
pattern or unknown colour space is kept as ``pattern:<name>`` or ``None``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BBox(_Model):
    x0: float
    top: float
    x1: float
    bottom: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.bottom - self.top

    def outside(self, width: float, height: float) -> bool:
        """True when the box lies wholly or partly outside a ``width`` x ``height`` page."""
        return self.x0 < 0 or self.top < 0 or self.x1 > width or self.bottom > height


class Style(_Model):
    font_name: str | None = None
    font_size: float | None = None
    fill_color: str | None = None
    stroke_color: str | None = None
    bold: bool | None = None
    italic: bool | None = None
    # DOCX ``w:vanish`` / ``w:specVanish``: hidden text.
    hidden: bool = False
    # PDF text rendering mode 3 (neither fill nor stroke): invisible text.
    invisible: bool = False


class Char(_Model):
    text: str
    bbox: BBox
    style: Style
    off_page: bool = False
    # Offset of the character's text in ``Page.text`` (PDF, see ``documents.pdf_text``).
    offset: int | None = None
    # Contrast between the darkest and lightest rendered pixels under the character
    # (WCAG ratio, 1.0 = one flat colour), kept only when low; see ``documents.render``.
    render_contrast: float | None = None


class Word(_Model):
    text: str
    bbox: BBox | None = None
    style: Style = Field(default_factory=Style)
    off_page: bool = False
    # OCR confidence 0 to 100, None for born-digital text.
    confidence: float | None = None


Region = Literal["body", "header", "footer", "table", "textbox", "footnote", "comment"]
TextSource = Literal["pdf", "ocr", "docx", "text", "vision"]


class TextBlock(_Model):
    """A line (PDF, OCR) or a run (DOCX): text with uniform style where possible."""

    text: str
    bbox: BBox | None = None
    style: Style = Field(default_factory=Style)
    source: TextSource
    region: Region = "body"
    off_page: bool = False
    confidence: float | None = None
    # Index of the DOCX paragraph or OCR line the block belongs to.
    group: int | None = None
    # DOCX: the shading painted behind the run (``#rrggbb``), None for the page itself.
    background: str | None = None


class Fill(_Model):
    """A filled rectangle on a PDF page (a background, a band, a box over text).

    Kept so hidden-text checks can compare a character's colour with what is behind it
    instead of assuming a white page. Drawing order is not recorded.
    """

    bbox: BBox
    color: str


RenderStatus = Literal["ok", "disabled", "no_text", "page_cap", "timeout", "too_large", "error"]
# Statuses that leave text on the page unchecked (the guardrails then fail closed).
RENDER_INCOMPLETE: frozenset[str] = frozenset({"page_cap", "timeout", "too_large", "error"})


class RenderCheck(_Model):
    """What the renderer-based visibility check did on a PDF page."""

    status: RenderStatus
    dpi: float | None = None


class Table(_Model):
    bbox: BBox | None = None
    rows: list[list[str | None]]


class OcrInfo(_Model):
    engine: str
    languages: str
    mean_confidence: float | None
    words: int
    dpi: int | None = None


class Page(_Model):
    number: int = Field(ge=1)
    width: float
    height: float
    unit: Literal["pt", "px"]
    rotation: int = 0
    text: str = ""
    blocks: list[TextBlock] = Field(default_factory=list)
    words: list[Word] = Field(default_factory=list)
    chars: list[Char] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    # Filled rectangles (PDF only), capped per page; see ``Fill``.
    fills: list[Fill] = Field(default_factory=list)
    ocr: OcrInfo | None = None
    image_count: int = 0
    # PDF only: the renderer-based visibility check (``documents.render``).
    render: RenderCheck | None = None


class ParseWarning(_Model):
    """A non-fatal finding. Any warning in ``REVIEW_WARNINGS`` sends the document to review."""

    code: str
    message: str
    page: int | None = None


class ParserInfo(_Model):
    name: str
    version: str
    libraries: dict[str, str] = Field(default_factory=dict)


class NormalizedDocument(_Model):
    schema_version: str = SCHEMA_VERSION
    media_type: str
    parser: ParserInfo
    pages: list[Page]
    metadata: dict[str, str] = Field(default_factory=dict)
    warnings: list[ParseWarning] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def warning_codes(self) -> list[str]:
        return sorted({w.code for w in self.warnings})

    def ocr_confidence(self) -> float | None:
        """Mean OCR confidence over pages that were OCR'd, weighted by word count."""
        pages = [
            p.ocr for p in self.pages if p.ocr is not None and p.ocr.mean_confidence is not None
        ]
        words = sum(p.words for p in pages)
        if not pages or words == 0:
            return None
        return sum((p.mean_confidence or 0.0) * p.words for p in pages) / words


# Warnings that route the document to the human queue (fail closed: a partial or
# low-quality result is never silently treated as complete).
REVIEW_WARNINGS = frozenset(
    {
        "pages_truncated",
        "chars_truncated",
        "ocr_unavailable",
        "ocr_language_missing",
        "low_ocr_confidence",
        "encrypted",
        "active_content",
        "embedded_files",
        "page_error",
        "image_too_large",
        "no_text",
        "unsafe_xml",
    }
)
