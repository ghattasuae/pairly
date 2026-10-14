# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""XML documents: dispatch to a registered e-invoice parser (UBL, Peppol PINT AE).

The default registry knows UBL 2.1 invoices, Peppol PINT AE included
(``pairly_core.documents.ubl``). Any other XML fails parsing with
``unsupported_einvoice_format`` and goes to the human queue. XML with a DTD or entity
declarations is refused outright (XXE and billion-laughs guard).
"""

from __future__ import annotations

import io
from pathlib import Path

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from pairly_core.documents.model import NormalizedDocument
from pairly_core.documents.registry import EInvoiceRegistry, ParseError, ParseOptions
from pairly_core.ingestion.sniff import XML

MAX_XML_BYTES = 32 * 1024 * 1024


def root_element(path: Path) -> tuple[str, dict[str, str]]:
    """The root tag (Clark notation) and declared namespaces of a safe XML file."""
    with path.open("rb") as handle:
        data = handle.read(MAX_XML_BYTES + 1)
    if len(data) > MAX_XML_BYTES:
        raise ParseError("too_large", "XML document too large")
    namespaces: dict[str, str] = {}
    try:
        for event, item in SafeET.iterparse(
            io.BytesIO(data), events=("start-ns", "start"), forbid_dtd=True
        ):
            if event == "start-ns":
                prefix, uri = item
                namespaces[str(prefix)] = str(uri)
            else:
                return str(item.tag), namespaces
    except DefusedXmlException:
        raise ParseError("unsafe_xml", "XML declares a DTD or entities") from None
    except SafeET.ParseError:
        raise ParseError("corrupt", "malformed XML") from None
    raise ParseError("corrupt", "empty XML document")


class XmlParser:
    name = "xml"
    version = "1"
    media_types = frozenset({XML})

    def __init__(self, einvoices: EInvoiceRegistry) -> None:
        self.einvoices = einvoices

    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument:
        tag, namespaces = root_element(path)
        parser = self.einvoices.detect(tag, namespaces)
        if parser is None:
            raise ParseError("unsupported_einvoice_format", "no e-invoice parser for this XML")
        document = parser.parse(path, options)
        return document.model_copy(
            update={"metadata": {**document.metadata, "einvoice:format": parser.format_id}}
        )
