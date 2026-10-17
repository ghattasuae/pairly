# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""UBL 2.1 invoices (Peppol BIS Billing 3.0, Peppol PINT AE): an ``EInvoiceParser``.

A structured e-invoice needs no model: every value is read exactly as the XML states it
(amounts stay the decimal strings they are, dates stay ISO). The parser produces:

* ``text``: one readable line per header value (``Invoice number: INV-1``) and one row
  per invoice line (``2. Docking station 5 405.58 S 5.00 2027.90``, columns named once
  above the rows), so people, the guardrails and the evidence locator all see the same
  content as for any other document. Free-text notes (``cbc:Note``) are kept as ``Note:`` lines:
  they are the usual place for an injected instruction, and the guardrails must read
  them like any text.
* ``metadata["einvoice:data"]``: the structured values as canonical JSON. Each value is
  ``{"value": <exact text>, "quote": <its line in text>}``, so a workflow maps them to its
  own fields with evidence and without guessing.

Safety: the XML is read with ``defusedxml`` (no DTD, no entity declarations, no external
references are ever fetched) under a size cap; every value is stripped of control
characters and line breaks and capped in length; at most ``MAX_LINES`` invoice lines are
read (more is a ``lines_truncated`` warning, which sends the document to a person).
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Final
from xml.etree.ElementTree import Element

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from pairly_core.documents.model import (
    NormalizedDocument,
    Page,
    ParserInfo,
    ParseWarning,
    TextBlock,
)
from pairly_core.documents.registry import ParseError, ParseOptions
from pairly_core.ingestion.sniff import XML

MAX_XML_BYTES: Final = 32 * 1024 * 1024
MAX_LINES: Final = 500
MAX_VALUE_CHARS: Final = 200
MAX_NOTES: Final = 20
# The evidence locator's longest quote (``pairly_core.extraction.evidence``).
MAX_QUOTE_CHARS: Final = 240
LINE_COLUMNS: Final = (
    "number",
    "description",
    "quantity",
    "unit price",
    "VAT category",
    "VAT rate in percent",
    "line total",
)
DATA_KEY: Final = "einvoice:data"

INVOICE_NS: Final = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
CAC: Final = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC: Final = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
PINT_AE_PREFIX: Final = "urn:peppol:pint:billing-1@ae-1"
# Peppol BIS Billing 3.0 (EN 16931 compliant UBL).
PEPPOL_BIS3: Final = "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
# PINT AE endpoint scheme of a UAE tax registration number.
TRN_SCHEME: Final = "0235"

_CONTROL = re.compile("[\\x00-\\x1f\\x7f\\u2028\\u2029]+")


def clean(text: str | None) -> str | None:
    """One line of at most ``MAX_VALUE_CHARS``: no control characters or line breaks."""
    if text is None:
        return None
    value = unicodedata.normalize("NFC", _CONTROL.sub(" ", text))
    value = " ".join(value.split())[:MAX_VALUE_CHARS].strip()
    return value or None


def _q(prefix: str, name: str) -> str:
    return f"{{{CAC if prefix == 'cac' else CBC}}}{name}"


def _path(path: str) -> str:
    """``cac:A/cbc:B`` in Clark notation for ``find``."""
    parts = []
    for step in path.split("/"):
        prefix, _, name = step.partition(":")
        parts.append(_q(prefix, name))
    return "/".join(parts)


