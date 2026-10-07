# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Hidden text: content a person does not see but a parser (and a model) reads.

PDF, per character:

* fill colour too close to what is behind it. The background is the smallest filled
  rectangle under the character's centre, or white when there is none. Closeness is the
  WCAG contrast ratio (1.0 for identical colours, 21 for black on white);
* text rendering mode 3 (neither filled nor stroked: invisible);
* a font smaller than ``min_font_pt``;
* a centre outside the page (off-page, or cut off by the crop box).

DOCX, per run: ``w:vanish`` / ``w:specVanish`` (hidden), white or near-white colour,
a theme background colour, a tiny font.

Clipping paths and text covered by an opaque shape drawn on top are not modelled:
pdfplumber does not report drawing order or clips. Those cases are left to the
instruction and stuffing detectors, which read the same text.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from pairly_core.documents.model import BBox, Char, Page, Style
from pairly_core.guardrails.base import ScanInput, evidence, union_bbox
from pairly_core.guardrails.report import Finding, FindingType, Severity, Span
from pairly_core.guardrails.textindex import PageIndex

WHITE = "#ffffff"
# Theme colours that render as the page background (python-docx names, folded).
BACKGROUND_THEMES = frozenset({"background_1", "light_1", "bg1", "lt1", "background1"})
# Most severe first: the finding's code is the first reason that applies.
_PRIORITY: tuple[str, ...] = (
    "invisible_render_mode",
    "low_contrast",
    "background_colour",
    "docx_hidden",
    "off_page",
    "tiny_font",
)


