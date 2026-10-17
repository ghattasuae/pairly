# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The UBL 2.1 / Peppol PINT AE e-invoice parser on the synthetic Pay invoices.

Every value must come out exactly as the XML states it (the synthetic ground truth is
what the XML was written from), the parser must never resolve a DTD, an entity or an
external reference, and notes (where injected instructions hide) must stay in the text
the guardrails read.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import pytest

from pairly_core.documents.einvoice import XmlParser
from pairly_core.documents.model import REVIEW_WARNINGS, NormalizedDocument
from pairly_core.documents.registry import (
    EInvoiceRegistry,
    ParseError,
    ParseOptions,
    default_registry,
)
from pairly_core.documents.ubl import DATA_KEY, MAX_VALUE_CHARS, UblInvoiceParser
from pairly_core.guardrails import ScanContext, scan_document
from pairly_core.ingestion import sniff as sn
from synth.pay.dataset import DocSpec, build

OPTIONS = ParseOptions(ocr_languages="eng")
NS = (
    "xmlns='urn:oasis:names:specification:ubl:schema:xsd:Invoice-2' "
    "xmlns:cbc='urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2' "
    "xmlns:cac='urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'"
)


@cache
def xml_invoices() -> tuple[DocSpec, ...]:
    return tuple(s for s in build().docs if s.record.format == "xml")


def _parse(tmp_path: Path, data: bytes, name: str = "inv.xml") -> NormalizedDocument:
    path = tmp_path / name
    path.write_bytes(data)
    return default_registry().get(sn.XML).parse(path, OPTIONS)


def test_registered_by_default() -> None:
    parser = default_registry().get(sn.XML)
    assert isinstance(parser, XmlParser)
    assert parser.einvoices.formats == {"ubl-2.1-invoice", "un-cefact-cii"}


