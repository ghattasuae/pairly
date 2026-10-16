# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""PDF parsing: pdfplumber for layout and text styling, pypdf for structure and metadata.

Per page: every character with its box, font, size, fill and stroke colour, text
rendering mode (3 = invisible) and an ``off_page`` flag; words (split where the style
changes); lines as blocks with their dominant style; tables. Pages without a text
layer but with images are rendered (pypdfium2, through pdfplumber) and OCR'd.

Page text and lines come from ``documents.pdf_text`` (reading order across columns,
Arabic presentation forms normalised and put back in logical order); every character
keeps its box and records its offset in the page text.

Per page, a renderer-based visibility check (``documents.render``): characters whose
rendered pixels show no contrast (covered by a shape, clipped) carry
``Char.render_contrast``, and ``Page.render`` says whether the page was checked.

Document level: the Info dictionary, raw XMP, and structural warnings for active
content (JavaScript, open actions) and embedded files. The CII invoice of a Factur-X or
ZUGFeRD PDF is read (``documents.facturx``) and added as one more page. Encrypted files
open only with an empty user password; otherwise parsing fails with ``encrypted``.

No PyMuPDF (AGPL).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

from pairly_core.documents.colors import to_hex
from pairly_core.documents.facturx import Attachments, find_attachments, read_embedded
from pairly_core.documents.model import (
    BBox,
    Char,
    Fill,
    NormalizedDocument,
    OcrInfo,
    Page,
    ParserInfo,
    ParseWarning,
    RenderCheck,
    Style,
    Table,
    TextBlock,
    Word,
)
from pairly_core.documents.pdf_text import lay_out
from pairly_core.documents.registry import ParseError, ParseOptions
from pairly_core.documents.render import PageRenderer
from pairly_core.ingestion.sniff import PDF

MAX_METADATA_VALUE = 4096
MAX_XMP_BYTES = 64 * 1024
# Filled rectangles kept per page (backgrounds for the hidden-text check).
MAX_FILLS_PER_PAGE = 2000
INVISIBLE_RENDER_MODE = 3

_patched = False


def _install_render_mode_hook() -> None:
    """Record the PDF text rendering mode on each character (pdfplumber drops it).

    Runs inside the sandbox process only. pdfminer passes the text state to
    ``render_string`` but not to ``render_char``; we remember it in between.
    """
    global _patched
    if _patched:
        return
    import pdfplumber.page as plumber_page

    aggregator = plumber_page.PDFPageAggregatorWithMarkedContent
    original_string = aggregator.render_string
    original_char = aggregator.render_char

    def render_string(self: Any, textstate: Any, *args: Any, **kwargs: Any) -> Any:
        self._pairly_render_mode = getattr(textstate, "render", 0)
        return original_string(self, textstate, *args, **kwargs)

    def render_char(self: Any, *args: Any, **kwargs: Any) -> Any:
        adv = original_char(self, *args, **kwargs)
        objs = getattr(self.cur_item, "_objs", None)
        if objs:
            objs[-1].rendermode = getattr(self, "_pairly_render_mode", 0)
        return adv

    aggregator.render_string = render_string  # type: ignore[method-assign]
    aggregator.render_char = render_char  # type: ignore[method-assign]
    plumber_page.ALL_ATTRS.add("rendermode")
    _patched = True


def _style(obj: dict[str, Any]) -> Style:
    size = obj.get("size")
    return Style(
        font_name=str(obj["fontname"])[:128] if obj.get("fontname") else None,
        font_size=round(float(size), 3) if isinstance(size, int | float) else None,
        fill_color=to_hex(obj.get("non_stroking_color")),
        stroke_color=to_hex(obj.get("stroking_color")),
        invisible=obj.get("rendermode") == INVISIBLE_RENDER_MODE,
    )


def _bbox(obj: dict[str, Any]) -> BBox:
    return BBox(
        x0=round(float(obj["x0"]), 3),
        top=round(float(obj["top"]), 3),
        x1=round(float(obj["x1"]), 3),
        bottom=round(float(obj["bottom"]), 3),
    )


