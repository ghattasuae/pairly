# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Draw a ``CvText`` as a scanned page: Pillow text, then scanner damage.

The page is A4 at 150 DPI, drawn with the bundled DejaVu Sans subset (Latin and
Arabic; Arabic is shaped and put in visual order first). Damage, all from the CV's
own seeded random generator: a small rotation (skew), speckle noise, a light blur and,
for JPEG, a low quality setting (block artefacts). No numpy and no unseeded noise, so
the output is reproducible.

A CV that does not fit on one page becomes a multi-page image PDF (``scan_pdf``), so
an image file never holds less than its ground truth.
"""

from __future__ import annotations

import datetime as dt
import io
import random
from dataclasses import dataclass
from functools import cache
from typing import Final

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from pairly_core.decisions.bidi import has_rtl, shape
from pairly_core.decisions.receipts import FONT_PATH
from synth.hire.text import CvText, Line

PAGE: Final = (1240, 1754)
MARGIN: Final = 90
SIDE: Final = frozenset({"contact", "personal", "skills", "languages", "certifications"})
FIXED_TIME: Final = dt.datetime(2026, 9, 1, 9, 0, 0)
_SIZES: Final = {"name": 40, "alt_name": 30, "headline": 26, "heading": 25, "entry": 21}


@cache
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


@dataclass
class ScanResult:
    data: bytes
    format: str  # png | jpg | scan_pdf
    pages: int


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    words: list[str] = []
    for word in text.split(" "):
        while font.getlength(word) > width and len(word) > 1:
            cut = len(word)
            while cut > 1 and font.getlength(word[:cut]) > width:
                cut -= 1
            words.append(word[:cut])
            word = word[cut:]
        words.append(word)
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if current and font.getlength(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


class _Pages:
    """Pages being drawn; with ``dry`` it only lays out (to count pages, no pixels)."""

    def __init__(self, rtl: bool, *, dry: bool = False) -> None:
        self.rtl = rtl
        self.dry = dry
        self.pages: list[Image.Image] = []
        self.new()

    def new(self) -> None:
        self.image = Image.new("L", (1, 1) if self.dry else PAGE, 255)
        self.draw = ImageDraw.Draw(self.image)
        self.pages.append(self.image)

    def line(self, line: Line, x0: int, x1: int, y: int) -> int:
        size = _SIZES.get(line.kind, 20)
        font = _font(size)
        indent = 22 if line.kind == "bullet" else 0
        grey = 70 if line.kind in ("dates", "contact") else 0
        if line.kind == "heading":
            y += 10
        for k, part in enumerate(_wrap(line.text, font, x1 - x0 - indent)):
            if y > PAGE[1] - MARGIN:
                self.new()
                y = MARGIN
            rtl = has_rtl(part) or self.rtl
            drawn = shape(part) if has_rtl(part) else part
            if self.dry:
                pass
            elif rtl:
                width = int(font.getlength(drawn))
                self.draw.text((x1 - indent - width, y), drawn, font=font, fill=grey)
                if line.kind == "bullet" and k == 0:
                    self.draw.text((x1 - 12, y), "\u2022", font=font, fill=grey)
            else:
                if line.kind == "bullet" and k == 0:
                    self.draw.text((x0, y), "\u2022", font=font, fill=grey)
                self.draw.text((x0 + indent, y), drawn, font=font, fill=grey)
            y += int(size * 1.35)
        if line.kind == "heading":
            if not self.dry:
                self.draw.line((x0, y - 2, x1, y - 2), fill=110, width=2)
            y += 6
        return y


def _damage(page: Image.Image, rng: random.Random, *, jpeg: bool) -> Image.Image:
    angle = rng.uniform(-1.6, 1.6)
    out = page.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=255)
    draw = ImageDraw.Draw(out)
    for _ in range(rng.randint(600, 1800)):
        x, y = rng.randrange(PAGE[0]), rng.randrange(PAGE[1])
        draw.point((x, y), fill=rng.randint(0, 140))
    for _ in range(rng.randint(0, 3)):
        y = rng.randrange(PAGE[1])
        draw.line((0, y, PAGE[0], y + rng.randint(-3, 3)), fill=rng.randint(200, 235), width=1)
    radius = rng.choice((0.0, 0.4, 0.6, 0.8))
    if radius:
        out = out.filter(ImageFilter.GaussianBlur(radius))
    del jpeg
    return out


def _lay_out(text: CvText, layout: str, *, dry: bool) -> list[Image.Image]:
    rtl = text.language == "ar"
    pages = _Pages(rtl, dry=dry)
    if layout == "two_column":
        header = [ln for ln in text.lines if ln.section == "header"]
        side = [ln for ln in text.lines if ln.section in SIDE]
        body = [ln for ln in text.lines if ln.section not in SIDE and ln.section != "header"]
        y = MARGIN
        for ln in header:
            y = pages.line(ln, MARGIN, PAGE[0] - MARGIN, y)
        top = y + 10
        y_side = top
        for ln in side:
            y_side = pages.line(ln, MARGIN, 420, y_side)
            if len(pages.pages) > 1:
                raise RuntimeError("side column overflowed")
        y = top
        for ln in body:
            y = pages.line(ln, 460, PAGE[0] - MARGIN, y)
    else:
        y = MARGIN
        for ln in text.lines:
            y = pages.line(ln, MARGIN, PAGE[0] - MARGIN, y)
    return pages.pages


def scan_format(text: CvText, layout: str, *, jpeg: bool) -> str:
    """The file format ``render_scan`` produces, without drawing (for manifests)."""
    if len(_lay_out(text, layout, dry=True)) > 1:
        return "scan_pdf"
    return "jpg" if jpeg else "png"


def render_scan(text: CvText, layout: str, rng: random.Random, *, jpeg: bool) -> ScanResult:
    damaged = [_damage(p, rng, jpeg=jpeg) for p in _lay_out(text, layout, dry=False)]
    buf = io.BytesIO()
    if len(damaged) > 1:
        first, *rest = damaged
        first.save(
            buf,
            format="PDF",
            resolution=150.0,
            save_all=True,
            append_images=rest,
            creationDate=FIXED_TIME,
            modDate=FIXED_TIME,
        )
        return ScanResult(buf.getvalue(), "scan_pdf", len(damaged))
    if jpeg:
        damaged[0].save(buf, format="JPEG", quality=rng.randint(35, 70), optimize=False)
        return ScanResult(buf.getvalue(), "jpg", 1)
    damaged[0].save(buf, format="PNG", optimize=False)
    return ScanResult(buf.getvalue(), "png", 1)
