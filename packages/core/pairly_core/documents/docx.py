# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""DOCX parsing with python-docx.

Every run becomes a ``TextBlock`` carrying its effective style: font name, size, colour
(``#rrggbb``, ``theme:<name>`` or ``None`` for automatic), bold, italic and ``hidden``
(``w:vanish`` / ``w:specVanish``). Effective means direct formatting first, then the
run's character style chain, then the paragraph style chain, then document defaults.

Text that a reader may not see is kept and labelled by region: headers and footers
(every section, first and even page variants), text boxes, tables, comments and
footnotes. Core properties (``docProps/core.xml``), app properties and custom
properties go into ``metadata``; they are a common place to hide instructions.

The ZIP is checked before python-docx opens it: total uncompressed size and entry count
are capped (zip bomb guard), and every part with a DTD or entity declaration is refused
(python-docx's own lxml parser does not resolve entities either).
"""

from __future__ import annotations

import zipfile
from collections.abc import Callable, Iterator
from functools import partial
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from pairly_core.documents.model import (
    NormalizedDocument,
    Page,
    ParserInfo,
    ParseWarning,
    Region,
    Style,
    Table,
    TextBlock,
)
from pairly_core.documents.registry import ParseError, ParseOptions
from pairly_core.ingestion.sniff import DOCX

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
MAX_ENTRIES = 5000
MAX_PART_BYTES = 32 * 1024 * 1024
MAX_METADATA_VALUE = 4096
EMU_PER_POINT = 12700


def _check_zip(path: Path, options: ParseOptions) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise ParseError("corrupt", f"not a readable DOCX: {type(exc).__name__}") from None
    if len(infos) > MAX_ENTRIES:
        raise ParseError("zip_bomb", "too many entries")
    total = sum(info.file_size for info in infos)
    if total > options.max_unzipped_bytes:
        raise ParseError("zip_bomb", "uncompressed size too large")
    for info in infos:
        if info.flag_bits & 0x1:
            raise ParseError("encrypted", "DOCX part is encrypted")


def _safe_xml(data: bytes) -> Element | None:
    """Parse a package part, refusing DTDs and entities (XXE, billion laughs)."""
    try:
        return SafeET.fromstring(data, forbid_dtd=True)
    except DefusedXmlException:
        raise ParseError("unsafe_xml", "DOCX part declares a DTD or entities") from None
    except SafeET.ParseError:
        return None


def _read_part(path: Path, name: str) -> bytes | None:
    with zipfile.ZipFile(path) as archive:
        try:
            info = archive.getinfo(name)
        except KeyError:
            return None
        if info.file_size > MAX_PART_BYTES:
            return None
        with archive.open(info) as handle:
            return handle.read(MAX_PART_BYTES)


# ------------------------------------------------------------------ style resolution


def _chain(style: Any) -> Iterator[Any]:
    seen = 0
    while style is not None and seen < 20:
        yield style
        style = getattr(style, "base_style", None)
        seen += 1


def _first(getters: list[Callable[[], Any]]) -> Any:
    for get in getters:
        try:
            value = get()
        except (AttributeError, ValueError, KeyError):
            value = None
        if value is not None:
            return value
    return None


def _first_attr(fonts: list[Any], attr: str) -> Any:
    """The first non-None ``attr`` along the style chain (direct formatting first)."""
    return _first([partial(getattr, f, attr) for f in fonts])


def _color(font: Any) -> str | None:
    color = font.color
    if color is None or color.type is None:
        return None
    if color.rgb is not None:
        return f"#{str(color.rgb).lower()}"
    if color.theme_color is not None:
        return f"theme:{str(color.theme_color).split('.')[-1].split(' ')[0].lower()}"
    return None


def _spec_vanish(run: Any) -> bool:
    rpr = run._r.rPr
    return rpr is not None and rpr.find(f"{W}specVanish") is not None


class _Defaults:
    """Document default run properties (``w:docDefaults``)."""

    def __init__(self, document: Any) -> None:
        self.size: float | None = None
        self.font: str | None = None
        try:
            styles_el = document.styles.element
            rpr = styles_el.find(f"{W}docDefaults/{W}rPrDefault/{W}rPr")
        except AttributeError:
            rpr = None
        if rpr is not None:
            sz = rpr.find(f"{W}sz")
            if sz is not None and sz.get(f"{W}val", "").isdigit():
                self.size = int(sz.get(f"{W}val")) / 2
            fonts = rpr.find(f"{W}rFonts")
            if fonts is not None:
                self.font = fonts.get(f"{W}ascii") or fonts.get(f"{W}hAnsi")


def _run_style(run: Any, paragraph: Any, defaults: _Defaults) -> Style:
    char_styles = list(_chain(getattr(run, "style", None)))
    para_styles = list(_chain(getattr(paragraph, "style", None)))
    fonts = [run.font] + [s.font for s in char_styles] + [s.font for s in para_styles]

    size = _first_attr(fonts, "size")
    color = _first([partial(_color, f) for f in fonts])
    name = _first_attr(fonts, "name") or defaults.font
    hidden = _first_attr(fonts, "hidden")
    bold = _first_attr(fonts, "bold")
    italic = _first_attr(fonts, "italic")
    return Style(
        font_name=str(name)[:128] if name else None,
        font_size=round(size.pt, 2) if size is not None else defaults.size,
        fill_color=color,
        bold=bool(bold) if bold is not None else None,
        italic=bool(italic) if italic is not None else None,
        hidden=bool(hidden) or _spec_vanish(run),
    )


# ------------------------------------------------------------------ parser


class DocxParser:
    name = "docx"
    version = "1"
    media_types = frozenset({DOCX})

    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument:
        import docx
        from docx.opc.exceptions import PackageNotFoundError
        from docx.table import Table as DocxTable
        from docx.text.paragraph import Paragraph

        _check_zip(path, options)
        try:
            document = docx.Document(str(path))
        except (PackageNotFoundError, KeyError, ValueError, zipfile.BadZipFile) as exc:
            raise ParseError("corrupt", f"not a readable DOCX: {type(exc).__name__}") from None
        warnings: list[ParseWarning] = []
        defaults = _Defaults(document)
        blocks: list[TextBlock] = []
        tables: list[Table] = []
        lines: list[str] = []
        group = [0]

        def add_paragraph(paragraph: Any, region: Region) -> None:
            texts: list[str] = []
            for run in paragraph.runs:
                if not run.text:
                    continue
                blocks.append(
                    TextBlock(
                        text=run.text,
                        style=_run_style(run, paragraph, defaults),
                        source="docx",
                        region=region,
                        group=group[0],
                    )
                )
                texts.append(run.text)
            if texts:
                lines.append("".join(texts))
            group[0] += 1

        def add_table(table: Any) -> None:
            rows: list[list[str | None]] = []
            for row in table.rows:
                rows.append([cell.text for cell in row.cells])
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        add_paragraph(paragraph, "table")
            tables.append(Table(rows=rows))

        for item in document.iter_inner_content():
            if isinstance(item, Paragraph):
                add_paragraph(item, "body")
            elif isinstance(item, DocxTable):
                add_table(item)

        body = document.element.body
        for txbx in body.iter(f"{W}txbxContent"):
            for p_el in txbx.iter(f"{W}p"):
                add_paragraph(Paragraph(p_el, document._body), "textbox")

        seen_parts: set[int] = set()
        for section in document.sections:
            for attr, region in (
                ("header", "header"),
                ("first_page_header", "header"),
                ("even_page_header", "header"),
                ("footer", "footer"),
                ("first_page_footer", "footer"),
                ("even_page_footer", "footer"),
            ):
                try:
                    part = getattr(section, attr)
                    if part.is_linked_to_previous:
                        continue
                    key = id(part._element)
                except (AttributeError, ValueError):
                    continue
                if key in seen_parts:
                    continue
                seen_parts.add(key)
                for paragraph in part.paragraphs:
                    add_paragraph(paragraph, region)  # type: ignore[arg-type]

        for comment_text in self._comments(document):
            blocks.append(TextBlock(text=comment_text, source="docx", region="comment"))
        for note in self._footnotes(path):
            blocks.append(TextBlock(text=note, source="docx", region="footnote"))

        metadata = self._metadata(document, path, warnings)
        try:
            first = document.sections[0]
            width = first.page_width.pt if first.page_width else 612.0
            height = first.page_height.pt if first.page_height else 792.0
        except (IndexError, AttributeError):
            width, height = 612.0, 792.0
        page = Page(
            number=1,
            width=float(width),
            height=float(height),
            unit="pt",
            text="\n".join(lines),
            blocks=blocks,
            tables=tables,
            image_count=len(document.inline_shapes),
        )
        if not page.text.strip() and not blocks:
            warnings.append(ParseWarning(code="no_text", message="no text found"))
        return NormalizedDocument(
            media_type=DOCX,
            parser=ParserInfo(
                name=self.name,
                version=self.version,
                libraries={"python-docx": pkg_version("python-docx")},
            ),
            pages=[page],
            metadata=metadata,
            warnings=warnings,
        )

    @staticmethod
    def _comments(document: Any) -> list[str]:
        comments = getattr(document, "comments", None)
        if comments is None:
            return []
        texts: list[str] = []
        try:
            for comment in comments:
                text = getattr(comment, "text", "")
                if text:
                    texts.append(str(text))
        except Exception:
            return texts
        return texts

    @staticmethod
    def _footnotes(path: Path) -> list[str]:
        data = _read_part(path, "word/footnotes.xml")
        if data is None:
            return []
        root = _safe_xml(data)
        if root is None:
            return []
        notes: list[str] = []
        for note in root.iter(f"{W}footnote"):
            if note.get(f"{W}type") in {"separator", "continuationSeparator"}:
                continue
            text = "".join(t.text or "" for t in note.iter(f"{W}t")).strip()
            if text:
                notes.append(text)
        return notes

    @staticmethod
    def _metadata(document: Any, path: Path, warnings: list[ParseWarning]) -> dict[str, str]:
        metadata: dict[str, str] = {}
        props = document.core_properties
        for field in (
            "author",
            "category",
            "comments",
            "content_status",
            "created",
            "identifier",
            "keywords",
            "language",
            "last_modified_by",
            "last_printed",
            "modified",
            "revision",
            "subject",
            "title",
            "version",
        ):
            try:
                value = getattr(props, field)
            except (AttributeError, ValueError):
                continue
            if value not in (None, ""):
                metadata[f"docx:{field}"] = str(value)[:MAX_METADATA_VALUE]
        for part, prefix in (
            ("docProps/app.xml", "docx:app:"),
            ("docProps/custom.xml", "docx:custom:"),
        ):
            try:
                data = _read_part(path, part)
                root = _safe_xml(data) if data is not None else None
            except ParseError:
                warnings.append(ParseWarning(code="unsafe_xml", message=f"{part} declares a DTD"))
                continue
            if root is None:
                continue
            for child in root:
                tag = child.tag.rsplit("}", 1)[-1]
                if prefix.endswith("custom:"):
                    name = child.get("name") or tag
                    value = "".join(child.itertext()).strip()
                else:
                    name, value = tag, (child.text or "").strip()
                if value:
                    metadata[f"{prefix}{name[:64]}"] = value[:MAX_METADATA_VALUE]
        return metadata
