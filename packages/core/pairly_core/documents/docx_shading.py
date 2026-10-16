# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""What is painted behind DOCX text: run, paragraph, cell and table-style shading.

The hidden-text check compares a run's colour with its background. Without shading,
white text in a dark table cell (a common two-column CV template) looks like white on
white. ``Shading`` resolves the background of a run, in the order Word paints it
(the innermost wins):

1. run shading (``w:rPr/w:shd``), then run highlight (``w:highlight``);
2. paragraph shading (``w:pPr/w:shd``), direct or from the paragraph style chain;
3. table cell shading (``w:tcPr/w:shd``);
4. the table style's whole-table cell shading (``w:tblStyle`` -> ``w:tcPr/w:shd``).

A fill is ``w:fill`` (hex) or ``w:themeFill`` resolved against the document theme
(``word/theme/theme1.xml``) with ``w:themeFillTint`` / ``w:themeFillShade`` applied.
``auto``, a missing or unreadable fill means "nothing painted here" and the next
layer (finally the white page) shows through. Conditional table formats (first row,
banded rows) are not modelled; a run there falls back to the whole-table shading.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, Final
from xml.etree.ElementTree import Element

W_NS: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W: Final = f"{{{W_NS}}}"
A_NS: Final = "http://schemas.openxmlformats.org/drawingml/2006/main"
_HEX: Final = re.compile(r"^[0-9A-Fa-f]{6}$")

# w:highlight values (ST_HighlightColor).
HIGHLIGHT: Final[dict[str, str]] = {
    "black": "#000000",
    "blue": "#0000ff",
    "cyan": "#00ffff",
    "green": "#00ff00",
    "magenta": "#ff00ff",
    "red": "#ff0000",
    "yellow": "#ffff00",
    "white": "#ffffff",
    "darkBlue": "#000080",
    "darkCyan": "#008080",
    "darkGreen": "#008000",
    "darkMagenta": "#800080",
    "darkRed": "#800000",
    "darkYellow": "#808000",
    "darkGray": "#808080",
    "lightGray": "#c0c0c0",
}
# w:themeFill names -> theme colour scheme slots.
THEME_SLOTS: Final[dict[str, str]] = {
    "dark1": "dk1",
    "light1": "lt1",
    "dark2": "dk2",
    "light2": "lt2",
    "text1": "dk1",
    "background1": "lt1",
    "text2": "dk2",
    "background2": "lt2",
    "accent1": "accent1",
    "accent2": "accent2",
    "accent3": "accent3",
    "accent4": "accent4",
    "accent5": "accent5",
    "accent6": "accent6",
    "hyperlink": "hlink",
    "followedHyperlink": "folHlink",
}


def theme_colours(theme: Element | None) -> dict[str, str]:
    """Colour scheme slot -> ``#rrggbb`` from a parsed theme part."""
    colours: dict[str, str] = {}
    if theme is None:
        return colours
    scheme = theme.find(f".//{{{A_NS}}}clrScheme")
    if scheme is None:
        return colours
    for slot in scheme:
        name = slot.tag.rsplit("}", 1)[-1]
        for child in slot:
            value = child.get("val") if child.tag.endswith("srgbClr") else child.get("lastClr")
            if value and _HEX.fullmatch(value):
                colours[name] = f"#{value.lower()}"
                break
    return colours


def _adjust(colour: str, tint: str | None, shade: str | None) -> str:
    channels = [int(colour[i : i + 2], 16) for i in (1, 3, 5)]
    if tint and re.fullmatch(r"[0-9A-Fa-f]{2}", tint):
        t = int(tint, 16) / 255
        channels = [round(c * t + 255 * (1 - t)) for c in channels]
    if shade and re.fullmatch(r"[0-9A-Fa-f]{2}", shade):
        s = int(shade, 16) / 255
        channels = [round(c * s) for c in channels]
    return "#" + "".join(f"{c:02x}" for c in channels)


class Shading:
    """Resolves backgrounds for one document (theme loaded once)."""

    def __init__(self, document: Any, theme: Element | None) -> None:
        self._document = document
        self._theme = theme_colours(theme)

    def fill(self, shd: Any) -> str | None:
        """``#rrggbb`` painted by a ``w:shd`` element, or None when it paints nothing."""
        if shd is None:
            return None
        if shd.get(f"{W}val") == "nil":
            return None
        theme = shd.get(f"{W}themeFill")
        if theme:
            base = self._theme.get(THEME_SLOTS.get(theme, theme))
            if base is not None:
                return _adjust(base, shd.get(f"{W}themeFillTint"), shd.get(f"{W}themeFillShade"))
        fill = shd.get(f"{W}fill")
        if fill and _HEX.fullmatch(fill):
            return f"#{fill.lower()}"
        return None

    def _style_chain(self, style: Any) -> Iterator[Any]:
        seen = 0
        while style is not None and seen < 20:
            yield style
            style = getattr(style, "base_style", None)
            seen += 1

    def run(self, run: Any) -> str | None:
        rpr = run._r.rPr
        if rpr is None:
            return None
        found = self.fill(rpr.find(f"{W}shd"))
        if found is not None:
            return found
        highlight = rpr.find(f"{W}highlight")
        if highlight is not None:
            return HIGHLIGHT.get(highlight.get(f"{W}val", ""))
        return None

    def paragraph(self, paragraph: Any) -> str | None:
        ppr = paragraph._p.pPr
        if ppr is not None:
            found = self.fill(ppr.find(f"{W}shd"))
            if found is not None:
                return found
        for style in self._style_chain(getattr(paragraph, "style", None)):
            spr = style.element.find(f"{W}pPr")
            if spr is not None:
                found = self.fill(spr.find(f"{W}shd"))
                if found is not None:
                    return found
        return None

    def cell(self, cell: Any) -> str | None:
        tcpr = cell._tc.tcPr
        if tcpr is None:
            return None
        return self.fill(tcpr.find(f"{W}shd"))

    def table(self, table: Any) -> str | None:
        """The table style's whole-table cell shading."""
        try:
            style = table.style
        except (AttributeError, KeyError, ValueError):
            return None
        for item in self._style_chain(style):
            tcpr = item.element.find(f"{W}tcPr")
            if tcpr is not None:
                found = self.fill(tcpr.find(f"{W}shd"))
                if found is not None:
                    return found
        return None
