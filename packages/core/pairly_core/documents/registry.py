# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Parsers keyed by (sniffed) media type, and the e-invoice parser registry.

A parser is any object with ``name``, ``version``, ``media_types`` and a synchronous
``parse(path, options)`` returning a ``NormalizedDocument``. Parsers run inside the
sandbox subprocess (``pairly_core.documents.sandbox``), never in the API or worker
process, so they may be slow or crash without harming anything else.

``ParseError`` is the expected failure (encrypted file, corrupt structure, unsupported
e-invoice format). Its ``code`` becomes the document's ``status_reason``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from pairly_core.documents.model import NormalizedDocument


class ParseOptions(BaseModel):
    """Options passed into the sandbox (JSON). Never contains secrets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ocr_languages: str = "eng+ara"
    ocr_dpi: int = Field(default=300, ge=72, le=600)
    max_pages: int = Field(default=200, ge=1)
    # Characters kept with full per-character detail, for the whole document.
    max_chars: int = Field(default=250_000, ge=0)
    # Decompression bomb guard for images and rendered pages.
    max_image_pixels: int = Field(default=80_000_000, ge=1)
    # Uncompressed size guard for ZIP based formats (DOCX).
    max_unzipped_bytes: int = Field(default=300 * 1024 * 1024, ge=1)
    # Below this OCR page text is considered too short to be a real text layer.
    min_text_chars_per_page: int = Field(default=1, ge=0)
    # Renderer-based visibility check for PDF text (``documents.render``): resolution,
    # pages checked per document, and a time budget for the whole document. Pages past
    # either limit are marked unchecked, which sends the document to a person.
    render_check: bool = True
    render_dpi: int = Field(default=110, ge=48, le=300)
    render_max_pages: int = Field(default=30, ge=1, le=1000)
    render_seconds: float = Field(default=20.0, gt=0, le=600)


class ParseError(Exception):
    """An expected, explainable parse failure. ``code`` is short and machine readable."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


@runtime_checkable
class DocumentParser(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def media_types(self) -> frozenset[str]: ...

    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument: ...


class ParserRegistry:
    """Media type -> parser. One parser per media type."""

    def __init__(self, parsers: Iterable[DocumentParser] = ()) -> None:
        self._parsers: dict[str, DocumentParser] = {}
        for parser in parsers:
            self.register(parser)

    def register(self, parser: DocumentParser) -> None:
        if not isinstance(parser, DocumentParser):
            raise TypeError("not a DocumentParser")
        for media_type in parser.media_types:
            if media_type in self._parsers:
                raise ValueError(f"a parser for {media_type} is already registered")
            self._parsers[media_type] = parser

    def get(self, media_type: str) -> DocumentParser:
        try:
            return self._parsers[media_type]
        except KeyError:
            raise ParseError("unsupported_media_type", f"no parser for {media_type}") from None

    @property
    def media_types(self) -> frozenset[str]:
        return frozenset(self._parsers)


# ------------------------------------------------------------------ e-invoices


@runtime_checkable
class EInvoiceParser(Protocol):
    """A structured e-invoice format. UBL 2.1 (Peppol BIS Billing 3.0, PINT AE) ships in
    ``pairly_core.documents.ubl`` and UN/CEFACT CII (Factur-X, ZUGFeRD) in
    ``pairly_core.documents.cii``; another format is one more class registered here."""

    @property
    def format_id(self) -> str:
        """Stable id, for example ``peppol-pint-ae``."""
        ...

    @property
    def version(self) -> str: ...

    def detect(self, root_tag: str, namespaces: dict[str, str]) -> bool:
        """True when the XML root element (Clark notation) is this format."""
        ...

    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument: ...


class EInvoiceRegistry:
    def __init__(self, parsers: Iterable[EInvoiceParser] = ()) -> None:
        self._parsers: dict[str, EInvoiceParser] = {}
        for parser in parsers:
            self.register(parser)

    def register(self, parser: EInvoiceParser) -> None:
        if not isinstance(parser, EInvoiceParser):
            raise TypeError("not an EInvoiceParser")
        if parser.format_id in self._parsers:
            raise ValueError(f"e-invoice format {parser.format_id} is already registered")
        self._parsers[parser.format_id] = parser

    def detect(self, root_tag: str, namespaces: dict[str, str]) -> EInvoiceParser | None:
        for parser in self._parsers.values():
            if parser.detect(root_tag, namespaces):
                return parser
        return None

    @property
    def formats(self) -> frozenset[str]:
        return frozenset(self._parsers)


def default_registry(einvoices: EInvoiceRegistry | None = None) -> ParserRegistry:
    """Every built-in parser. Imports are local so the API never loads parser libraries."""
    from pairly_core.documents.cii import CiiInvoiceParser
    from pairly_core.documents.docx import DocxParser
    from pairly_core.documents.einvoice import XmlParser
    from pairly_core.documents.ocr import ImageParser
    from pairly_core.documents.pdf import PdfParser
    from pairly_core.documents.text import TextParser
    from pairly_core.documents.ubl import UblInvoiceParser

    return ParserRegistry(
        [
            PdfParser(),
            DocxParser(),
            ImageParser(),
            TextParser(),
            XmlParser(einvoices or EInvoiceRegistry([UblInvoiceParser(), CiiInvoiceParser()])),
        ]
    )
