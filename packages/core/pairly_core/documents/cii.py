# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""UN/CEFACT Cross Industry Invoice (CII D16B, EN 16931): an ``EInvoiceParser``.

CII is the XML inside Factur-X and ZUGFeRD 2 PDFs (``pairly_core.documents.facturx``)
and is also sent on its own. The parser produces exactly what the UBL parser produces
(``pairly_core.documents.ubl``): one readable line per header value, one row per invoice
line, ``Note:`` lines for free text, and ``metadata["einvoice:data"]`` with each value and
its line as the quote, so a workflow maps either format with the same code.

Values are read as the XML states them. The one conversion is a date in CII format
``102`` (``YYYYMMDD``), written as the ISO date it denotes; a date in any other format
is kept as written. A credit note (type code 381 and the other credit codes) is not an
invoice and is refused, like a UBL ``CreditNote``.

Safety is the UBL parser's: ``defusedxml`` with DTDs, entities and external references
forbidden, a size cap, cleaned and capped values, at most ``MAX_LINES`` lines.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final
from xml.etree.ElementTree import Element

from pairly_core.documents.model import NormalizedDocument, ParserInfo, ParseWarning
from pairly_core.documents.registry import ParseError, ParseOptions
from pairly_core.documents.ubl import (
    DATA_KEY,
    LINE_COLUMNS,
    MAX_LINES,
    MAX_NOTES,
    MAX_QUOTE_CHARS,
    Writer,
    clean,
    read_xml,
    safe_root,
    text_page,
)
from pairly_core.ingestion.sniff import XML

RSM: Final = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
RAM: Final = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
UDT: Final = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"
ROOT_TAG: Final = f"{{{RSM}}}CrossIndustryInvoice"
EN16931: Final = "urn:cen.eu:en16931:2017"
# UNTDID 1001 credit note codes: not invoices.
CREDIT_TYPE_CODES: Final = frozenset({"81", "83", "261", "262", "296", "308", "381", "396", "420"})
_NS: Final = {"rsm": RSM, "ram": RAM, "udt": UDT}
_DATE_102 = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def _path(path: str) -> str:
    parts = []
    for step in path.split("/"):
        prefix, _, name = step.partition(":")
        parts.append(f"{{{_NS[prefix]}}}{name}")
    return "/".join(parts)


def _find(node: Element | None, path: str) -> Element | None:
    return None if node is None else node.find(_path(path))


def _text(node: Element | None, path: str) -> str | None:
    found = _find(node, path)
    return clean(found.text) if found is not None else None


def _date(node: Element | None, path: str) -> str | None:
    """A ``udt:DateTimeString``: format 102 as an ISO date, any other format as written."""
    found = _find(node, f"{path}/udt:DateTimeString")
    if found is None:
        return None
    value = clean(found.text)
    match = _DATE_102.match(value or "")
    if match and found.get("format", "102") == "102":
        return f"{match[1]}-{match[2]}-{match[3]}"
    return value


def _tax_id(party: Element | None) -> str | None:
    """The VAT registration (scheme ``VA``), else the fiscal number (``FC``)."""
    if party is None:
        return None
    registrations = party.findall(_path("ram:SpecifiedTaxRegistration/ram:ID"))
    for scheme in ("VA", "FC"):
        for reg in registrations:
            if reg.get("schemeID") == scheme:
                return clean(reg.text)
    return None


def _amount(node: Element | None, path: str, currency: str | None) -> str | None:
    """An amount; when it repeats in several currencies, the one in ``currency``."""
    if node is None:
        return None
    amounts = node.findall(_path(path))
    for amount in amounts:
        if currency is not None and amount.get("currencyID") == currency:
            return clean(amount.text)
    return clean(amounts[0].text) if amounts else None


def _vat_rate(settlement: Element | None) -> str | None:
    """The invoice's VAT rate when its taxed categories share one rate, else None."""
    if settlement is None:
        return None
    rates = set()
    for tax in settlement.findall(_path("ram:ApplicableTradeTax")):
        percent = _text(tax, "ram:RateApplicablePercent")
        category = _text(tax, "ram:CategoryCode")
        if percent is not None and category not in {"Z", "E", "O"}:
            rates.add(percent)
    return next(iter(rates)) if len(rates) == 1 else None