def _dominant(chars: Iterable[dict[str, Any]]) -> Style:
    styles = Counter(_style(c) for c in chars)
    if not styles:
        return Style()
    return styles.most_common(1)[0][0]


def _metadata_value(value: Any) -> str:
    return str(value)[:MAX_METADATA_VALUE]


def _structure(
    path: Path, warnings: list[ParseWarning]
) -> tuple[dict[str, str], bool, Attachments]:
    """pypdf pass: Info, XMP, active content, embedded files.

    Returns (metadata, encrypted, attachments). A Factur-X attachment that is the only
    embedded file is not an ``embedded_files`` warning: ``documents.facturx`` reads it.
    """
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(path, strict=False)
    except (PdfReadError, OSError, ValueError) as exc:
        raise ParseError("corrupt", f"cannot read PDF: {type(exc).__name__}") from None
    encrypted = bool(reader.is_encrypted)
    if encrypted:
        try:
            ok = bool(reader.decrypt(""))
        except Exception:
            ok = False
        if not ok:
            raise ParseError("encrypted", "PDF needs a password")
        warnings.append(
            ParseWarning(code="encrypted", message="PDF is encrypted (no user password)")
        )

    metadata: dict[str, str] = {}
    attachments = Attachments(names=())
    try:
        info: Any = reader.metadata or {}
        for key, value in info.items():
            metadata[f"pdf:{str(key).lstrip('/')}"] = _metadata_value(value)
    except Exception:
        warnings.append(ParseWarning(code="metadata_error", message="unreadable Info dictionary"))

    try:
        root: Any = reader.trailer["/Root"].get_object()
        meta_ref = root.get("/Metadata")
        if meta_ref is not None:
            data = meta_ref.get_object().get_data()[: MAX_XMP_BYTES + 1]
            metadata["xmp"] = data[:MAX_XMP_BYTES].decode("utf-8", errors="replace")
            if len(data) > MAX_XMP_BYTES:
                warnings.append(ParseWarning(code="xmp_truncated", message="XMP truncated"))
        names_ref: Any = root.get("/Names")
        names: Any = names_ref.get_object() if names_ref is not None else {}
        if "/OpenAction" in root or "/AA" in root or "/JavaScript" in names:
            warnings.append(
                ParseWarning(
                    code="active_content", message="PDF has JavaScript or automatic actions"
                )
            )
        if "/EmbeddedFiles" in names:
            try:
                attachments = find_attachments(reader)
            except Exception:
                attachments = Attachments(names=("?",))
            if not attachments.only_einvoice:
                warnings.append(
                    ParseWarning(code="embedded_files", message="PDF has embedded files")
                )
    except Exception:
        warnings.append(ParseWarning(code="structure_error", message="unreadable document catalog"))
    metadata["pdf:pages"] = str(len(reader.pages))
    return metadata, encrypted, attachments


def _fills(page: Any, warnings: list[ParseWarning], number: int) -> list[Fill]:
    """Filled rectangles with a solid colour, largest first, capped per page."""
    found: list[Fill] = []
    for obj in page.rects:
        if not obj.get("fill"):
            continue
        color = to_hex(obj.get("non_stroking_color"))
        if color is None or color.startswith("pattern:"):
            continue
        found.append(Fill(bbox=_bbox(obj), color=color))
    found.sort(key=lambda f: f.bbox.width * f.bbox.height, reverse=True)
    if len(found) > MAX_FILLS_PER_PAGE:
        warnings.append(
            ParseWarning(code="fills_truncated", message="too many filled shapes", page=number)
        )
        found = found[:MAX_FILLS_PER_PAGE]
    return found


