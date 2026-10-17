# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Render an ``InvoiceDoc``: digital PDF (reportlab), scan (Pillow), DOCX (python-docx)
and structured e-invoice XML (UBL 2.1 in the Peppol PINT AE shape).

Every renderer is deterministic: reportlab with ``invariant=1`` (the PDF dates are then
rewritten in place to the invoice's own dates, same length, so offsets stay valid),
seeded scanner damage, DOCX zips with fixed timestamps, XML from sorted, fixed input.

The XML is a minimal valid structure, not a certified PINT AE document: it has the
header, parties with TRN, payment means with the IBAN, tax totals by category,
monetary totals and lines, which is what an extractor needs. It is not validated
against the PINT AE schematron.
"""

from __future__ import annotations

import datetime as dt
import io
import random
import re
import zipfile
from functools import cache
from typing import Any, Final
from xml.etree import ElementTree as ET

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from pairly_core.decisions.bidi import has_rtl
from pairly_core.decisions.receipts import FONT_PATH
from synth.hire.facts import stable_seed
from synth.pay.banking import format_iban
from synth.pay.catalog import BUYER_ADDRESS
from synth.pay.invoices import InvoiceDoc, print_date, printed_money
from synth.pay.layout import LABELS, Box, H, Rule, W, arabic_font, compose, visual

DPI: Final = 150
SCALE: Final = DPI / 72
FIXED_TIME: Final = dt.datetime(2026, 9, 1, 9, 0, 0)
PDF_DATE_RE: Final = re.compile(rb"/(CreationDate|ModDate) \(D:(\d{14})([^)]*)\)")


# ---------------------------------------------------------------------------- PDF


def _bold(font: str) -> str:
    return {"Helvetica": "Helvetica-Bold", "Times-Roman": "Times-Bold", "Courier": "Courier-Bold"}[
        font
    ]


def pdf_date(date: str, hour: int = 9, minute: int = 30) -> str:
    """``2026-03-15`` -> ``20260315093000`` (the 14 digits a PDF date starts with)."""
    return date.replace("-", "") + f"{hour:02d}{minute:02d}00"


def render_pdf(doc: InvoiceDoc) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(W, H), invariant=1)
    meta = {
        "title": f"Invoice {doc.truth.invoice_number}",
        "author": doc.vendor.legal_name,
        "creator": doc.producer,
        "subject": "",
        "keywords": "",
        **doc.metadata,
    }
    pdf.setTitle(meta["title"])
    pdf.setAuthor(meta["author"])
    pdf.setCreator(meta["creator"])
    if meta.get("producer"):
        pdf.setProducer(meta["producer"])
    for h in doc.hidden:
        if h.mode == "meta_subject":
            meta["subject"] = h.text
        elif h.mode == "meta_keywords":
            meta["keywords"] = h.text
    if meta["subject"]:
        pdf.setSubject(meta["subject"])
    if meta["keywords"]:
        pdf.setKeywords(meta["keywords"])
    latin, bold = doc.font, _bold(doc.font)
    arabic = arabic_font()
    for item in compose(doc):
        if isinstance(item, Box):
            pdf.saveState()
            if item.fill is not None:
                pdf.setFillColorRGB(*item.fill)
            if item.stroke is not None:
                pdf.setStrokeColorRGB(*item.stroke)
                pdf.setLineWidth(0.6)
            if item.tag == "logo_circle":
                pdf.circle(
                    item.x + item.w / 2, H - item.y - item.h / 2, item.w / 2, stroke=0, fill=1
                )
            else:
                pdf.rect(
                    item.x,
                    H - item.y - item.h,
                    item.w,
                    item.h,
                    stroke=1 if item.stroke else 0,
                    fill=1 if item.fill else 0,
                )
            pdf.restoreState()
        elif isinstance(item, Rule):
            pdf.saveState()
            pdf.setStrokeColorRGB(*item.color)
            pdf.setLineWidth(item.width)
            pdf.line(item.x0, H - item.y0, item.x1, H - item.y1)
            pdf.restoreState()
        else:
            rtl = has_rtl(item.text)
            font = arabic if rtl else (bold if item.bold else latin)
            text = visual(item.text)
            pdf.setFont(font, item.size)
            pdf.setFillColorRGB(*item.color)
            y = H - item.y
            if item.align == "r":
                pdf.drawRightString(item.x, y, text)
            elif item.align == "c":
                pdf.drawCentredString(item.x, y, text)
            else:
                pdf.drawString(item.x, y, text)
    pdf.showPage()
    pdf.save()
    data = buf.getvalue()
    created, modified = doc.pdf_dates or (
        pdf_date(doc.truth.invoice_date),
        pdf_date(doc.truth.invoice_date),
    )
    return _set_dates(data, created, modified)


def _set_dates(data: bytes, created: str, modified: str) -> bytes:
    """Rewrite the invariant dates in place (same length: xref offsets stay valid)."""

    def repl(match: re.Match[bytes]) -> bytes:
        stamp = created if match.group(1) == b"CreationDate" else modified
        return b"/" + match.group(1) + b" (D:" + stamp.encode() + b"+04'00')"

    out, n = PDF_DATE_RE.subn(repl, data)
    if n != 2 or len(out) != len(data):
        raise RuntimeError("unexpected PDF info dictionary")
    return out


# ---------------------------------------------------------------------------- scans


@cache
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def _grey(rgb: tuple[float, float, float]) -> int:
    return int(255 * (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]))


def _scan_page(doc: InvoiceDoc) -> Image.Image:
    page = Image.new("L", (round(W * SCALE), round(H * SCALE)), 255)
    draw = ImageDraw.Draw(page)
    for item in compose(doc):
        if getattr(item, "tag", "") == "hidden":
            continue
        if isinstance(item, Box):
            box = (
                item.x * SCALE,
                item.y * SCALE,
                (item.x + item.w) * SCALE,
                (item.y + item.h) * SCALE,
            )
            fill = _grey(item.fill) if item.fill is not None else None
            outline = _grey(item.stroke) if item.stroke is not None else None
            if item.tag == "logo_circle":
                draw.ellipse(box, fill=fill, outline=outline)
            else:
                draw.rectangle(box, fill=fill, outline=outline)
        elif isinstance(item, Rule):
            draw.line(
                (item.x0 * SCALE, item.y0 * SCALE, item.x1 * SCALE, item.y1 * SCALE),
                fill=_grey(item.color),
                width=max(1, round(item.width * SCALE)),
            )
        else:
            size = max(1, round(item.size * SCALE))
            font = _font(size)
            text = visual(item.text)
            w = font.getlength(text)
            x = item.x * SCALE
            if item.align == "r":
                x -= w
            elif item.align == "c":
                x -= w / 2
            y = item.y * SCALE - size * 0.95
            draw.text(
                (x, y),
                text,
                font=font,
                fill=_grey(item.color),
                stroke_width=1 if item.bold and size >= 26 else 0,
                stroke_fill=_grey(item.color),
            )
    return page


def _damage(page: Image.Image, rng: random.Random) -> Image.Image:
    width, height = page.size
    out = page.rotate(
        rng.uniform(-1.8, 1.8), resample=Image.Resampling.BICUBIC, expand=False, fillcolor=255
    )
    draw = ImageDraw.Draw(out)
    for _ in range(rng.randint(500, 1600)):
        draw.point((rng.randrange(width), rng.randrange(height)), fill=rng.randint(0, 150))
    for _ in range(rng.randint(0, 3)):
        y = rng.randrange(height)
        draw.line((0, y, width, y + rng.randint(-3, 3)), fill=rng.randint(195, 235), width=1)
    radius = rng.choice((0.0, 0.4, 0.6, 0.8))
    if radius:
        out = out.filter(ImageFilter.GaussianBlur(radius))
    return out


def render_scan(doc: InvoiceDoc) -> bytes:
    rng = random.Random(stable_seed(doc.seed, doc.id, "scan"))
    page = _damage(_scan_page(doc), rng)
    buf = io.BytesIO()
    if doc.format == "jpg":
        page.save(buf, format="JPEG", quality=rng.randint(40, 75), optimize=False)
    elif doc.format == "png":
        page.save(buf, format="PNG", optimize=False)
    else:  # scan_pdf: what a scanner's "scan to PDF" produces
        page.save(
            buf,
            format="PDF",
            resolution=float(DPI),
            creationDate=FIXED_TIME.timetuple(),
            modDate=FIXED_TIME.timetuple(),
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------- DOCX


def render_docx(doc: InvoiceDoc) -> bytes:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    truth = doc.truth
    rtl = doc.layout == "rtl"
    document = docx.Document()
    core = document.core_properties
    core.author = doc.metadata.get("author", doc.vendor.legal_name)
    core.title = doc.metadata.get("title", f"Invoice {truth.invoice_number}")
    core.created = FIXED_TIME
    core.modified = FIXED_TIME
    core.last_modified_by = doc.metadata.get("last_modified_by", core.author)
    core.revision = int(doc.metadata.get("revision", "1"))
    for h in doc.hidden:
        if h.mode == "meta_subject":
            core.subject = h.text
        elif h.mode == "meta_keywords":
            core.keywords = h.text
    style = document.styles["Normal"]
    style.font.name = {
        "Helvetica": "Arial",
        "Times-Roman": "Times New Roman",
        "Courier": "Courier New",
    }[doc.font]
    style.font.size = Pt(10)

    def bidi(paragraph: Any) -> None:
        if not rtl:
            return
        ppr = paragraph._p.get_or_add_pPr()
        ppr.append(OxmlElement("w:bidi"))
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for run in paragraph.runs:
            rpr = run._r.get_or_add_rPr()
            rpr.append(OxmlElement("w:rtl"))

    def para(text: str, *, bold: bool = False, size: float | None = None) -> Any:
        p = document.add_paragraph()
        run = p.add_run(text)
        run.bold = bold
        if size:
            run.font.size = Pt(size)
        bidi(p)
        return p

    def label(key: str) -> str:
        en, ar = LABELS[key]
        if rtl:
            return ar
        return f"{en} / {ar}" if doc.layout == "bilingual" else en

    accent = doc.accent.lstrip("#")
    p = para(truth.vendor_name, bold=True, size=16)
    p.runs[0].font.color.rgb = RGBColor.from_string(accent.upper())
    if doc.layout == "bilingual" and truth.vendor_name_ar:
        para(truth.vendor_name_ar, size=12)
    address = doc.vendor.address
    para(f"{address.line1}, {address.line2}, {address.city}")
    if truth.vendor_trn:
        para(f"{label('trn')}: {truth.vendor_trn}")
    para(label("tax_invoice" if truth.vendor_trn else "invoice"), bold=True, size=14)
    fmt = doc.date_format
    rows = [
        ("number", truth.invoice_number),
        ("date", print_date(truth.invoice_date, fmt)),
        ("due", print_date(truth.due_date, fmt)),
        ("terms", truth.payment_terms),
    ]
    if truth.po_number:
        rows.append(("po", truth.po_number))
    rows.append(("currency", truth.currency))
    for key, value in rows:
        para(f"{label(key)}: {value}")
    para(f"{label('bill_to')}: {truth.buyer_name}, {', '.join(BUYER_ADDRESS)}")
    para(f"{label('trn')}: {truth.buyer_trn}")
    heads = ["no", "description", "qty", "unit", "unit_price", "vat_pct", "vat", "amount"]
    table = document.add_table(rows=1, cols=len(heads))
    table.style = "Table Grid"
    for cell, key in zip(table.rows[0].cells, heads, strict=True):
        cell.text = label(key) if key != "no" else "#"
    for line in truth.lines:
        cells = table.add_row().cells
        values = [
            str(line.line_no),
            line.description,
            line.quantity,
            line.unit,
            printed_money(line.unit_price),
            f"{line.vat_rate.rstrip('0').rstrip('.')}%",
            printed_money(line.vat_amount),
            printed_money(line.line_total),
        ]
        for cell, value in zip(cells, values, strict=True):
            cell.text = value
    if rtl:
        tbl_pr = table._tbl.tblPr
        tbl_pr.append(OxmlElement("w:bidiVisual"))
    rate = truth.vat_rate.rstrip("0").rstrip(".")
    para(f"{label('subtotal')}: {printed_money(truth.subtotal)}")
    para(f"{label('vat_total')} {rate}%: {printed_money(truth.vat_amount)}")
    para(f"{label('total')}: {truth.currency} {printed_money(truth.total)}", bold=True)
    if truth.bank is not None:
        bank = truth.bank
        para(label("bank"), bold=True)
        para(f"{label('account_name')}: {bank.account_name}")
        para(f"{label('bank_name')}: {bank.bank_name}")
        para(f"{label('iban')}: {format_iban(bank.iban)}")
        para(f"{label('swift')}: {bank.bic}")
    for note in doc.notes:
        para(note, bold=True)
    for h in doc.hidden:
        if h.mode == "docx_hidden":
            p = document.add_paragraph()
            run = p.add_run(h.text)
            rpr = run._r.get_or_add_rPr()
            rpr.append(OxmlElement("w:vanish"))
        elif h.mode == "white":
            p = document.add_paragraph()
            run = p.add_run(h.text)
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            run.font.size = Pt(2)
    del qn
    buf = io.BytesIO()
    document.save(buf)
    return _rezip(buf.getvalue())


def _rezip(data: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            fixed = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            fixed.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(fixed, src.read(info.filename))
    return out.getvalue()


# ---------------------------------------------------------------------------- UBL XML

NS: Final = {
    "": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}
CUSTOMIZATION: Final = "urn:peppol:pint:billing-1@ae-1"
PROFILE: Final = "urn:peppol:bis:billing"
TRN_SCHEME: Final = "0235"  # PINT AE endpoint scheme for a UAE TIN


def _el(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    prefix, _, name = tag.partition(":")
    el = ET.SubElement(parent, f"{{{NS[prefix]}}}{name}", attrs)
    if text is not None:
        el.text = text
    return el


def render_xml(
    doc: InvoiceDoc,
    *,
    customization: str = CUSTOMIZATION,
    tax_currency: tuple[str, str] | None = None,
) -> bytes:
    """UBL 2.1. ``customization`` picks the profile (PINT AE by default, or Peppol BIS
    Billing 3.0); ``tax_currency`` (code, VAT amount) adds the second ``cac:TaxTotal`` in
    the VAT accounting currency that BIS 3 allows."""
    for prefix, uri in NS.items():
        ET.register_namespace(prefix, uri)
    truth = doc.truth
    vendor = doc.vendor
    cur = truth.currency
    root = ET.Element(f"{{{NS['']}}}Invoice")
    _el(root, "cbc:CustomizationID", customization)
    _el(root, "cbc:ProfileID", PROFILE)
    _el(root, "cbc:ID", truth.invoice_number)
    _el(root, "cbc:IssueDate", truth.invoice_date)
    _el(root, "cbc:DueDate", truth.due_date)
    _el(root, "cbc:InvoiceTypeCode", "380")
    for note in doc.notes:
        _el(root, "cbc:Note", note)
    for h in doc.hidden:
        if h.mode == "xml_note":
            _el(root, "cbc:Note", h.text)
    _el(root, "cbc:DocumentCurrencyCode", cur)
    if tax_currency is not None:
        _el(root, "cbc:TaxCurrencyCode", tax_currency[0])
    if truth.po_number:
        _el(_el(root, "cac:OrderReference"), "cbc:ID", truth.po_number)

    supplier = _el(_el(root, "cac:AccountingSupplierParty"), "cac:Party")
    if truth.vendor_trn:
        _el(supplier, "cbc:EndpointID", truth.vendor_trn, schemeID=TRN_SCHEME)
    _el(_el(supplier, "cac:PartyName"), "cbc:Name", truth.vendor_name)
    address = _el(supplier, "cac:PostalAddress")
    _el(address, "cbc:StreetName", vendor.address.line1)
    _el(address, "cbc:AdditionalStreetName", vendor.address.line2)
    _el(address, "cbc:CityName", vendor.address.city)
    _el(address, "cbc:CountrySubentity", vendor.address.region)
    _el(_el(address, "cac:Country"), "cbc:IdentificationCode", vendor.address.country)
    if truth.vendor_trn:
        tax = _el(supplier, "cac:PartyTaxScheme")
        _el(tax, "cbc:CompanyID", truth.vendor_trn)
        _el(_el(tax, "cac:TaxScheme"), "cbc:ID", "VAT")
    legal = _el(supplier, "cac:PartyLegalEntity")
    _el(legal, "cbc:RegistrationName", truth.vendor_name)
    _el(legal, "cbc:CompanyID", vendor.trade_licence)
    contact = _el(supplier, "cac:Contact")
    _el(contact, "cbc:Telephone", vendor.phone_on_file)
    _el(contact, "cbc:ElectronicMail", vendor.sender_addresses[0])

    customer = _el(_el(root, "cac:AccountingCustomerParty"), "cac:Party")
    _el(customer, "cbc:EndpointID", truth.buyer_trn, schemeID=TRN_SCHEME)
    _el(_el(customer, "cac:PartyName"), "cbc:Name", truth.buyer_name)
    caddr = _el(customer, "cac:PostalAddress")
    _el(caddr, "cbc:StreetName", BUYER_ADDRESS[0])
    _el(caddr, "cbc:CityName", "Dubai")
    _el(_el(caddr, "cac:Country"), "cbc:IdentificationCode", "AE")
    ctax = _el(customer, "cac:PartyTaxScheme")
    _el(ctax, "cbc:CompanyID", truth.buyer_trn)
    _el(_el(ctax, "cac:TaxScheme"), "cbc:ID", "VAT")
    _el(_el(customer, "cac:PartyLegalEntity"), "cbc:RegistrationName", truth.buyer_name)

    if truth.bank is not None:
        means = _el(root, "cac:PaymentMeans")
        _el(means, "cbc:PaymentMeansCode", "30")
        account = _el(means, "cac:PayeeFinancialAccount")
        _el(account, "cbc:ID", truth.bank.iban)
        _el(account, "cbc:Name", truth.bank.account_name)
        branch = _el(account, "cac:FinancialInstitutionBranch")
        _el(branch, "cbc:ID", truth.bank.bic)
    _el(_el(root, "cac:PaymentTerms"), "cbc:Note", truth.payment_terms)

    tax_total = _el(root, "cac:TaxTotal")
    _el(tax_total, "cbc:TaxAmount", truth.vat_amount, currencyID=cur)
    by_cat: dict[tuple[str, str], list[Any]] = {}
    for line in truth.lines:
        by_cat.setdefault((line.vat_category, line.vat_rate), []).append(line)
    from decimal import Decimal

    for (cat, rate), lines in sorted(by_cat.items()):
        sub = _el(tax_total, "cac:TaxSubtotal")
        base = sum((Decimal(x.line_total) for x in lines), Decimal(0))
        vat = sum((Decimal(x.vat_amount) for x in lines), Decimal(0))
        _el(sub, "cbc:TaxableAmount", f"{base:.2f}", currencyID=cur)
        _el(sub, "cbc:TaxAmount", f"{vat:.2f}", currencyID=cur)
        tcat = _el(sub, "cac:TaxCategory")
        _el(tcat, "cbc:ID", cat)
        _el(tcat, "cbc:Percent", rate)
        if cat == "O":
            _el(tcat, "cbc:TaxExemptionReason", "Reverse charge")
        _el(_el(tcat, "cac:TaxScheme"), "cbc:ID", "VAT")

    if tax_currency is not None:
        _el(_el(root, "cac:TaxTotal"), "cbc:TaxAmount", tax_currency[1], currencyID=tax_currency[0])

    totals = _el(root, "cac:LegalMonetaryTotal")
    _el(totals, "cbc:LineExtensionAmount", truth.subtotal, currencyID=cur)
    _el(totals, "cbc:TaxExclusiveAmount", truth.subtotal, currencyID=cur)
    _el(totals, "cbc:TaxInclusiveAmount", truth.total, currencyID=cur)
    _el(totals, "cbc:PayableAmount", truth.total, currencyID=cur)

    for line in truth.lines:
        il = _el(root, "cac:InvoiceLine")
        _el(il, "cbc:ID", str(line.line_no))
        _el(il, "cbc:InvoicedQuantity", line.quantity, unitCode="C62")
        _el(il, "cbc:LineExtensionAmount", line.line_total, currencyID=cur)
        item = _el(il, "cac:Item")
        _el(item, "cbc:Name", line.description)
        tcat = _el(item, "cac:ClassifiedTaxCategory")
        _el(tcat, "cbc:ID", line.vat_category)
        _el(tcat, "cbc:Percent", line.vat_rate)
        _el(_el(tcat, "cac:TaxScheme"), "cbc:ID", "VAT")
        _el(_el(il, "cac:Price"), "cbc:PriceAmount", line.unit_price, currencyID=cur)
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


# ---------------------------------------------------------------------------- body


def render_body(doc: InvoiceDoc) -> str:
    """The invoice as plain text in an email body (``email_body`` format)."""
    truth = doc.truth
    fmt = doc.date_format
    out = [
        f"{truth.vendor_name}",
        f"TRN: {truth.vendor_trn}" if truth.vendor_trn else "",
        "",
        "TAX INVOICE" if truth.vendor_trn else "INVOICE",
        f"Invoice No.: {truth.invoice_number}",
        f"Invoice date: {print_date(truth.invoice_date, fmt)}",
        f"Due date: {print_date(truth.due_date, fmt)}",
        f"Payment terms: {truth.payment_terms}",
    ]
    if truth.po_number:
        out.append(f"PO number: {truth.po_number}")
    out += [f"Bill to: {truth.buyer_name} (TRN {truth.buyer_trn})", ""]
    for line in truth.lines:
        out.append(
            f"{line.line_no}. {line.description} | {line.quantity} {line.unit} x "
            f"{printed_money(line.unit_price)} | VAT {line.vat_rate.rstrip('0').rstrip('.')}% "
            f"{printed_money(line.vat_amount)} | {printed_money(line.line_total)}"
        )
    rate = truth.vat_rate.rstrip("0").rstrip(".")
    out += [
        "",
        f"Subtotal: {printed_money(truth.subtotal)}",
        f"VAT {rate}%: {printed_money(truth.vat_amount)}",
        f"Total: {truth.currency} {printed_money(truth.total)}",
    ]
    if truth.bank is not None:
        out += [
            "",
            f"Account name: {truth.bank.account_name}",
            f"Bank: {truth.bank.bank_name}",
            f"IBAN: {format_iban(truth.bank.iban)}",
            f"SWIFT: {truth.bank.bic}",
        ]
    out += doc.notes
    return "\n".join(x for x in out if x is not None) + "\n"


def render(doc: InvoiceDoc) -> bytes:
    if doc.format == "pdf":
        return render_pdf(doc)
    if doc.format in ("png", "jpg", "scan_pdf"):
        return render_scan(doc)
    if doc.format == "docx":
        return render_docx(doc)
    if doc.format == "xml":
        return render_xml(doc)
    if doc.format == "email_body":
        return render_body(doc).encode("utf-8")
    raise ValueError(doc.format)