def test_every_synthetic_xml_invoice_is_read_exactly(tmp_path: Path) -> None:
    specs = xml_invoices()
    assert len(specs) >= 20
    for spec in specs:
        path = tmp_path / f"{spec.record.id}.xml"
        path.write_bytes(spec.render())
        doc = default_registry().get(sn.XML).parse(path, OPTIONS)
        assert doc.metadata["einvoice:format"] == "ubl-2.1-invoice"
        data = json.loads(doc.metadata[DATA_KEY])
        assert data["pint_ae"] is True
        truth = spec.record.truth
        header = {k: v["value"] for k, v in data["header"].items()}
        expected = {
            "supplier_name": truth.vendor_name,
            "supplier_trn": truth.vendor_trn,
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
        assert {k: header.get(k) for k in expected} == expected, spec.record.id
        bank = {k: v["value"] for k, v in data["bank"].items()}
        if truth.bank is None:
            assert bank == {}
        else:
            assert bank == {
                "iban": truth.bank.iban,
                "account_name": truth.bank.account_name,
                "swift": truth.bank.bic,
            }
        assert len(data["lines"]) == len(truth.lines)
        for line, want in zip(data["lines"], truth.lines, strict=True):
            got = {k: v["value"] for k, v in line.items() if k != "line_no"}
            assert got == {
                "description": want.description,
                "quantity": want.quantity,
                "unit_price": want.unit_price,
                "vat_category": want.vat_category,
                "vat_rate": want.vat_rate,
                "line_total": want.line_total,
            }
        # Every quote is a line of the text the guardrails and the locator see.
        lines = set(doc.text.split("\n"))
        quotes = [v["quote"] for v in data["header"].values()]
        quotes += [v["quote"] for line in data["lines"] for k, v in line.items() if k != "line_no"]
        assert all(q in lines for q in quotes)


def test_notes_stay_in_the_text_for_the_guardrails(tmp_path: Path) -> None:
    injected = [s for s in xml_invoices() if s.record.attack and s.record.attack.payloads]
    assert injected, "the synthetic set has an XML invoice with an injected note"
    spec = injected[0]
    path = tmp_path / "note.xml"
    path.write_bytes(spec.render())
    doc = default_registry().get(sn.XML).parse(path, OPTIONS)
    assert any(line.startswith("Note: ") for line in doc.text.split("\n"))
    report = scan_document(doc, context=ScanContext(filename="note.xml"))
    assert report.flagged_findings()


@pytest.mark.parametrize(
    "data",
    [
        b"<?xml version='1.0'?><!DOCTYPE Invoice [<!ENTITY x SYSTEM 'file:///etc/passwd'>]>"
        b"<Invoice xmlns='urn:oasis:names:specification:ubl:schema:xsd:Invoice-2'>&x;</Invoice>",
        b"<?xml version='1.0'?><!DOCTYPE Invoice SYSTEM 'http://example.test/x.dtd'>"
        b"<Invoice xmlns='urn:oasis:names:specification:ubl:schema:xsd:Invoice-2'/>",
        b"<?xml version='1.0'?><!DOCTYPE l [<!ENTITY a 'aaaa'><!ENTITY b '&a;&a;&a;'>]>"
        b"<Invoice xmlns='urn:oasis:names:specification:ubl:schema:xsd:Invoice-2'>&b;</Invoice>",
    ],
)
def test_never_resolves_dtds_or_entities(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "evil.xml"
    path.write_bytes(data)
    with pytest.raises(ParseError) as exc:
        UblInvoiceParser().parse(path, OPTIONS)
    assert exc.value.code == "unsafe_xml"
    with pytest.raises(ParseError) as exc:
        XmlParser(EInvoiceRegistry([UblInvoiceParser()])).parse(path, OPTIONS)
    assert exc.value.code == "unsafe_xml"


def test_values_are_cleaned_and_capped(tmp_path: Path) -> None:
    long_name = "A" * 1000
    xml = (
        f"<?xml version='1.0'?><Invoice {NS}><cbc:ID>INV‮-7\n\tX</cbc:ID>"
        "<cac:AccountingSupplierParty><cac:Party><cac:PartyName>"
        f"<cbc:Name>{long_name}</cbc:Name></cac:PartyName></cac:Party>"
        "</cac:AccountingSupplierParty></Invoice>"
    ).encode()
    doc = _parse(tmp_path, xml)
    data = json.loads(doc.metadata[DATA_KEY])
    assert data["header"]["invoice_number"]["value"] == "INV‮-7 X"
    assert len(data["header"]["supplier_name"]["value"]) == MAX_VALUE_CHARS
    assert data["pint_ae"] is False
    assert all("\n" not in q["quote"] for q in data["header"].values())


def test_line_cap_sends_the_document_to_review(tmp_path: Path) -> None:
    lines = "".join(
        f"<cac:InvoiceLine><cbc:ID>{i}</cbc:ID><cbc:LineExtensionAmount currencyID='AED'>1.00"
        "</cbc:LineExtensionAmount></cac:InvoiceLine>"
        for i in range(501)
    )
    doc = _parse(tmp_path, f"<?xml version='1.0'?><Invoice {NS}>{lines}</Invoice>".encode())
    assert "lines_truncated" in doc.warning_codes()
    assert "lines_truncated" in REVIEW_WARNINGS


def test_other_xml_is_not_an_invoice(tmp_path: Path) -> None:
    path = tmp_path / "credit.xml"
    path.write_bytes(
        b"<?xml version='1.0'?><CreditNote xmlns='urn:oasis:names:specification:ubl:schema:"
        b"xsd:CreditNote-2'/>"
    )
    with pytest.raises(ParseError) as exc:
        default_registry().get(sn.XML).parse(path, OPTIONS)
    assert exc.value.code == "unsupported_einvoice_format"
    with pytest.raises(ParseError) as exc:
        UblInvoiceParser().parse(path, OPTIONS)
    assert exc.value.code == "unsupported_einvoice_format"
