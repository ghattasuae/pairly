# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""More e-invoice formats: UN/CEFACT CII, Factur-X PDFs and Peppol BIS Billing 3.0.

A few regular synthetic invoices are rendered again in each format, with the same
ground truth (``manifests/einvoices.jsonl``), so a parser test checks every field
against what the XML was written from:

* ``cii``: a CII D16B ``CrossIndustryInvoice`` (EN 16931 guideline), dates in format
  ``102`` (``YYYYMMDD``).
* ``factur-x``: the invoice's own digital PDF with that CII attached as
  ``factur-x.xml``. A minimal Factur-X: the attachment and the XML are what a reader
  needs; the PDF is not converted to PDF/A-3 and carries no Factur-X XMP.
* ``peppol-bis3``: UBL 2.1 with the Peppol BIS Billing 3.0 customization; an invoice in
  a foreign currency also carries the VAT total in AED (a second ``cac:TaxTotal``, at a
  fixed synthetic rate), which a reader must not take for the invoice's VAT.

The selection takes the first invoices (by id) that fit; nothing random.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Final, Literal
from xml.etree import ElementTree as ET

from synth.pay.dataset import Dataset, DocSpec
from synth.pay.invoices import InvoiceDoc
from synth.pay.models import InvoiceTruth, _Model
from synth.pay.render import TRN_SCHEME, render_pdf, render_xml

CII_NS: Final = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}
EN16931: Final = "urn:cen.eu:en16931:2017"
BIS3: Final = "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
# A fixed, made-up conversion for the AED VAT total of a foreign-currency BIS 3 invoice.
SYNTHETIC_AED_RATE: Final = Decimal("4.00")
COUNTS: Final = {"cii": 3, "factur-x": 3, "peppol-bis3": 2}
EXTENSIONS: Final = {"cii": "xml", "factur-x": "pdf", "peppol-bis3": "xml"}

EInvoiceFormat = Literal["cii", "factur-x", "peppol-bis3"]


class EInvoiceSample(_Model):
    id: str
    file: str
    format: EInvoiceFormat
    source_id: str
    tax_currency: str | None = None
    tax_currency_amount: str | None = None
    truth: InvoiceTruth


