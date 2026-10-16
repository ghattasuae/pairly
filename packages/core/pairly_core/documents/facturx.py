# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Factur-X and ZUGFeRD 2: the CII invoice embedded in a PDF.

A Factur-X PDF is an ordinary invoice PDF with the same invoice as UN/CEFACT CII XML
attached under a fixed name (``factur-x.xml``; ``zugferd-invoice.xml`` and
``xrechnung.xml`` for ZUGFeRD 2). The PDF parser (``pairly_core.documents.pdf``) asks
this module for that attachment; when it reads as a CII invoice the document gets:

* one more page, without layout, holding the CII lines (``text_page``), so the quotes of
  the structured values are in the text the guardrails and the evidence locator read;
* ``einvoice:format`` (``un-cefact-cii``), ``einvoice:data``, ``einvoice:container``
  (``factur-x``) and ``einvoice:attachment`` (the file name) metadata.

The XML goes through ``defusedxml`` like any XML: a DTD, an entity declaration or an
external reference is refused and leaves an ``unsafe_xml`` warning, which sends the
document to a person. Because the PDF and its XML can disagree (the picture shows one
bank account, the data another), the invoice number and the total of the XML must also
appear in the PDF's own text, or the document carries ``einvoice_mismatch`` and goes to
a person. Any other attachment keeps the ``embedded_files`` warning.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from pairly_core.documents.cii import CiiInvoiceParser, read_cii
from pairly_core.documents.model import Page, ParseWarning
from pairly_core.documents.registry import ParseError
from pairly_core.documents.ubl import DATA_KEY, MAX_XML_BYTES, safe_root, text_page

FACTURX_NAMES: Final = frozenset({"factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml"})
CONTAINER: Final = "factur-x"
MAX_ATTACHMENTS: Final = 50


@dataclass(frozen=True)
class Attachments:
    """The embedded files of a PDF: their names, and the e-invoice XML when there is one."""

    names: tuple[str, ...]
    einvoice_name: str | None = None
    einvoice_xml: bytes | None = None
    too_large: bool = False

    @property
    def only_einvoice(self) -> bool:
        return self.einvoice_xml is not None and len(self.names) == 1


def find_attachments(reader: Any) -> Attachments:
    """The embedded files of a ``pypdf.PdfReader`` (at most ``MAX_ATTACHMENTS`` names)."""
    names: list[str] = []
    found_name: str | None = None
    found: bytes | None = None
    too_large = False
    for index, attachment in enumerate(reader.attachment_list):
        if index >= MAX_ATTACHMENTS:
            break
        name = str(attachment.name or "")[:200]
        names.append(name)
        if found is not None or name.lower() not in FACTURX_NAMES:
            continue
        declared = attachment.size
        if isinstance(declared, int) and declared > MAX_XML_BYTES:
            too_large = True
            continue
        data = bytes(attachment.content)
        if len(data) > MAX_XML_BYTES:
            too_large = True
            continue
        found_name, found = name, data
    return Attachments(tuple(names), found_name, found, too_large)


def _in_visual_text(value: str | None, text: str) -> bool:
    if not value:
        return False
    return value in text or value in text.replace(",", "")


def read_embedded(
    attachments: Attachments, visual_text: str, page_number: int
) -> tuple[Page | None, dict[str, str], list[ParseWarning]]:
    """The e-invoice page, metadata and warnings of a PDF's attachments."""
    warnings: list[ParseWarning] = []
    if attachments.too_large:
        warnings.append(ParseWarning(code="embedded_files", message="e-invoice XML too large"))
    if attachments.einvoice_xml is None:
        return None, {}, warnings
    try:
        lines, data, extra = read_cii(safe_root(attachments.einvoice_xml))
    except ParseError as exc:
        code = "unsafe_xml" if exc.code == "unsafe_xml" else "embedded_files"
        warnings.append(ParseWarning(code=code, message=f"embedded e-invoice not read: {exc.code}"))
        return None, {}, warnings
    warnings.extend(extra)
    header = data["header"]
    number = (header.get("invoice_number") or {}).get("value")
    total = (header.get("total") or {}).get("value")
    if not (_in_visual_text(number, visual_text) and _in_visual_text(total, visual_text)):
        warnings.append(
            ParseWarning(
                code="einvoice_mismatch",
                message="the embedded invoice number or total is not in the PDF text",
            )
        )
    metadata = {
        "einvoice:format": CiiInvoiceParser.format_id,
        DATA_KEY: json.dumps(data, ensure_ascii=False, sort_keys=True),
        "einvoice:customization": data["customization"] or "",
        "einvoice:container": CONTAINER,
        "einvoice:attachment": attachments.einvoice_name or "",
    }
    return text_page(lines, page_number), metadata, warnings