def _linear(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def contrast_ratio(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _is_hex(colour: str | None) -> bool:
    return (
        colour is not None
        and len(colour) == 7
        and colour.startswith("#")
        and all(c in "0123456789abcdef" for c in colour[1:].lower())
    )


def background_at(page: Page, x: float, y: float) -> str:
    """Colour of the smallest filled rectangle containing (x, y), else white."""
    best: tuple[float, str] | None = None
    for fill in page.fills:
        box = fill.bbox
        if box.x0 <= x <= box.x1 and box.top <= y <= box.bottom:
            area = box.width * box.height
            if best is None or area < best[0]:
                best = (area, fill.color)
    return best[1] if best else WHITE


@dataclass
class _Reasons:
    codes: dict[str, Severity] = field(default_factory=dict)
    details: dict[str, str | float | int | bool] = field(default_factory=dict)

    def add(self, code: str, severity: Severity) -> None:
        current = self.codes.get(code)
        if current is None or severity.rank > current.rank:
            self.codes[code] = severity

    def merge(self, other: _Reasons) -> None:
        for code, severity in other.codes.items():
            self.add(code, severity)
        for key, value in other.details.items():
            self.details.setdefault(key, value)

    @property
    def severity(self) -> Severity:
        return max(self.codes.values(), key=lambda s: s.rank)

    @property
    def code(self) -> str:
        return next(c for c in _PRIORITY if c in self.codes)


def _style_reasons(style: Style, background: str, scan: ScanInput) -> _Reasons:
    cfg = scan.config
    reasons = _Reasons()
    if style.invisible:
        reasons.add("invisible_render_mode", Severity.HIGH)
    if style.hidden:
        reasons.add("docx_hidden", Severity.HIGH)
    size = style.font_size
    if size is not None and 0 <= size < cfg.min_font_pt:
        high = size < cfg.tiny_font_high_below_pt
        reasons.add("tiny_font", Severity.HIGH if high else Severity.MEDIUM)
        reasons.details["font_size_pt"] = round(size, 2)
    colour = style.fill_color
    if colour is not None and _is_hex(colour):
        ratio = contrast_ratio(colour.lower(), background)
        if ratio < cfg.min_contrast:
            high = ratio < cfg.high_contrast_below
            reasons.add("low_contrast", Severity.HIGH if high else Severity.MEDIUM)
            reasons.details["contrast_ratio"] = round(ratio, 3)
            reasons.details["colour"] = colour.lower()
            reasons.details["background"] = background
    elif colour is not None and colour.startswith("theme:"):
        if colour.split(":", 1)[1] in BACKGROUND_THEMES:
            reasons.add("background_colour", Severity.MEDIUM)
            reasons.details["colour"] = colour
    return reasons


def _char_reasons(ch: Char, page: Page, scan: ScanInput) -> _Reasons:
    cx = (ch.bbox.x0 + ch.bbox.x1) / 2
    cy = (ch.bbox.top + ch.bbox.bottom) / 2
    reasons = _style_reasons(ch.style, background_at(page, cx, cy), scan)
    if not (0 <= cx <= page.width and 0 <= cy <= page.height):
        reasons.add("off_page", Severity.HIGH)
    return reasons


@dataclass
class _Run:
    start: int
    end: int
    reasons: _Reasons
    boxes: list[BBox]


def _occurrences(text: str, needle: str, lo: int, hi: int) -> Iterator[tuple[int, int]]:
    """Every occurrence of ``needle`` in ``text[lo:hi]`` (fail-closed fallback)."""
    needle = needle.strip()
    if len(needle) < 2:
        return
    pos = text.find(needle, lo, hi)
    while pos >= 0:
        yield pos, pos + len(needle)
        pos = text.find(needle, pos + len(needle), hi)


class HiddenTextDetector:
    name = "hidden_text"

    def detect(self, scan: ScanInput) -> Iterable[Finding]:
        for page, pidx in zip(scan.doc.pages, scan.index.pages, strict=True):
            if page.chars:
                yield from self._pdf_page(scan, page, pidx)
            else:
                yield from self._block_page(scan, page, pidx)

    def _finding(self, scan: ScanInput, run: _Run, page: int, location: str) -> Finding:
        spans = (Span(start=run.start, end=run.end),) if run.end > run.start else ()
        return Finding(
            id="",
            type=FindingType.HIDDEN_TEXT,
            code=run.reasons.code,
            severity=run.reasons.severity,
            detector=self.name,
            evidence=evidence(scan.text[run.start : run.end].strip()),
            location=location,
            page=page,
            bbox=union_bbox(run.boxes),
            spans=spans,
            remove=True,
            details={"reasons": ",".join(sorted(run.reasons.codes)), **run.reasons.details},
        )

    def _pdf_page(self, scan: ScanInput, page: Page, pidx: PageIndex) -> Iterator[Finding]:
        text = scan.text
        placed: list[tuple[int, int, _Reasons, BBox]] = []
        unplaced: dict[int | None, list[int]] = {}
        for ci, ch in enumerate(page.chars):
            if not ch.text or ch.text.isspace():
                continue
            reasons = _char_reasons(ch, page, scan)
            if not reasons.codes:
                continue
            offset = pidx.chars[ci]
            if offset is None:
                unplaced.setdefault(pidx.char_blocks[ci], []).append(ci)
            else:
                placed.append((offset, offset + len(ch.text), reasons, ch.bbox))
        placed.sort(key=lambda item: item[0])
        runs: list[_Run] = []
        for start, end, reasons, box in placed:
            last = runs[-1] if runs else None
            # Overlapping characters always join; adjacent ones only for the same reasons,
            # so each finding says one thing (white text, tiny text, ...).
            overlapping = last is not None and start < last.end
            adjacent = (
                last is not None
                and start >= last.end
                and not text[last.end : start].strip()
                and set(last.reasons.codes) == set(reasons.codes)
            )
            if last is not None and (overlapping or adjacent):
                last.end = max(last.end, end)
                last.reasons.merge(reasons)
                last.boxes.append(box)
            else:
                copy = _Reasons()
                copy.merge(reasons)
                runs.append(_Run(start, end, copy, [box]))
        for run in runs:
            yield self._finding(scan, run, page.number, "body")
        # Fail closed: characters we could not place remove their whole line, or every
        # occurrence of their text on the page when the line itself was not found.
        for block_index, char_ids in unplaced.items():
            reasons = _Reasons()
            for ci in char_ids:
                reasons.merge(_char_reasons(page.chars[ci], page, scan))
            boxes = [page.chars[ci].bbox for ci in char_ids]
            span = pidx.blocks[block_index] if block_index is not None else None
            if span is not None:
                yield self._finding(
                    scan, _Run(span[0], span[1], reasons, boxes), page.number, "body"
                )
                continue
            hidden = "".join(page.chars[ci].text for ci in char_ids)
            lo, hi = pidx.base, pidx.base + pidx.length
            found = False
            for start, end in _occurrences(text, hidden, lo, hi):
                found = True
                yield self._finding(scan, _Run(start, end, reasons, boxes), page.number, "body")
            if not found:
                run = _Run(0, 0, reasons, boxes)
                finding = self._finding(scan, run, page.number, "body")
                yield finding.model_copy(update={"evidence": evidence(hidden)})

    def _block_page(self, scan: ScanInput, page: Page, pidx: PageIndex) -> Iterator[Finding]:
        text = scan.text
        pending: _Run | None = None
        pending_group: int | None = None
        pending_region = "body"
        for bi, block in enumerate(page.blocks):
            if not block.text.strip():
                continue
            background = WHITE
            if block.bbox is not None:
                cx = (block.bbox.x0 + block.bbox.x1) / 2
                cy = (block.bbox.top + block.bbox.bottom) / 2
                background = background_at(page, cx, cy)
            reasons = _style_reasons(block.style, background, scan)
            if block.off_page:
                reasons.add("off_page", Severity.HIGH)
            if not reasons.codes:
                continue
            span = pidx.blocks[bi]
            if span is None:
                if pending is not None:
                    yield self._finding(scan, pending, page.number, pending_region)
                    pending = None
                if block.region in {"comment", "footnote"}:
                    run = _Run(0, 0, reasons, [])
                    finding = self._finding(scan, run, page.number, block.region)
                    yield finding.model_copy(update={"evidence": evidence(block.text)})
                    continue
                lo, hi = pidx.base, pidx.base + pidx.length
                for start, end in _occurrences(text, block.text, lo, hi):
                    run = _Run(start, end, reasons, [])
                    yield self._finding(scan, run, page.number, block.region)
                continue
            start, end = span
            if (
                pending is not None
                and pending_group == block.group
                and start >= pending.end
                and not text[pending.end : start].strip()
                and set(pending.reasons.codes) == set(reasons.codes)
            ):
                pending.end = end
                pending.reasons.merge(reasons)
                continue
            if pending is not None:
                yield self._finding(scan, pending, page.number, pending_region)
            pending = _Run(start, end, reasons, [block.bbox] if block.bbox else [])
            pending_group = block.group
            pending_region = block.region
        if pending is not None:
            yield self._finding(scan, pending, page.number, pending_region)
