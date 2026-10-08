# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Renderer-based visibility check for PDF text (runs inside the parse sandbox).

The colour checks in the guardrails compare a character's fill colour with the filled
rectangle behind it. That misses text a reader cannot see for other reasons: an
opaque shape or image drawn on top of it, or a clipping path that cuts it away.
pdfplumber reports neither drawing order nor clips, so this pass asks the renderer:

1. render the page with pypdfium2 (already used for OCR, no new dependency) at
   ``ParseOptions.render_dpi``, as an 8-bit grey image;
2. for every visible character, take the pixels under its box and compute the
   contrast between the darkest and the lightest of them (WCAG ratio, 1.0 means one
   flat colour: nothing was drawn there that a person could see);
3. keep that ratio on the character (``Char.render_contrast``) when it is low
   (below ``REPORT_BELOW``), so the document JSON stays small.

The guardrails then flag characters whose rendered contrast is low although their
fill colour looks fine (``hidden_text.covered_or_clipped``).

Cost is bounded: a page cap (``render_max_pages``), a time budget for the whole
document (``render_seconds``), a pixel cap (``max_image_pixels``, the DPI is lowered
to fit, and below ``MIN_DPI`` the page is not rendered). Every page records what
happened in ``Page.render``. Anything other than ``ok`` (or ``disabled`` by
configuration) on a page with text makes the guardrails record a detector error, so
the document goes to a person (fail closed). The sandbox wall clock still bounds a
single slow page.

Known limit: text covered by a shape and then overprinted with other visible text at
the same place still shows contrast, so it is not caught here.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from pairly_core.documents.model import BBox, RenderCheck
from pairly_core.documents.registry import ParseOptions

# Only ratios below this are stored on the character (the guardrail threshold is lower).
REPORT_BELOW: Final = 3.0
# Rendering below this resolution would blur small text into its background.
MIN_DPI: Final = 48.0
# Boxes narrower or shorter than this (points) cannot be measured: a glyph whose width
# the font does not report gets a zero-width box although it is drawn.
MIN_BOX_PT: Final = 0.75


def _linear(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


_LUMINANCE: Final = tuple(_linear(v) for v in range(256))


def grey_contrast(darkest: int, lightest: int) -> float:
    """WCAG contrast ratio between two 8-bit grey levels."""
    lo, hi = _LUMINANCE[darkest], _LUMINANCE[lightest]
    return (max(lo, hi) + 0.05) / (min(lo, hi) + 0.05)


@dataclass
class RenderBudget:
    """The per-document budget: pages rendered so far and the deadline."""

    options: ParseOptions
    started: float
    pages: int = 0

    @classmethod
    def start(cls, options: ParseOptions) -> RenderBudget:
        return cls(options=options, started=time.monotonic())

    def exhausted(self) -> bool:
        return time.monotonic() - self.started > self.options.render_seconds


class PageRenderer:
    """Opens the PDF once with pypdfium2 and renders pages on demand."""

    def __init__(self, path: Any, password: str | None, options: ParseOptions) -> None:
        self._path = path
        self._password = password
        self._options = options
        self._doc: Any = None
        self.budget = RenderBudget.start(options)

    def _document(self) -> Any:
        if self._doc is None:
            import pypdfium2  # type: ignore[import-untyped]

            self._doc = pypdfium2.PdfDocument(self._path, password=self._password)
        return self._doc

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    def check(
        self,
        index: int,
        cropbox: tuple[float, float, float, float],
        boxes: Sequence[BBox | None],
    ) -> tuple[RenderCheck, dict[int, float]]:
        """Rendered contrast for each box (``None`` boxes are skipped).

        Returns the page status and ``{box index: ratio}`` for ratios below
        ``REPORT_BELOW``. Never raises: a failure is a status, not an exception.
        """
        options = self._options
        if not options.render_check:
            return RenderCheck(status="disabled"), {}
        if not any(b is not None for b in boxes):
            return RenderCheck(status="no_text"), {}
        if self.budget.pages >= options.render_max_pages:
            return RenderCheck(status="page_cap"), {}
        if self.budget.exhausted():
            return RenderCheck(status="timeout"), {}
        x0, top, x1, bottom = cropbox
        width, height = max(x1 - x0, 0.0), max(bottom - top, 0.0)
        if width <= 0 or height <= 0:
            return RenderCheck(status="error"), {}
        dpi = float(options.render_dpi)
        pixels = (width * dpi / 72) * (height * dpi / 72)
        if pixels > options.max_image_pixels:
            dpi = 72 * math.sqrt(options.max_image_pixels / (width * height))
            if dpi < MIN_DPI:
                return RenderCheck(status="too_large"), {}
        self.budget.pages += 1
        try:
            page = self._document().get_page(index)
            try:
                bitmap = page.render(scale=dpi / 72, grayscale=True, may_draw_forms=True)
                image = bitmap.to_pil().convert("L")
            finally:
                page.close()
        except Exception:
            return RenderCheck(status="error", dpi=round(dpi, 2)), {}
        scale = image.size[0] / width
        low: dict[int, float] = {}
        img_w, img_h = image.size
        for i, box in enumerate(boxes):
            if box is None or box.width < MIN_BOX_PT or box.height < MIN_BOX_PT:
                continue
            left = math.floor((box.x0 - x0) * scale)
            upper = math.floor((box.top - top) * scale)
            right = math.ceil((box.x1 - x0) * scale)
            lower = math.ceil((box.bottom - top) * scale)
            left, upper = max(left, 0), max(upper, 0)
            right, lower = min(right, img_w), min(lower, img_h)
            if right <= left or lower <= upper:
                # Nothing of it is on the rendered page: off-page text has its own check.
                continue
            darkest, lightest = image.crop((left, upper, right, lower)).getextrema()
            ratio = grey_contrast(int(darkest), int(lightest))
            if ratio < REPORT_BELOW:
                low[i] = round(ratio, 3)
        return RenderCheck(status="ok", dpi=round(dpi, 2)), low
