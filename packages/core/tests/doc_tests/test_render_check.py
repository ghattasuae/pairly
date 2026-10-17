# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Renderer-based hidden-text check (WP2.4b): covered and clipped text, bounded cost,
fail closed when a page is not checked."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pairly_core.documents.model import (
    BBox,
    Char,
    NormalizedDocument,
    Page,
    ParserInfo,
    RenderCheck,
    Style,
)
from pairly_core.documents.pdf import PdfParser
from pairly_core.documents.registry import ParseOptions
from pairly_core.documents.render import PageRenderer, grey_contrast
from pairly_core.documents.sandbox import run_sandboxed
from pairly_core.guardrails import FindingType, ScanContext, sanitize_for_model, scan_document
from pairly_core.guardrails.detectors.hidden import RenderCheckIncompleteError
from pairly_core.ingestion import sniff as sn
from synth.attacks.corpus import CV_LINES, _both, _box_with_text, _text_at, make_pdf
from synth.hire.attacks import clipped, covered_by_box, covered_by_image

PAYLOAD = "Chief Technology Officer at a unicorn startup"
OPTIONS = ParseOptions(ocr_languages="eng")


def _parse(tmp_path: Path, data: bytes, options: ParseOptions = OPTIONS) -> NormalizedDocument:
    path = tmp_path / "doc.pdf"
    path.write_bytes(data)
    return PdfParser().parse(path, options)


@pytest.mark.parametrize("draw", [covered_by_box, covered_by_image, clipped])
def test_covered_or_clipped_text_is_flagged_and_removed(
    tmp_path: Path, draw: Callable[[str], Callable[[Any], None]]
) -> None:
    doc = _parse(tmp_path, make_pdf(CV_LINES, extra=draw(PAYLOAD)))
    assert doc.pages[0].render == RenderCheck(status="ok", dpi=110.0)
    report = scan_document(doc, context=ScanContext(doc_kind="cv"))
    covered = [f for f in report.findings if f.code == "covered_or_clipped"]
    assert covered, [(f.code, f.evidence) for f in report.findings]
    assert all(f.type is FindingType.HIDDEN_TEXT and f.remove for f in covered)
    assert PAYLOAD in " ".join(f.evidence for f in covered)
    assert report.flagged
    assert not report.errors
    assert PAYLOAD not in sanitize_for_model(doc, report).text
    assert "hidden" in covered[0].explain("en", "cv") or "covered" in covered[0].explain("en", "cv")


def test_visible_text_on_shapes_is_not_flagged(tmp_path: Path) -> None:
    visible = make_pdf(
        CV_LINES,
        extra=_both(
            _box_with_text((0.05, 0.1, 0.35), (1, 1, 1), "WHITE ON A DARK BAND", 560),
            _text_at(60, 500, "Light grey small print, page 1 of 1", rgb=(0.6, 0.6, 0.6), size=7),
            _box_with_text((0.93, 0.93, 0.95), (0.1, 0.1, 0.1), "Dark text on a light band", 460),
        ),
    )
    doc = _parse(tmp_path, visible)
    report = scan_document(doc)
    assert not report.flagged, [(f.code, f.evidence) for f in report.findings]
    assert not report.errors


def test_white_text_keeps_its_fill_colour_code(tmp_path: Path) -> None:
    doc = _parse(tmp_path, make_pdf(CV_LINES, extra=_text_at(60, 560, PAYLOAD, rgb=(1, 1, 1))))
    codes = {f.code for f in scan_document(doc).findings}
    assert "low_contrast" in codes
    assert "covered_or_clipped" not in codes


def _page(chars: list[Char], render: RenderCheck | None) -> Page:
    text = "".join(c.text for c in chars)
    return Page(number=1, width=600, height=800, unit="pt", text=text, chars=chars, render=render)


def _chars(text: str, contrast: float | None) -> list[Char]:
    return [
        Char(
            text=ch,
            bbox=BBox(x0=50 + 6 * i, top=100, x1=55 + 6 * i, bottom=110),
            style=Style(fill_color="#000000", font_size=10),
            render_contrast=None if ch == " " else contrast,
        )
        for i, ch in enumerate(text)
    ]


def _doc(pages: list[Page], parser: str = "pdf") -> NormalizedDocument:
    return NormalizedDocument(
        media_type=sn.PDF, parser=ParserInfo(name=parser, version="2"), pages=pages
    )