def read_cii(root: Element) -> tuple[list[str], dict[str, Any], list[ParseWarning]]:
    """The readable lines, the structured data and any warnings of one CII invoice."""
    if root.tag != ROOT_TAG:
        raise ParseError("unsupported_einvoice_format", "not a Cross Industry Invoice")
    document = _find(root, "rsm:ExchangedDocument")
    type_code = _text(document, "ram:TypeCode")
    if type_code in CREDIT_TYPE_CODES:
        raise ParseError("unsupported_einvoice_format", "a credit note, not an invoice")
    out = Writer()
    warnings: list[ParseWarning] = []
    guideline = _text(
        root, "rsm:ExchangedDocumentContext/ram:GuidelineSpecifiedDocumentContextParameter/ram:ID"
    )
    transaction = _find(root, "rsm:SupplyChainTradeTransaction")
    agreement = _find(transaction, "ram:ApplicableHeaderTradeAgreement")
    settlement = _find(transaction, "ram:ApplicableHeaderTradeSettlement")
    seller = _find(agreement, "ram:SellerTradeParty")
    buyer = _find(agreement, "ram:BuyerTradeParty")
    terms = _find(settlement, "ram:SpecifiedTradePaymentTerms")
    totals = _find(settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    currency = _text(settlement, "ram:InvoiceCurrencyCode")
    out.add("Document", "Invoice (UN/CEFACT CII)")
    header: dict[str, Any] = {}
    for key, label, value in (
        ("supplier_name", "Supplier", _text(seller, "ram:Name")),
        ("supplier_trn", "Supplier TRN", _tax_id(seller)),
        ("buyer_name", "Buyer", _text(buyer, "ram:Name")),
        ("buyer_trn", "Buyer TRN", _tax_id(buyer)),
        ("invoice_number", "Invoice number", _text(document, "ram:ID")),
        ("invoice_date", "Invoice date", _date(document, "ram:IssueDateTime")),
        ("due_date", "Due date", _date(terms, "ram:DueDateDateTime")),
        ("payment_terms", "Payment terms", _text(terms, "ram:Description")),
        ("currency", "Currency", currency),
        (
            "po_number",
            "Purchase order",
            _text(agreement, "ram:BuyerOrderReferencedDocument/ram:IssuerAssignedID"),
        ),
        (
            "subtotal",
            "Subtotal (tax exclusive)",
            _text(totals, "ram:TaxBasisTotalAmount") or _text(totals, "ram:LineTotalAmount"),
        ),
        ("vat_rate", "VAT rate (percent)", _vat_rate(settlement)),
        ("vat_amount", "VAT amount", _amount(totals, "ram:TaxTotalAmount", currency)),
        (
            "total",
            "Total payable",
            _text(totals, "ram:DuePayableAmount") or _text(totals, "ram:GrandTotalAmount"),
        ),
    ):
        item = out.add(label, value)
        if item is not None:
            header[key] = item

    bank: dict[str, Any] = {}
    means = _find(settlement, "ram:SpecifiedTradeSettlementPaymentMeans")
    for key, label, value in (
        ("iban", "Payee IBAN", _text(means, "ram:PayeePartyCreditorFinancialAccount/ram:IBANID")),
        (
            "account_name",
            "Payee account name",
            _text(means, "ram:PayeePartyCreditorFinancialAccount/ram:AccountName"),
        ),
        (
            "swift",
            "Payee bank BIC",
            _text(means, "ram:PayeeSpecifiedCreditorFinancialInstitution/ram:BICID"),
        ),
    ):
        item = out.add(label, value)
        if item is not None:
            bank[key] = item

    lines: list[dict[str, Any]] = []
    elements = (
        []
        if transaction is None
        else transaction.findall(_path("ram:IncludedSupplyChainTradeLineItem"))
    )
    if len(elements) > MAX_LINES:
        warnings.append(
            ParseWarning(code="lines_truncated", message=f"only the first {MAX_LINES} lines read")
        )
    if elements:
        out.lines.append(f"Invoice lines ({', '.join(LINE_COLUMNS)}):")
    for position, element in enumerate(elements[:MAX_LINES], start=1):
        tax = "ram:SpecifiedLineTradeSettlement/ram:ApplicableTradeTax"
        values = {
            "description": _text(element, "ram:SpecifiedTradeProduct/ram:Name"),
            "quantity": _text(element, "ram:SpecifiedLineTradeDelivery/ram:BilledQuantity"),
            "unit_price": _text(
                element,
                "ram:SpecifiedLineTradeAgreement/ram:NetPriceProductTradePrice/ram:ChargeAmount",
            ),
            "vat_category": _text(element, f"{tax}/ram:CategoryCode"),
            "vat_rate": _text(element, f"{tax}/ram:RateApplicablePercent"),
            "line_total": _text(
                element,
                "ram:SpecifiedLineTradeSettlement/"
                "ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount",
            ),
        }
        number = _text(element, "ram:AssociatedDocumentLineDocument/ram:LineID") or str(position)
        row = f"{position}. " + " ".join(v if v is not None else "-" for v in values.values())
        out.lines.append(row)
        line: dict[str, Any] = {"line_no": number}
        for key, value in values.items():
            if value is not None:
                quote = row if len(row) <= MAX_QUOTE_CHARS else value
                line[key] = {"value": value, "quote": quote}
        lines.append(line)

    notes = (
        []
        if document is None
        else [clean(n.text) for n in document.findall(_path("ram:IncludedNote/ram:Content"))]
    )
    for note in [n for n in notes if n][:MAX_NOTES]:
        out.add("Note", note)
    data = {
        "format": CiiInvoiceParser.format_id,
        "customization": guideline,
        "en16931": bool(guideline and guideline.startswith(EN16931)),
        "type_code": type_code,
        "header": header,
        "bank": bank,
        "lines": lines,
    }
    return out.lines, data, warnings


class CiiInvoiceParser:
    """UN/CEFACT ``CrossIndustryInvoice`` documents (CII D16B, Factur-X, ZUGFeRD 2)."""

    format_id = "un-cefact-cii"
    version = "1"

    def detect(self, root_tag: str, namespaces: dict[str, str]) -> bool:
        return root_tag == ROOT_TAG

    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument:
        lines, structured, warnings = read_cii(safe_root(read_xml(path)))
        return NormalizedDocument(
            media_type=XML,
            parser=ParserInfo(name="cii", version=self.version),
            pages=[text_page(lines)],
            metadata={
                DATA_KEY: json.dumps(structured, ensure_ascii=False, sort_keys=True),
                "einvoice:customization": structured["customization"] or "",
            },
            warnings=warnings,
        )
