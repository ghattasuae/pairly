# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Write a ``CvText`` as a DOCX with python-docx, in one of several layouts.

Layouts: ``single_column`` (headings and bullet paragraphs), ``table`` (experience in
a two column grid), ``two_column`` (a one-row table: contact, skills and languages on
the left), ``sidebar`` (the same with a dark shaded left cell and white text: a clean
control the DOCX hidden-text check cannot see the shading of), ``arabic`` (right to
left paragraphs) and ``bilingual``.

Arabic text is stored in logical order with ``w:bidi`` and ``w:rtl`` set, as Word does.
``extra`` appends attack paragraphs; ``props`` and ``custom`` set core and custom
document properties. The package is rewritten with fixed timestamps (deterministic).
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from pairly_core.decisions.bidi import has_rtl
from synth.hire.text import CvText, Line

SIDE_SECTIONS: Final = frozenset({"contact", "personal", "skills", "languages", "certifications"})
FIXED_TIME: Final = dt.datetime(2026, 9, 1, 9, 0, 0)


@dataclass(frozen=True)
class Run:
    text: str
    rgb: tuple[int, int, int] | None = None
    size: float | None = None
    hidden: bool = False


@dataclass
class DocxCv:
    text: CvText
    layout: str
    accent: tuple[int, int, int] = (0x1F, 0x3A, 0x68)
    photo_png: bytes | None = None
    extra: Sequence[Sequence[Run] | str] = ()
    props: dict[str, str] = field(default_factory=dict)
    custom: dict[str, str] = field(default_factory=dict)
    author: str = ""

    def render(self) -> bytes:
        document = docx.Document()
        core = document.core_properties
        core.author = self.author
        core.title = self.props.get("title", "Curriculum vitae")
        core.last_modified_by = self.author
        core.created = FIXED_TIME
        core.modified = FIXED_TIME
        core.revision = 1
        for key in ("comments", "subject", "keywords"):
            if self.props.get(key):
                setattr(core, key, self.props[key])
        style = document.styles["Normal"]
        style.font.name = "Calibri"
        style.font.size = Pt(10.5)
        if self.photo_png is not None:
            para = document.add_paragraph()
            if self.text.language == "ar":
                para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            para.add_run().add_picture(io.BytesIO(self.photo_png), width=Pt(72))
        lines = self.text.lines
        if self.layout in ("two_column", "sidebar"):
            self._two_column(document, lines)
        elif self.layout == "table":
            self._table(document, lines)
        else:
            for line in lines:
                self._paragraph(document, line)
        for spec in self.extra:
            para = document.add_paragraph()
            runs = [Run(spec)] if isinstance(spec, str) else list(spec)
            if any(has_rtl(r.text) for r in runs):
                _rtl(para)
            for r in runs:
                run = para.add_run(r.text)
                if r.rgb is not None:
                    run.font.color.rgb = RGBColor(*r.rgb)
                if r.size is not None:
                    run.font.size = Pt(r.size)
                if r.hidden:
                    run.font.hidden = True
        buf = io.BytesIO()
        document.save(buf)
        extra = {"docProps/custom.xml": _custom_xml(self.custom)} if self.custom else {}
        return _rezip(buf.getvalue(), extra)

    # ------------------------------------------------------------------ parts

    def _paragraph(
        self,
        container: Any,
        line: Line,
        *,
        white: bool = False,
    ) -> None:
        kind = line.kind
        rtl = has_rtl(line.text) or self.text.language == "ar"
        if kind == "bullet":
            para = container.add_paragraph(style="List Bullet")
        else:
            para = container.add_paragraph()
        if rtl:
            _rtl(para)
        run = para.add_run(line.text)
        if rtl:
            run._r.get_or_add_rPr().append(OxmlElement("w:rtl"))
        if kind == "name":
            run.font.size = Pt(20)
            run.bold = True
        elif kind == "alt_name":
            run.font.size = Pt(15)
        elif kind == "headline":
            run.font.size = Pt(12)
        elif kind == "heading":
            run.font.size = Pt(12.5)
            run.bold = True
            if not white:
                run.font.color.rgb = RGBColor(*self.accent)
        elif kind == "entry":
            run.bold = True
        elif kind in ("dates", "contact") and not white:
            run.font.color.rgb = RGBColor(0x59, 0x59, 0x59)
        if white:
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    def _two_column(self, document: Any, lines: Sequence[Line]) -> None:
        header = [ln for ln in lines if ln.section == "header"]
        for line in header:
            self._paragraph(document, line)
        table = document.add_table(rows=1, cols=2)
        left, right = table.rows[0].cells
        left.width = Pt(160)
        right.width = Pt(330)
        sidebar = self.layout == "sidebar"
        if sidebar:
            _shade(left, "1F2D3D")
        _clear(left)
        _clear(right)
        for line in lines:
            if line.section in SIDE_SECTIONS:
                self._paragraph(left, line, white=sidebar)
        for line in lines:
            if line.section not in SIDE_SECTIONS and line.section != "header":
                self._paragraph(right, line)

    def _table(self, document: Any, lines: Sequence[Line]) -> None:
        exp = [ln for ln in lines if ln.section == "experience"]
        for line in lines:
            if line.section in ("experience",):
                break
            self._paragraph(document, line)
        heading = next((ln for ln in exp if ln.kind == "heading"), None)
        if heading is not None:
            self._paragraph(document, heading)
        table = document.add_table(rows=1, cols=2)
        table.style = "Table Grid"
        hdr = table.rows[0].cells
        hdr[0].text = "Period"
        hdr[1].text = "Role and achievements"
        for cell in hdr:
            _shade(cell, "E7E9EE")
        row_cells: Any = None
        for line in exp:
            if line.kind == "entry":
                row_cells = table.add_row().cells
                _clear(row_cells[0])
                _clear(row_cells[1])
                self._paragraph(row_cells[1], line)
            elif line.kind == "dates" and row_cells is not None:
                self._paragraph(row_cells[0], line)
            elif line.kind == "bullet" and row_cells is not None:
                self._paragraph(row_cells[1], line)
        seen_exp = False
        for line in lines:
            if line.section == "experience":
                seen_exp = True
                continue
            if seen_exp:
                self._paragraph(document, line)


def _rtl(para: Any) -> None:
    ppr = para._p.get_or_add_pPr()
    ppr.append(ppr.makeelement(qn("w:bidi"), {}))
    para.alignment = WD_ALIGN_PARAGRAPH.RIGHT


def _shade(cell: Any, fill: str) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcpr.append(shd)


def _clear(cell: Any) -> None:
    """Drop the empty first paragraph a new cell has, keeping the cell valid."""
    paragraphs = cell.paragraphs
    if len(paragraphs) == 1 and not paragraphs[0].text:
        paragraphs[0]._p.getparent().remove(paragraphs[0]._p)


def _rezip(data: bytes, extra: dict[str, str]) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        names = [i.filename for i in src.infolist()]
        for name in names:
            if name in extra:
                continue
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(info, src.read(name))
        for name, text in extra.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(info, text)
    return out.getvalue()


def _custom_xml(custom: dict[str, str]) -> str:
    from xml.sax.saxutils import escape

    props = "".join(
        f'<property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="{i + 2}" '
        f'name="{escape(name)}"><vt:lpwstr>{escape(value)}</vt:lpwstr></property>'
        for i, (name, value) in enumerate(custom.items())
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties '
        'xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        f"{props}</Properties>"
    )


def render_docx(text: CvText, layout: str, **kwargs: Any) -> bytes:
    return DocxCv(text, layout, **kwargs).render()