def _text(node: Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(_path(path))
    return clean(found.text) if found is not None else None


def _find(node: Element | None, path: str) -> Element | None:
    return None if node is None else node.find(_path(path))


class Writer:
    """Collects the readable lines and the structured values with their quotes."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, label: str, value: str | None) -> dict[str, str] | None:
        if value is None:
            return None
        line = f"{label}: {value}"
        self.lines.append(line)
        return {"value": value, "quote": line}


def _supplier_trn(party: Element | None) -> str | None:
    trn = _text(party, "cac:PartyTaxScheme/cbc:CompanyID")
    if trn:
        return trn
    endpoint = _find(party, "cbc:EndpointID")
    if endpoint is not None and endpoint.get("schemeID") == TRN_SCHEME:
        return clean(endpoint.text)
    return None


def _party_name(party: Element | None) -> str | None:
    return _text(party, "cac:PartyName/cbc:Name") or _text(
        party, "cac:PartyLegalEntity/cbc:RegistrationName"
    )


def _vat_rate(root: Element) -> str | None:
    """The invoice's VAT rate when its taxed categories share one rate, else None."""
    rates = set()
    for sub in root.findall(_path("cac:TaxTotal/cac:TaxSubtotal")):
        percent = _text(sub, "cac:TaxCategory/cbc:Percent")
        category = _text(sub, "cac:TaxCategory/cbc:ID")
        if percent is not None and category not in {"Z", "E", "O"}:
            rates.add(percent)
    return next(iter(rates)) if len(rates) == 1 else None


def _tax_amount(root: Element, currency: str | None) -> str | None:
    """The VAT total in the document currency.

    Peppol BIS 3 may carry a second ``cac:TaxTotal`` in the VAT accounting currency (no
    subtotals): the one whose amount is in the document currency is the invoice's.
    """
    amounts = root.findall(_path("cac:TaxTotal/cbc:TaxAmount"))
    for amount in amounts:
        if currency is not None and amount.get("currencyID") == currency:
            return clean(amount.text)
    return clean(amounts[0].text) if amounts else None


def read_invoice(root: Element) -> tuple[list[str], dict[str, Any], list[ParseWarning]]:
    """The readable lines, the structured data and any warnings of one UBL invoice."""
    out = Writer()
    warnings: list[ParseWarning] = []
    customization = _text(root, "cbc:CustomizationID")
    header: dict[str, Any] = {}
    supplier = _find(root, "cac:AccountingSupplierParty/cac:Party")
    customer = _find(root, "cac:AccountingCustomerParty/cac:Party")
    totals = _find(root, "cac:LegalMonetaryTotal")
    currency = _text(root, "cbc:DocumentCurrencyCode")
    out.add("Document", "Invoice (UBL 2.1)")
    for key, label, value in (
        ("supplier_name", "Supplier", _party_name(supplier)),
        ("supplier_trn", "Supplier TRN", _supplier_trn(supplier)),
        ("buyer_name", "Buyer", _party_name(customer)),
        ("buyer_trn", "Buyer TRN", _supplier_trn(customer)),
        ("invoice_number", "Invoice number", _text(root, "cbc:ID")),
        ("invoice_date", "Invoice date", _text(root, "cbc:IssueDate")),
        ("due_date", "Due date", _text(root, "cbc:DueDate")),
        ("payment_terms", "Payment terms", _text(root, "cac:PaymentTerms/cbc:Note")),
        ("currency", "Currency", currency),
        ("po_number", "Purchase order", _text(root, "cac:OrderReference/cbc:ID")),
        (
            "subtotal",
            "Subtotal (tax exclusive)",
            _text(totals, "cbc:TaxExclusiveAmount") or _text(totals, "cbc:LineExtensionAmount"),
        ),
        ("vat_rate", "VAT rate (percent)", _vat_rate(root)),
        ("vat_amount", "VAT amount", _tax_amount(root, currency)),
        (
            "total",
            "Total payable",
            _text(totals, "cbc:PayableAmount") or _text(totals, "cbc:TaxInclusiveAmount"),
        ),
    ):
        item = out.add(label, value)
        if item is not None:
            header[key] = item

    bank: dict[str, Any] = {}
    account = _find(root, "cac:PaymentMeans/cac:PayeeFinancialAccount")
    for key, label, value in (
        ("iban", "Payee IBAN", _text(account, "cbc:ID")),
        ("account_name", "Payee account name", _text(account, "cbc:Name")),
        ("swift", "Payee bank BIC", _text(account, "cac:FinancialInstitutionBranch/cbc:ID")),
    ):
        item = out.add(label, value)
        if item is not None:
            bank[key] = item

    lines: list[dict[str, Any]] = []
    elements = root.findall(_path("cac:InvoiceLine"))
    if len(elements) > MAX_LINES:
        warnings.append(
            ParseWarning(code="lines_truncated", message=f"only the first {MAX_LINES} lines read")
        )
    if elements:
        out.lines.append(f"Invoice lines ({', '.join(LINE_COLUMNS)}):")
    for position, element in enumerate(elements[:MAX_LINES], start=1):
        values = {
            "description": _text(element, "cac:Item/cbc:Name"),
            "quantity": _text(element, "cbc:InvoicedQuantity"),
            "unit_price": _text(element, "cac:Price/cbc:PriceAmount"),
            "vat_category": _text(element, "cac:Item/cac:ClassifiedTaxCategory/cbc:ID"),
            "vat_rate": _text(element, "cac:Item/cac:ClassifiedTaxCategory/cbc:Percent"),
            "line_total": _text(element, "cbc:LineExtensionAmount"),
        }
        number = _text(element, "cbc:ID") or str(position)
        # One row per line, values in column order and separated by single spaces: a
        # label repeated on every row would read as keyword stuffing to the guardrails.
        row = f"{position}. " + " ".join(v if v is not None else "-" for v in values.values())
        out.lines.append(row)
        line: dict[str, Any] = {"line_no": number}
        for key, value in values.items():
            if value is not None:
                quote = row if len(row) <= MAX_QUOTE_CHARS else value
                line[key] = {"value": value, "quote": quote}
        lines.append(line)

    notes = [clean(n.text) for n in root.findall(_path("cbc:Note"))]
    for note in [n for n in notes if n][:MAX_NOTES]:
        out.add("Note", note)
    data = {
        "format": UblInvoiceParser.format_id,
        "customization": customization,
        "pint_ae": bool(customization and customization.startswith(PINT_AE_PREFIX)),
        "peppol_bis3": bool(customization and customization.startswith(PEPPOL_BIS3)),
        "header": header,
        "bank": bank,
        "lines": lines,
    }
    return out.lines, data, warnings


def read_xml(path: Path) -> bytes:
    """The bytes of an XML file, refused above ``MAX_XML_BYTES``."""
    with path.open("rb") as handle:
        data = handle.read(MAX_XML_BYTES + 1)
    if len(data) > MAX_XML_BYTES:
        raise ParseError("too_large", "XML document too large")
    return data


def safe_root(data: bytes) -> Element:
    """The root element, read with ``defusedxml``: a DTD, an entity declaration or an
    external reference is refused (``unsafe_xml``), never resolved or fetched."""
    if len(data) > MAX_XML_BYTES:
        raise ParseError("too_large", "XML document too large")
    try:
        root: Element = SafeET.fromstring(
            data, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
    except DefusedXmlException:
        raise ParseError("unsafe_xml", "XML declares a DTD or entities") from None
    except SafeET.ParseError:
        raise ParseError("corrupt", "malformed XML") from None
    return root


def text_page(lines: list[str], number: int = 1) -> Page:
    """A page holding the readable lines of a structured e-invoice (no layout)."""
    blocks = [TextBlock(text=line, source="text", group=index) for index, line in enumerate(lines)]
    return Page(
        number=number, width=0.0, height=0.0, unit="pt", text="\n".join(lines), blocks=blocks
    )


class UblInvoiceParser:
    """UBL 2.1 ``Invoice`` documents (Peppol BIS Billing 3.0, PINT AE)."""

    format_id = "ubl-2.1-invoice"
    version = "2"

    def detect(self, root_tag: str, namespaces: dict[str, str]) -> bool:
        return root_tag == f"{{{INVOICE_NS}}}Invoice"

    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument:
        root = safe_root(read_xml(path))
        if root.tag != f"{{{INVOICE_NS}}}Invoice":
            raise ParseError("unsupported_einvoice_format", "not a UBL invoice")
        lines, structured, warnings = read_invoice(root)
        metadata = {
            DATA_KEY: json.dumps(structured, ensure_ascii=False, sort_keys=True),
            "einvoice:customization": structured["customization"] or "",
        }
        return NormalizedDocument(
            media_type=XML,
            parser=ParserInfo(name="ubl", version=self.version),
            pages=[text_page(lines)],
            metadata=metadata,
            warnings=warnings,
        )