def _el(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    prefix, _, name = tag.partition(":")
    el = ET.SubElement(parent, f"{{{CII_NS[prefix]}}}{name}", attrs)
    if text is not None:
        el.text = text
    return el


def _date(parent: ET.Element, tag: str, iso: str) -> None:
    _el(_el(parent, tag), "udt:DateTimeString", iso.replace("-", ""), format="102")


def _party(parent: ET.Element, tag: str, name: str, trn: str | None, country: str) -> None:
    party = _el(parent, tag)
    _el(party, "ram:Name", name)
    address = _el(party, "ram:PostalTradeAddress")
    _el(address, "ram:CountryID", country)
    if trn:
        uri = _el(party, "ram:URIUniversalCommunication")
        _el(uri, "ram:URIID", trn, schemeID=TRN_SCHEME)
        _el(_el(party, "ram:SpecifiedTaxRegistration"), "ram:ID", trn, schemeID="VA")


def render_cii(doc: InvoiceDoc) -> bytes:
    """The invoice as a CII D16B ``CrossIndustryInvoice`` (EN 16931 guideline)."""
    for prefix, uri in CII_NS.items():
        ET.register_namespace(prefix, uri)
    truth = doc.truth
    cur = truth.currency
    root = ET.Element(f"{{{CII_NS['rsm']}}}CrossIndustryInvoice")
    context = _el(root, "rsm:ExchangedDocumentContext")
    _el(_el(context, "ram:GuidelineSpecifiedDocumentContextParameter"), "ram:ID", EN16931)
    document = _el(root, "rsm:ExchangedDocument")
    _el(document, "ram:ID", truth.invoice_number)
    _el(document, "ram:TypeCode", "380")
    _date(document, "ram:IssueDateTime", truth.invoice_date)
    for note in doc.notes:
        _el(_el(document, "ram:IncludedNote"), "ram:Content", note)

    transaction = _el(root, "rsm:SupplyChainTradeTransaction")
    for line in truth.lines:
        item = _el(transaction, "ram:IncludedSupplyChainTradeLineItem")
        _el(_el(item, "ram:AssociatedDocumentLineDocument"), "ram:LineID", str(line.line_no))
        _el(_el(item, "ram:SpecifiedTradeProduct"), "ram:Name", line.description)
        price = _el(_el(item, "ram:SpecifiedLineTradeAgreement"), "ram:NetPriceProductTradePrice")
        _el(price, "ram:ChargeAmount", line.unit_price)
        delivery = _el(item, "ram:SpecifiedLineTradeDelivery")
        _el(delivery, "ram:BilledQuantity", line.quantity, unitCode="C62")
        settlement = _el(item, "ram:SpecifiedLineTradeSettlement")
        tax = _el(settlement, "ram:ApplicableTradeTax")
        _el(tax, "ram:TypeCode", "VAT")
        _el(tax, "ram:CategoryCode", line.vat_category)
        _el(tax, "ram:RateApplicablePercent", line.vat_rate)
        summary = _el(settlement, "ram:SpecifiedTradeSettlementLineMonetarySummation")
        _el(summary, "ram:LineTotalAmount", line.line_total)

    agreement = _el(transaction, "ram:ApplicableHeaderTradeAgreement")
    _party(
        agreement,
        "ram:SellerTradeParty",
        truth.vendor_name,
        truth.vendor_trn,
        doc.vendor.address.country,
    )
    _party(agreement, "ram:BuyerTradeParty", truth.buyer_name, truth.buyer_trn, "AE")
    if truth.po_number:
        _el(
            _el(agreement, "ram:BuyerOrderReferencedDocument"),
            "ram:IssuerAssignedID",
            truth.po_number,
        )
    _el(transaction, "ram:ApplicableHeaderTradeDelivery")

    settlement = _el(transaction, "ram:ApplicableHeaderTradeSettlement")
    _el(settlement, "ram:InvoiceCurrencyCode", cur)
    if truth.bank is not None:
        means = _el(settlement, "ram:SpecifiedTradeSettlementPaymentMeans")
        _el(means, "ram:TypeCode", "30")
        account = _el(means, "ram:PayeePartyCreditorFinancialAccount")
        _el(account, "ram:IBANID", truth.bank.iban)
        _el(account, "ram:AccountName", truth.bank.account_name)
        _el(
            _el(means, "ram:PayeeSpecifiedCreditorFinancialInstitution"),
            "ram:BICID",
            truth.bank.bic,
        )
    by_cat: dict[tuple[str, str], list[Decimal]] = {}
    vat_by_cat: dict[tuple[str, str], Decimal] = {}
    for line in truth.lines:
        key = (line.vat_category, line.vat_rate)
        by_cat.setdefault(key, []).append(Decimal(line.line_total))
        vat_by_cat[key] = vat_by_cat.get(key, Decimal(0)) + Decimal(line.vat_amount)
    for (cat, rate), bases in sorted(by_cat.items()):
        tax = _el(settlement, "ram:ApplicableTradeTax")
        _el(tax, "ram:CalculatedAmount", f"{vat_by_cat[(cat, rate)]:.2f}")
        _el(tax, "ram:TypeCode", "VAT")
        if cat == "O":
            _el(tax, "ram:ExemptionReason", "Reverse charge")
        _el(tax, "ram:BasisAmount", f"{sum(bases, Decimal(0)):.2f}")
        _el(tax, "ram:CategoryCode", cat)
        _el(tax, "ram:RateApplicablePercent", rate)
    terms = _el(settlement, "ram:SpecifiedTradePaymentTerms")
    _el(terms, "ram:Description", truth.payment_terms)
    _date(terms, "ram:DueDateDateTime", truth.due_date)
    totals = _el(settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    _el(totals, "ram:LineTotalAmount", truth.subtotal)
    _el(totals, "ram:TaxBasisTotalAmount", truth.subtotal)
    _el(totals, "ram:TaxTotalAmount", truth.vat_amount, currencyID=cur)
    _el(totals, "ram:GrandTotalAmount", truth.total)
    _el(totals, "ram:DuePayableAmount", truth.total)
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def render_facturx(doc: InvoiceDoc) -> bytes:
    """The invoice's digital PDF with its CII attached as ``factur-x.xml``."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(render_pdf(doc))))
    writer.add_attachment("factur-x.xml", render_cii(doc))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _tax_currency(truth: InvoiceTruth) -> tuple[str, str] | None:
    if truth.currency == "AED":
        return None
    return "AED", f"{Decimal(truth.vat_amount) * SYNTHETIC_AED_RATE:.2f}"


def render_sample(sample: EInvoiceSample, doc: InvoiceDoc) -> bytes:
    if sample.format == "cii":
        return render_cii(doc)
    if sample.format == "factur-x":
        return render_facturx(doc)
    tax = (
        (sample.tax_currency, sample.tax_currency_amount)
        if sample.tax_currency and sample.tax_currency_amount
        else None
    )
    return render_xml(doc, customization=BIS3, tax_currency=tax)


def _fits(spec: DocSpec, fmt: str) -> bool:
    record = spec.record
    if record.kind != "invoice" or record.attack is not None:
        return False
    if fmt == "factur-x":
        # The PDF's own text must carry the invoice number and the total.
        return record.format == "pdf" and record.text_layer == "logical"
    if fmt == "peppol-bis3":
        return record.truth.currency != "AED"
    return record.format != "xml"


def samples(ds: Dataset) -> list[tuple[EInvoiceSample, DocSpec]]:
    """The e-invoice samples with the invoice each one renders."""
    chosen: list[tuple[EInvoiceSample, DocSpec]] = []
    used: set[str] = set()
    ordered = sorted(ds.regular(), key=lambda s: s.record.id)
    for fmt, count in COUNTS.items():
        picked = [s for s in ordered if s.record.id not in used and _fits(s, fmt)][:count]
        for index, spec in enumerate(picked, start=1):
            used.add(spec.record.id)
            sample_id = f"EINV-{fmt.upper()}-{index:02d}"
            tax = _tax_currency(spec.record.truth) if fmt == "peppol-bis3" else None
            sample = EInvoiceSample(
                id=sample_id,
                file=f"einvoices/{sample_id}.{EXTENSIONS[fmt]}",
                format=fmt,  # type: ignore[arg-type]
                source_id=spec.record.id,
                tax_currency=tax[0] if tax else None,
                tax_currency_amount=tax[1] if tax else None,
                truth=spec.record.truth,
            )
            chosen.append((sample, spec))
    return chosen