class PdfParser:
    name = "pdf"
    # 2: renderer-based visibility check (Page.render, Char.render_contrast).
    # 3: own reading order (columns, right-to-left text, Char.offset).
    # 4: the CII invoice of a Factur-X / ZUGFeRD PDF as one more page (documents.facturx).
    version = "4"
    media_types = frozenset({PDF})

    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument:
        import pdfplumber
        from pdfminer.pdfparser import PDFSyntaxError

        _install_render_mode_hook()
        warnings: list[ParseWarning] = []
        metadata, encrypted, attachments = _structure(path, warnings)
        budget = [options.max_chars]
        pages: list[Page] = []
        renderer = PageRenderer(path, "" if encrypted else None, options)
        try:
            with pdfplumber.open(path, password="" if encrypted else None) as pdf:
                total = len(pdf.pages)
                for index, page in enumerate(pdf.pages):
                    if index >= options.max_pages:
                        warnings.append(
                            ParseWarning(
                                code="pages_truncated",
                                message=f"only {options.max_pages} of {total} pages read",
                            )
                        )
                        break
                    try:
                        pages.append(
                            self._page(page, index + 1, options, budget, warnings, renderer)
                        )
                    except (ParseError, MemoryError):
                        raise
                    except Exception as exc:
                        warnings.append(
                            ParseWarning(
                                code="page_error",
                                message=f"page could not be read: {type(exc).__name__}",
                                page=index + 1,
                            )
                        )
                        pages.append(
                            Page(
                                number=index + 1,
                                width=float(page.width),
                                height=float(page.height),
                                unit="pt",
                            )
                        )
                    finally:
                        page.close()
        except (PDFSyntaxError, OSError, ValueError, KeyError, TypeError) as exc:
            raise ParseError("corrupt", f"cannot read PDF: {type(exc).__name__}") from None
        finally:
            renderer.close()
        if pages and not any(p.text.strip() for p in pages):
            warnings.append(ParseWarning(code="no_text", message="no text found"))
        einvoice, einvoice_metadata, einvoice_warnings = read_embedded(
            attachments, "\n".join(p.text for p in pages), len(pages) + 1
        )
        warnings.extend(einvoice_warnings)
        if einvoice is not None:
            pages.append(einvoice)
            metadata.update(einvoice_metadata)
        if budget[0] < 0:
            warnings.append(
                ParseWarning(code="chars_truncated", message="character detail limit reached")
            )
        return NormalizedDocument(
            media_type=PDF,
            parser=ParserInfo(
                name=self.name,
                version=self.version,
                libraries={
                    "pdfplumber": pkg_version("pdfplumber"),
                    "pdfminer.six": pkg_version("pdfminer.six"),
                    "pypdf": pkg_version("pypdf"),
                    "pypdfium2": pkg_version("pypdfium2"),
                },
            ),
            pages=pages,
            metadata=metadata,
            warnings=warnings,
        )

    def _page(
        self,
        page: Any,
        number: int,
        options: ParseOptions,
        budget: list[int],
        warnings: list[ParseWarning],
        renderer: PageRenderer,
    ) -> Page:
        width, height = float(page.width), float(page.height)
        raw_chars: list[dict[str, Any]] = list(page.chars)
        layout = lay_out(raw_chars, width)
        chars: list[Char] = []
        for i, obj in enumerate(raw_chars):
            if budget[0] <= 0:
                budget[0] = -1
                break
            box = _bbox(obj)
            chars.append(
                Char(
                    text=layout.glyphs[i],
                    bbox=box,
                    style=_style(obj),
                    off_page=box.outside(width, height),
                    offset=layout.offsets[i],
                )
            )
            budget[0] -= 1
        chars, render = self._render_check(page, number, chars, renderer)

        extra = ["fontname", "size", "non_stroking_color", "stroking_color"]
        if raw_chars and "rendermode" in raw_chars[0]:
            extra.append("rendermode")
        words: list[Word] = []
        for obj in page.extract_words(extra_attrs=extra, keep_blank_chars=False):
            box = _bbox(obj)
            words.append(
                Word(
                    text=str(obj["text"]),
                    bbox=box,
                    style=_style(obj),
                    off_page=box.outside(width, height),
                )
            )

        blocks: list[TextBlock] = []
        for index, (seg, start, end) in enumerate(layout.segments):
            box = BBox(
                x0=round(seg.x0, 3),
                top=round(seg.top, 3),
                x1=round(seg.x1, 3),
                bottom=round(seg.bottom, 3),
            )
            blocks.append(
                TextBlock(
                    text=layout.text[start:end],
                    bbox=box,
                    style=_dominant(raw_chars[i] for i in seg.chars),
                    source="pdf",
                    off_page=box.outside(width, height),
                    group=index,
                )
            )

        tables: list[Table] = []
        try:
            for found in page.find_tables():
                rows = [
                    [cell if cell is None else str(cell) for cell in row] for row in found.extract()
                ]
                x0, top, x1, bottom = found.bbox
                tables.append(Table(bbox=BBox(x0=x0, top=top, x1=x1, bottom=bottom), rows=rows))
        except Exception:
            warnings.append(ParseWarning(code="table_error", message="tables skipped", page=number))

        fills = _fills(page, warnings, number)
        text = layout.text
        image_count = len(page.images)
        result = Page(
            number=number,
            width=width,
            height=height,
            unit="pt",
            rotation=int(page.rotation or 0),
            text=text,
            blocks=blocks,
            words=words,
            chars=chars,
            tables=tables,
            fills=fills,
            image_count=image_count,
            render=render,
        )
        if len(text.strip()) < options.min_text_chars_per_page and image_count:
            result = self._ocr(page, result, options, warnings)
        return result

    def _render_check(
        self, page: Any, number: int, chars: list[Char], renderer: PageRenderer
    ) -> tuple[list[Char], RenderCheck]:
        """Rendered contrast under every visible character (see ``documents.render``)."""
        boxes = [
            None if not ch.text.strip() or ch.style.invisible or ch.off_page else ch.bbox
            for ch in chars
        ]
        cropbox = tuple(float(v) for v in page.cropbox)
        check, low = renderer.check(
            number - 1, (cropbox[0], cropbox[1], cropbox[2], cropbox[3]), boxes
        )
        if not low:
            return chars, check
        updated = [
            ch.model_copy(update={"render_contrast": low[i]}) if i in low else ch
            for i, ch in enumerate(chars)
        ]
        return updated, check

    def _ocr(
        self, page: Any, result: Page, options: ParseOptions, warnings: list[ParseWarning]
    ) -> Page:
        from pairly_core.documents.ocr import (
            ENGINE,
            ocr_image,
            resolve_languages,
            tesseract_available,
            tesseract_version,
        )

        if not tesseract_available():
            warnings.append(
                ParseWarning(
                    code="ocr_unavailable", message="scanned page not OCR'd", page=result.number
                )
            )
            return result
        languages, missing = resolve_languages(options.ocr_languages)
        if missing and not any(w.code == "ocr_language_missing" for w in warnings):
            warnings.append(
                ParseWarning(
                    code="ocr_language_missing",
                    message=f"OCR languages not installed: {'+'.join(missing)}",
                )
            )
        if not languages:
            warnings.append(
                ParseWarning(code="ocr_unavailable", message="no OCR language", page=result.number)
            )
            return result
        pixels = (result.width * options.ocr_dpi / 72) * (result.height * options.ocr_dpi / 72)
        if pixels > options.max_image_pixels:
            warnings.append(
                ParseWarning(
                    code="image_too_large", message="page too large to OCR", page=result.number
                )
            )
            return result
        image = page.to_image(resolution=options.ocr_dpi).original
        ocr = ocr_image(image, languages=languages, scale=72.0 / options.ocr_dpi)
        return result.model_copy(
            update={
                "text": ocr.text,
                "blocks": ocr.blocks,
                "words": ocr.words,
                "ocr": OcrInfo(
                    engine=f"{ENGINE} {tesseract_version()}",
                    languages=languages,
                    mean_confidence=ocr.mean_confidence,
                    words=len(ocr.words),
                    dpi=options.ocr_dpi,
                ),
            }
        )
