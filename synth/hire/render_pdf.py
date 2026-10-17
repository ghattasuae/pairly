# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Draw a ``CvText`` as a PDF with reportlab, in one of several layouts.

Layouts: ``single_column``, ``two_column`` (contact, skills and languages in a narrow
left column), ``sidebar`` (the same, on a dark band with white text: a clean control
for the hidden-text checks), ``table`` (experience in a bordered grid with a shaded
header row), ``arabic`` (right to left) and ``bilingual`` (English with Arabic lines).

Arabic lines are shaped and put in visual order (``pairly_core.decisions.bidi``) and
drawn with the bundled DejaVu Sans subset, as most PDF producers do: the text layer
then holds presentation forms in visual order (``text_layer = visual_shaped``).

``extra`` draws on the first page after the content (attack overlays); ``metadata``
sets the Info dictionary. Output is deterministic (``invariant=1``).
"""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from pairly_core.decisions.bidi import has_rtl, shape
from pairly_core.decisions.receipts import FONT_PATH
from synth.hire.text import CvText, Line

Draw = Callable[[canvas.Canvas], None]
RGB = tuple[float, float, float]

ARABIC_FONT: Final = "PairlySynthDejaVu"
WIDTH, HEIGHT = A4
MARGIN: Final = 50.0
BOTTOM: Final = 72.0
SIDE_SECTIONS: Final = frozenset({"contact", "personal", "skills", "languages", "certifications"})
SIDEBAR_RGB: Final = (0.12, 0.18, 0.27)


def _arabic_font() -> str:
    if ARABIC_FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(ARABIC_FONT, str(FONT_PATH)))
    return ARABIC_FONT


@dataclass(frozen=True)
class PdfStyle:
    accent: RGB = (0.1, 0.2, 0.45)
    grey_dates: bool = False
    footer: bool = True
    body_size: float = 10.0


@dataclass
class _Column:
    x0: float
    x1: float
    y: float
    rtl: bool = False
    text_rgb: RGB = (0, 0, 0)
    muted_rgb: RGB = (0.4, 0.4, 0.4)
    accent: RGB = (0.1, 0.2, 0.45)


_SIZES: Final = {
    "name": (20.0, True),
    "alt_name": (15.0, False),
    "headline": (12.0, False),
    "heading": (11.5, True),
    "entry": (10.0, True),
}


def _split_long(word: str, font: str, size: float, width: float) -> list[str]:
    """Break a word wider than the column (an email address) into pieces that fit."""
    pieces: list[str] = []
    current = ""
    for ch in word:
        if current and pdfmetrics.stringWidth(current + ch, font, size) > width:
            pieces.append(current)
            current = ch
        else:
            current += ch
    return [*pieces, current] if current else pieces


def wrap(text: str, font: str, size: float, width: float) -> list[str]:
    words: list[str] = []
    for word in text.split(" "):
        if pdfmetrics.stringWidth(word, font, size) > width:
            words.extend(_split_long(word, font, size, width))
        else:
            words.append(word)
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if current and pdfmetrics.stringWidth(candidate, font, size) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


@dataclass
class PdfCv:
    text: CvText
    layout: str
    style: PdfStyle = field(default_factory=PdfStyle)
    photo: bool = False
    extra: Draw | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    author: str | None = None

    def render(self) -> bytes:
        buf = io.BytesIO()
        pdf = canvas.Canvas(buf, pagesize=A4, invariant=1)
        pdf.setTitle(self.metadata.get("title", "Curriculum vitae"))
        pdf.setAuthor(self.author or "")
        if self.metadata.get("subject"):
            pdf.setSubject(self.metadata["subject"])
        if self.metadata.get("keywords"):
            pdf.setKeywords(self.metadata["keywords"])
        pdf.setCreator("Pairly synthetic CV generator")
        self._pdf = pdf
        self._page = 1
        rtl = self.text.language == "ar"
        two = self.layout in ("two_column", "sidebar")
        self._page_start()
        if two:
            side = _Column(MARGIN - 20, 190, HEIGHT - 60, rtl=rtl, accent=self.style.accent)
            main = _Column(210, WIDTH - MARGIN, HEIGHT - 60, rtl=rtl, accent=self.style.accent)
            if self.layout == "sidebar":
                side.text_rgb = (1, 1, 1)
                side.muted_rgb = (0.85, 0.88, 0.92)
                side.accent = (0.95, 0.8, 0.3)
            side_lines = [ln for ln in self.text.lines if ln.section in SIDE_SECTIONS]
            main_lines = [ln for ln in self.text.lines if ln.section not in SIDE_SECTIONS]
            header = [ln for ln in main_lines if ln.section == "header"]
            body = [ln for ln in main_lines if ln.section != "header"]
            self._flow(main, header, allow_break=False)
            if self.photo:
                side.y = HEIGHT - 165
            self._flow(side, side_lines, allow_break=False)
            self._flow(main, body, allow_break=True)
        else:
            col = _Column(MARGIN, WIDTH - MARGIN, HEIGHT - 60, rtl=rtl, accent=self.style.accent)
            if self.photo:
                col.x1 = WIDTH - MARGIN - (90 if not rtl else 0)
                col.x0 = MARGIN + (90 if rtl else 0)
            header = [ln for ln in self.text.lines if ln.section in ("header", "contact")]
            body = [ln for ln in self.text.lines if ln.section not in ("header", "contact")]
            self._flow(col, header, allow_break=False, compact_contact=True)
            col.x0, col.x1 = MARGIN, WIDTH - MARGIN
            col.y = min(col.y, HEIGHT - 162) if self.photo else col.y
            if self.layout == "table":
                self._flow_table(col, body)
            else:
                self._flow(col, body, allow_break=True)
        self._page_end()
        pdf.save()
        return buf.getvalue()

    # ------------------------------------------------------------------ pages

    def _page_start(self) -> None:
        pdf = self._pdf
        if self.layout == "sidebar":
            pdf.setFillColorRGB(*SIDEBAR_RGB)
            pdf.rect(0, 0, 200, HEIGHT, stroke=0, fill=1)
            pdf.setFillColorRGB(0, 0, 0)
        if self.photo and self._page == 1:
            self._draw_photo()

    def _page_end(self) -> None:
        pdf = self._pdf
        if self._page == 1 and self.extra is not None:
            pdf.saveState()
            self.extra(pdf)  # noqa: S610 (a drawing callback, not Django)
            pdf.restoreState()
        if self.style.footer:
            pdf.setFont("Helvetica", 8)
            pdf.setFillColorRGB(0.55, 0.55, 0.55)
            pdf.drawRightString(WIDTH - MARGIN, 30, f"Page {self._page}")
            pdf.setFillColorRGB(0, 0, 0)

    def _new_page(self, col: _Column) -> None:
        self._page_end()
        self._pdf.showPage()
        self._page += 1
        self._page_start()
        col.y = HEIGHT - 60

    def _draw_photo(self) -> None:
        pdf = self._pdf
        rtl = self.text.language == "ar"
        x = MARGIN if rtl else WIDTH - MARGIN - 80
        if self.layout in ("two_column", "sidebar"):
            x = 60
        y = HEIGHT - 150
        pdf.setFillColorRGB(0.82, 0.84, 0.87)
        pdf.roundRect(x, y, 80, 95, 6, stroke=0, fill=1)
        pdf.setFillColorRGB(0.6, 0.63, 0.68)
        pdf.circle(x + 40, y + 62, 17, stroke=0, fill=1)
        pdf.ellipse(x + 14, y + 6, x + 66, y + 42, stroke=0, fill=1)
        pdf.setFillColorRGB(0, 0, 0)

    # ------------------------------------------------------------------ lines

    def _font(self, line: Line, text: str) -> tuple[str, float]:
        size, bold = _SIZES.get(line.kind, (self.style.body_size, False))
        # Helvetica covers Latin-1 only: an Arabic comma alone needs the DejaVu font too.
        if has_rtl(text) or any(ord(c) > 0xFF for c in text):
            return _arabic_font(), size
        return ("Helvetica-Bold" if bold else "Helvetica"), size

    def _colour(self, col: _Column, line: Line) -> RGB:
        if line.kind == "heading":
            return col.accent
        if line.kind == "dates" and self.style.grey_dates:
            return col.muted_rgb
        if line.kind in ("contact", "headline"):
            return col.muted_rgb if col.text_rgb != (0, 0, 0) else (0.25, 0.25, 0.25)
        return col.text_rgb

    def _draw(self, col: _Column, line: Line, *, allow_break: bool) -> None:
        pdf = self._pdf
        font, size = self._font(line, line.text)
        rtl = has_rtl(line.text)
        indent = 12.0 if line.kind == "bullet" else 0.0
        width = col.x1 - col.x0 - indent
        leading = size * 1.3
        if line.kind == "heading":
            col.y -= 6
        for k, part in enumerate(wrap(line.text, font, size, width)):
            if col.y < BOTTOM:
                if not allow_break:
                    raise RuntimeError("a fixed column overflowed its page")
                self._new_page(col)
            pdf.setFont(font, size)
            pdf.setFillColorRGB(*self._colour(col, line))
            if rtl or col.rtl:
                shaped = shape(part) if rtl else part
                right = col.x1 - (indent if line.kind == "bullet" else 0)
                pdf.drawRightString(right, col.y, shaped)
                if line.kind == "bullet" and k == 0:
                    pdf.setFont(_arabic_font(), size)
                    pdf.drawRightString(col.x1, col.y, "\u2022")
            else:
                if line.kind == "bullet" and k == 0:
                    pdf.setFont(_arabic_font(), size)
                    pdf.drawString(col.x0, col.y, "\u2022")
                    pdf.setFont(font, size)
                pdf.drawString(col.x0 + indent, col.y, part)
            col.y -= leading
        if line.kind == "heading":
            pdf.setStrokeColorRGB(*col.accent)
            pdf.setLineWidth(0.6)
            pdf.line(col.x0, col.y + leading - 4, col.x1, col.y + leading - 4)
            pdf.setStrokeColorRGB(0, 0, 0)
            col.y -= 2
        pdf.setFillColorRGB(0, 0, 0)

    def _flow(
        self,
        col: _Column,
        lines: Sequence[Line],
        *,
        allow_break: bool,
        compact_contact: bool = False,
    ) -> None:
        pending: list[Line] = []
        for line in lines:
            if compact_contact and line.kind == "contact":
                pending.append(line)
                continue
            if pending:
                self._contact_row(col, pending)
                pending = []
            self._draw(col, line, allow_break=allow_break)
        if pending:
            self._contact_row(col, pending)
        col.y -= 4

    def _contact_row(self, col: _Column, lines: list[Line]) -> None:
        joined = "  |  ".join(ln.text for ln in lines)
        self._draw(col, Line(joined, "contact", "contact"), allow_break=False)

    def _flow_table(self, col: _Column, lines: Sequence[Line]) -> None:
        """Experience as a grid (period | role and achievements); the rest as text."""
        pdf = self._pdf
        exp = [ln for ln in lines if ln.section == "experience"]
        before = [ln for ln in lines if ln.section == "summary"]
        after = [ln for ln in lines if ln.section not in ("summary", "experience")]
        self._flow(col, before, allow_break=True)
        heading = next((ln for ln in exp if ln.kind == "heading"), None)
        if heading is not None:
            self._draw(col, heading, allow_break=True)
        # Entries come before their dates in the text: pair each entry with its dates.
        paired: list[tuple[Line, list[Line]]] = []
        current: list[Line] = []
        for ln in exp:
            if ln.kind == "entry":
                current = [ln]
                paired.append((Line("", "dates", "experience"), current))
            elif ln.kind == "dates" and paired:
                paired[-1] = (ln, paired[-1][1])
            elif ln.kind == "bullet" and paired:
                current.append(ln)
        left_w = 105.0
        header_h = 16.0
        if col.y - header_h < BOTTOM:
            self._new_page(col)
        pdf.setFillColorRGB(0.9, 0.91, 0.93)
        pdf.rect(col.x0, col.y - header_h + 11, col.x1 - col.x0, header_h, stroke=0, fill=1)
        pdf.setFillColorRGB(0.15, 0.15, 0.15)
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(col.x0 + 4, col.y - 1, "Period")
        pdf.drawString(col.x0 + left_w + 4, col.y - 1, "Role and achievements")
        col.y -= header_h + 2
        for dates, cells in paired:
            needed = 0.0
            parts: list[tuple[Line, list[str]]] = []
            for ln in cells:
                font, size = self._font(ln, ln.text)
                indent = 10.0 if ln.kind == "bullet" else 0.0
                chunk = wrap(ln.text, font, size, col.x1 - col.x0 - left_w - 10 - indent)
                parts.append((ln, chunk))
                needed += len(chunk) * size * 1.3
            if col.y - needed < BOTTOM:
                self._new_page(col)
            top = col.y + 11
            pdf.setFont("Helvetica", 9)
            pdf.setFillColorRGB(0.3, 0.3, 0.3)
            pdf.drawString(col.x0 + 4, col.y, dates.text)
            y = col.y
            for ln, chunk in parts:
                font, size = self._font(ln, ln.text)
                indent = 10.0 if ln.kind == "bullet" else 0.0
                pdf.setFillColorRGB(0, 0, 0)
                for k, piece in enumerate(chunk):
                    if ln.kind == "bullet" and k == 0:
                        pdf.setFont(_arabic_font(), size)
                        pdf.drawString(col.x0 + left_w + 4, y, "\u2022")
                    pdf.setFont(font, size)
                    pdf.drawString(col.x0 + left_w + 4 + indent, y, piece)
                    y -= size * 1.3
            bottom = y + 6
            pdf.setStrokeColorRGB(0.7, 0.7, 0.7)
            pdf.setLineWidth(0.5)
            pdf.rect(col.x0, bottom, col.x1 - col.x0, top - bottom, stroke=1, fill=0)
            pdf.line(col.x0 + left_w, bottom, col.x0 + left_w, top)
            pdf.setStrokeColorRGB(0, 0, 0)
            col.y = y - 8
        self._flow(col, after, allow_break=True)


def render_pdf(text: CvText, layout: str, **kwargs: Any) -> bytes:
    return PdfCv(text, layout, **kwargs).render()