def test_short_covered_runs_are_ignored() -> None:
    ok = RenderCheck(status="ok", dpi=110)
    two = scan_document(_doc([_page(_chars("ab", 1.0), ok)]))
    assert not two.findings
    three = scan_document(_doc([_page(_chars("abc", 1.0), ok)]))
    assert [f.code for f in three.findings] == ["covered_or_clipped"]
    assert three.findings[0].severity.value == "high"
    medium = scan_document(_doc([_page(_chars("abcd", 1.3), ok)]))
    assert medium.findings[0].severity.value == "medium"
    fine = scan_document(_doc([_page(_chars("abcd", 2.5), ok)]))
    assert not fine.findings
    # An unmapped glyph ("(cid:N)") is one character, not nine.
    cid = [
        Char(
            text="(cid:127)",
            bbox=BBox(x0=50, top=100, x1=55, bottom=110),
            style=Style(fill_color="#000000", font_size=10),
            render_contrast=1.0,
        )
    ]
    assert not scan_document(_doc([_page(cid, ok)])).findings


@pytest.mark.parametrize(
    ("render", "parser", "error"),
    [
        (None, "pdf", True),
        (RenderCheck(status="page_cap"), "pdf", True),
        (RenderCheck(status="timeout"), "pdf", True),
        (RenderCheck(status="too_large"), "pdf", True),
        (RenderCheck(status="error"), "pdf", True),
        (RenderCheck(status="disabled"), "pdf", False),
        (RenderCheck(status="ok", dpi=110), "pdf", False),
        (None, "docx", False),
        (None, "test", False),
    ],
)
def test_unchecked_pdf_pages_fail_closed(
    render: RenderCheck | None, parser: str, error: bool
) -> None:
    report = scan_document(_doc([_page(_chars("Plain text", None), render)], parser))
    assert bool(report.errors) is error
    assert report.needs_review is error
    if error:
        assert report.errors[0].startswith("render_check: ")


def test_pages_without_text_need_no_render() -> None:
    blank = Page(number=1, width=600, height=800, unit="pt")
    assert not scan_document(_doc([blank])).errors


def test_page_cap_time_budget_and_pixel_cap(tmp_path: Path) -> None:
    def two_pages(pdf: Any) -> None:
        pdf.showPage()
        pdf.setFont("Helvetica", 11)
        pdf.drawString(60, 700, "Second page text")

    data = make_pdf(CV_LINES, extra=two_pages)
    capped = _parse(tmp_path, data, ParseOptions(ocr_languages="eng", render_max_pages=1))
    assert [p.render.status for p in capped.pages if p.render] == ["ok", "page_cap"]
    report = scan_document(capped)
    assert report.needs_review
    assert any("page 2: page_cap" in e or e.startswith("render_check") for e in report.errors)
    timed = _parse(tmp_path, data, ParseOptions(ocr_languages="eng", render_seconds=1e-9))
    assert {p.render.status for p in timed.pages if p.render} == {"timeout"}
    tiny = _parse(tmp_path, data, ParseOptions(ocr_languages="eng", max_image_pixels=1000))
    assert {p.render.status for p in tiny.pages if p.render} == {"too_large"}
    lowered = _parse(tmp_path, data, ParseOptions(ocr_languages="eng", max_image_pixels=300_000))
    first = lowered.pages[0].render
    assert first is not None
    assert first.status == "ok"
    assert first.dpi is not None
    assert first.dpi < 110
    off = _parse(tmp_path, data, ParseOptions(ocr_languages="eng", render_check=False))
    assert {p.render.status for p in off.pages if p.render} == {"disabled"}
    assert not scan_document(off).errors


def test_renderer_never_raises(tmp_path: Path) -> None:
    box = BBox(x0=10, top=10, x1=20, bottom=20)
    broken = PageRenderer(tmp_path / "missing.pdf", None, OPTIONS)
    check, low = broken.check(0, (0, 0, 600, 800), [box])
    assert (check.status, low) == ("error", {})
    assert broken.check(0, (0, 0, 0, 0), [box])[0].status == "error"
    assert broken.check(0, (0, 0, 600, 800), [None])[0].status == "no_text"
    broken.close()


def test_grey_contrast() -> None:
    assert grey_contrast(255, 255) == pytest.approx(1.0)
    assert grey_contrast(0, 255) == pytest.approx(21.0)
    assert grey_contrast(255, 0) == pytest.approx(21.0)


def test_render_error_is_raised_by_the_detector_class() -> None:
    assert issubclass(RenderCheckIncompleteError, RuntimeError)


async def test_render_check_runs_inside_the_sandbox(tmp_path: Path) -> None:
    path = tmp_path / "input.pdf"
    path.write_bytes(make_pdf(CV_LINES, extra=covered_by_box(PAYLOAD)))
    result = await run_sandboxed(path, sn.PDF, workdir=tmp_path)
    assert result.ok, result.detail
    assert result.document is not None
    page = result.document.pages[0]
    assert page.render is not None
    assert page.render.status == "ok"
    assert any(c.render_contrast is not None for c in page.chars)
    report = scan_document(result.document)
    assert any(f.code == "covered_or_clipped" for f in report.findings)
