# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The synthetic Pay dataset (WP4.1): determinism, IBANs, TRNs, arithmetic, matching
cases, attacks, emails and the committed manifests."""

from __future__ import annotations

import email
import hashlib
import io
import json
import random
import zipfile
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from email import policy
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from pypdf import PdfReader

from synth.pay.__main__ import MANIFEST_DIR, manifests
from synth.pay.__main__ import main as cli
from synth.pay.attacks import APPROVAL_THRESHOLD, lookalike, transpose
from synth.pay.banking import BANKS, iban_is_valid, make_iban, trn_is_well_formed
from synth.pay.dataset import DEFAULT_COUNT, DEFAULT_SEED, Dataset, build
from synth.pay.emails import render_email
from synth.pay.models import InvoiceTruth

EM_DASH = chr(0x2014)
CENT = Decimal("0.01")


@pytest.fixture(scope="module")
def ds() -> Dataset:
    return build()


def _sum(values: object) -> Decimal:
    return sum((Decimal(v) for v in values), Decimal(0))  # type: ignore[attr-defined]


def _consistent(t: InvoiceTruth) -> None:
    for line in t.lines:
        total = (Decimal(line.quantity) * Decimal(line.unit_price)).quantize(CENT, ROUND_HALF_UP)
        assert Decimal(line.line_total) == total, line
        vat = (total * Decimal(line.vat_rate) / 100).quantize(CENT, ROUND_HALF_UP)
        assert Decimal(line.vat_amount) == vat, line
        assert line.vat_rate == {"S": "5.00", "Z": "0.00", "O": "0.00"}[line.vat_category]
    assert Decimal(t.subtotal) == _sum(x.line_total for x in t.lines)
    assert Decimal(t.vat_amount) == _sum(x.vat_amount for x in t.lines)
    assert Decimal(t.total) == Decimal(t.subtotal) + Decimal(t.vat_amount)
    for value in (t.subtotal, t.vat_amount, t.total):
        assert Decimal(value).as_tuple().exponent == -2


def test_build_is_deterministic(ds: Dataset) -> None:
    again = build()
    assert [s.record for s in again.docs] == [s.record for s in ds.docs]
    assert again.vendors == ds.vendors
    assert again.purchase_orders == ds.purchase_orders
    assert again.receipts == ds.receipts
    assert again.emails == ds.emails
    other = build(DEFAULT_COUNT, DEFAULT_SEED + 1)
    assert [v.legal_name for v in other.vendors] != [v.legal_name for v in ds.vendors]


def test_renders_are_deterministic(ds: Dataset) -> None:
    seen: set[str] = set()
    picked = []
    for spec in ds.docs:
        if spec.record.format not in seen:
            seen.add(spec.record.format)
            picked.append(spec)
    assert seen >= {"pdf", "png", "jpg", "scan_pdf", "docx", "xml", "email_body"}
    for spec in picked:
        assert spec.render() == spec.render(), spec.record.id
    blobs = {s.record.id: s.render() for s in ds.docs if s.record.email_id == "eml-0003"}
    record = ds.emails[2]
    assert render_email(record, ds, blobs) == render_email(record, ds, blobs)


def test_counts(ds: Dataset) -> None:
    regular = list(ds.regular())
    assert len(ds.vendors) >= 30
    assert len(regular) >= 300
    assert {v.country for v in ds.vendors} >= {"AE", "GB", "DE", "SA"}
    formats = Counter(s.record.format for s in regular)
    assert formats["pdf"] > 100
    assert formats["xml"] >= 20
    assert formats["docx"] >= 5
    assert formats["png"] + formats["jpg"] + formats["scan_pdf"] >= 30
    languages = Counter(s.record.language for s in regular)
    assert languages["ar"] >= 20
    assert languages["bilingual"] >= 20
    currencies = Counter(s.record.truth.currency for s in regular)
    assert currencies.most_common(1)[0][0] == "AED"
    assert len(currencies) >= 4
    assert any(x.vat_category == "Z" for s in regular for x in s.record.truth.lines)


def test_ibans_are_valid_and_synthetic(ds: Dataset) -> None:
    codes = {(b.country, b.code) for b in BANKS}
    ibans = [v.bank.iban for v in (*ds.vendors, *ds.unknown_vendors)]
    ibans += [s.record.truth.bank.iban for s in ds.docs if s.record.truth.bank is not None]
    assert len(ibans) > 300
    for iban in ibans:
        assert iban_is_valid(iban), iban
        country = iban[:2]
        assert any(iban[4:].startswith(code) for c, code in codes if c == country), iban
    assert {i[:2] for i in ibans} >= {"AE", "GB", "DE", "FR", "NL", "SA", "BH"}
    # The validator rejects a single changed digit and a wrong length.
    good = ibans[0]
    bad = good[:-1] + str((int(good[-1]) + 1) % 10)
    assert not iban_is_valid(bad)
    assert not iban_is_valid(good + "0")
    rng = random.Random(7)  # noqa: S311 (seeded test data)
    for bank in BANKS:
        assert iban_is_valid(make_iban(bank, rng))
        assert bank.bic.startswith(("XQ", "XZ"))
        assert "synthetic" in bank.name


def test_trns_have_the_shape(ds: Dataset) -> None:
    trns = [v.trn for v in ds.vendors if v.trn is not None]
    assert len(trns) >= 26
    assert len(set(trns)) == len(trns)
    for trn in trns:
        assert trn_is_well_formed(trn)
        assert trn.startswith("19")
    for spec in ds.regular():
        t = spec.record.truth
        assert t.vendor_trn == ds.vendor(t.vendor_id or "").trn
        if t.vendor_trn:
            assert trn_is_well_formed(t.vendor_trn)
        assert trn_is_well_formed(t.buyer_trn)


def test_regular_invoices_add_up(ds: Dataset) -> None:
    for spec in ds.regular():
        t = spec.record.truth
        _consistent(t)
        vendor = ds.vendor(t.vendor_id or "")
        assert t.invoice_date <= t.due_date
        if vendor.country != "AE":
            assert all(x.vat_category == "O" for x in t.lines)
        if t.bank is not None:
            assert t.bank == vendor.bank
    numbers = Counter(
        (s.record.truth.vendor_id, s.record.truth.invoice_number) for s in ds.regular()
    )
    assert max(numbers.values()) == 1


def test_matching_cases(ds: Dataset) -> None:
    pos = {p.po_number: p for p in ds.purchase_orders}
    grns = {g.grn_number: g for g in ds.receipts}
    scenarios = Counter(c.scenario for c in ds.cases)
    for name in (
        "exact",
        "price_inside",
        "price_outside",
        "qty_inside",
        "qty_outside",
        "partial_matched",
        "partial_overbilled",
        "multi_invoice",
        "missing_receipt",
        "no_po",
        "unknown_po",
    ):
        assert scenarios[name] >= 2, name
    for po in ds.purchase_orders:
        assert Decimal(po.subtotal) == _sum(x.line_total for x in po.lines)
        for line in po.lines:
            assert Decimal(line.line_total) == (
                Decimal(line.quantity) * Decimal(line.unit_price)
            ).quantize(CENT, ROUND_HALF_UP)
    for case in ds.cases:
        invoices = [ds.doc(i).record.truth for i in case.invoice_ids]
        assert (case.expected == "match") == (not case.expected_exceptions)
        if case.scenario in ("no_po", "unknown_po"):
            assert case.po_number not in pos
            assert case.expected_exceptions == ("no_po",)
            continue
        po = pos[case.po_number or ""]
        assert all(t.po_number == po.po_number for t in invoices)
        assert all(grns[g].po_number == po.po_number for g in case.grn_numbers)
        if po.match_type == "3-way" and case.scenario != "missing_receipt":
            assert case.grn_numbers
        if case.scenario == "missing_receipt":
            assert not case.grn_numbers
            assert case.expected_exceptions == ("missing_receipt",)
        if case.scenario in ("price_inside", "price_outside"):
            inv = invoices[0]
            ratios = [
                (Decimal(i.unit_price) - Decimal(p.unit_price)) / Decimal(p.unit_price) * 100
                for i, p in zip(inv.lines, po.lines, strict=True)
            ]
            worst = max(ratios)
            if case.scenario == "price_inside":
                assert Decimal("1.5") < worst <= Decimal("2.0")
            else:
                assert Decimal("2.0") < worst < Decimal("2.6")
        if case.scenario in ("qty_inside", "qty_outside"):
            received = {
                ln.po_line_no: Decimal(ln.quantity_received)
                for g in case.grn_numbers
                for ln in grns[g].lines
            }
            ratios = [
                (Decimal(i.quantity) - received[i.line_no]) / received[i.line_no] * 100
                for i in invoices[0].lines
            ]
            worst = max(ratios)
            assert (worst <= 5) == (case.scenario == "qty_inside")
            assert worst in (Decimal(4), Decimal(6))
        if case.scenario == "multi_invoice":
            assert len(invoices) == 2
            billed = _sum(t.subtotal for t in invoices)
            assert billed == Decimal(po.subtotal)
        if case.scenario == "partial_matched":
            received_total = sum(
                Decimal(ln.quantity_received) for g in case.grn_numbers for ln in grns[g].lines
            )
            ordered_total = sum(Decimal(x.quantity) for x in po.lines)
            assert received_total < ordered_total


def test_attacks(ds: Dataset) -> None:
    attacks = [s.record for s in ds.docs if s.record.kind == "attack"]
    assert len(attacks) >= 60
    types = Counter(t for r in attacks if r.attack for t in r.attack.types)
    for name in (
        "exact_duplicate",
        "near_duplicate",
        "transposed_digits",
        "prefix_suffix",
        "date_shifted",
        "same_amount_within_days",
        "same_line_items",
        "bank_change",
        "lookalike_domain",
        "first_time_sender",
        "urgency",
        "split_invoice",
        "inflated_amount",
        "round_amount",
        "vat_wrong_rate",
        "line_total_error",
        "prompt_injection",
        "fake_invoice",
        "template_mismatch",
        "metadata_inconsistency",
        "editing_traces",
    ):
        assert types[name] >= 1, name
    vendors = {v.id: v for v in ds.vendors}
    unknown = {v.id for v in ds.unknown_vendors}
    for r in attacks:
        info = r.attack
        assert info is not None
        assert info.expected_findings
        assert info.description
        if info.original_id:
            original = ds.doc(info.original_id).record
            assert original.kind == "invoice"
            assert original.truth.vendor_id == r.truth.vendor_id
        if "bank_mismatch" in info.expected_findings:
            assert r.truth.bank is not None
            assert r.truth.bank.iban != vendors[r.truth.vendor_id or ""].bank.iban
            assert iban_is_valid(r.truth.bank.iban)
        if "unknown_vendor" in info.expected_findings:
            assert r.truth.vendor_id in unknown
        if "split_under_threshold" in info.expected_findings:
            assert Decimal(r.truth.total) < APPROVAL_THRESHOLD
            assert Decimal(r.truth.total) > APPROVAL_THRESHOLD * Decimal("0.95")
            assert len(info.related_ids) == 2
        if "prompt_injection" in info.expected_findings:
            assert info.payloads
        clean = not {"vat_error", "arithmetic_error"} & set(info.expected_findings)
        if clean:
            _consistent(r.truth)
        else:
            assert info.correct_values
    transposed = [r for r in attacks if r.attack and "transposed_digits" in r.attack.types]
    for r in transposed:
        assert r.attack is not None
        assert r.attack.original_id is not None
        source = ds.doc(r.attack.original_id).record.truth
        assert transpose(source.invoice_number) == r.truth.invoice_number
        assert source.total == r.truth.total


def test_exact_duplicates_share_the_file_hash(ds: Dataset) -> None:
    for spec in ds.docs:
        info = spec.record.attack
        if info is not None and "exact_duplicate" in info.types:
            original = ds.doc(info.original_id or "")
            digest = hashlib.sha256(spec.render()).hexdigest()
            assert digest == hashlib.sha256(original.render()).hexdigest()


def test_lookalike_domains() -> None:
    assert lookalike("ziskar.example", "homoglyph") == "z1skar.example"
    assert lookalike("ziskar.example", "extra_letter") == "ziskkar.example"
    assert lookalike("ziskar.example", "tld") == "ziskar.test"
    idn = lookalike("ziskar.example", "idn")
    assert idn.startswith("xn--")
    assert idn.endswith(".example")
    assert transpose("INV-2026-00153") == "INV-2026-00135"
    assert transpose("1111") is None


def test_demo_trio(ds: Dataset) -> None:
    demo = {s.record.demo: s for s in ds.docs if s.record.demo}
    assert set(demo) == {"clean", "near_duplicate", "bank_change"}
    clean = demo["clean"].record
    assert clean.kind == "invoice"
    assert clean.format == "pdf"
    assert clean.attack is None
    case = next(c for c in ds.cases if clean.id in c.invoice_ids)
    assert case.scenario == "exact"
    assert case.expected == "match"
    assert case.grn_numbers
    near = demo["near_duplicate"].record
    assert near.attack is not None
    assert "transposed_digits" in near.attack.types
    original = ds.doc(near.attack.original_id or "").record
    assert transpose(original.truth.invoice_number) == near.truth.invoice_number
    bank = demo["bank_change"].record
    assert bank.attack is not None
    assert {
        "bank_mismatch",
        "sender_lookalike_domain",
        "prompt_injection",
        "hidden_text",
    } <= set(bank.attack.expected_findings)
    vendor = ds.vendor(bank.truth.vendor_id or "")
    assert bank.attack.notes["sender"].split("@")[1] not in vendor.email_domains
    assert any("approve immediately" in p for p in bank.attack.payloads)
    vendors = {s.record.truth.vendor_id for s in demo.values()}
    assert len(vendors) == 3
    # The payload is in the PDF text layer, but not in the ground truth.
    text = PdfReader(io.BytesIO(demo["bank_change"].render())).pages[0].extract_text()
    assert "approve immediately" in text
    assert "approve" not in json.dumps(bank.truth.model_dump())
    emails = [e for e in ds.emails if e.demo]
    assert [e.demo for e in emails] == ["clean", "near_duplicate", "bank_change"]
    assert all(e.kind == "demo" and e.original_sender for e in emails)
    assert "sender_lookalike_domain" in emails[2].expected_findings


def test_emails(ds: Dataset) -> None:
    kinds = Counter(e.kind for e in ds.emails)
    for kind in ("single", "multi_attachment", "with_extra", "body", "forwarded", "marketing"):
        assert kinds[kind] >= 3, kind
    emailed = {i for e in ds.emails for i in e.invoice_ids}
    for spec in ds.docs:
        assert (spec.record.channel == "email") == (spec.record.id in emailed), spec.record.id
        if spec.record.email_id:
            assert spec.record.id in ds.emails[int(spec.record.email_id[4:]) - 1].invoice_ids
    for e in ds.emails:
        domain = e.sender.rsplit("@", 1)[1]
        assert domain.endswith((".example", ".test")), e.sender
        assert e.ignore == (e.kind == "marketing")
        for att in e.attachments:
            assert not att.filename.startswith(("atk", "inv")), att.filename
    # A multi-attachment email parses back into its invoices.
    multi = next(e for e in ds.emails if e.kind == "multi_attachment")
    blobs = {i: ds.doc(i).render() for i in multi.invoice_ids}
    msg = email.message_from_bytes(render_email(multi, ds, blobs), policy=policy.default)
    names = [p.get_filename() for p in msg.iter_attachments()]  # type: ignore[attr-defined]
    assert names == [a.filename for a in multi.attachments]
    body = next(e for e in ds.emails if e.kind == "body")
    msg = email.message_from_bytes(render_email(body, ds, {}), policy=policy.default)
    text = msg.get_body(("plain",)).get_content()  # type: ignore[union-attr]
    truth = ds.doc(body.body_invoice_id or "").record.truth
    assert truth.invoice_number in text


def test_xml_and_docx_carry_the_truth(ds: Dataset) -> None:
    xml = next(s for s in ds.regular() if s.record.format == "xml")
    root = ET.fromstring(xml.render())  # noqa: S314 (our own generated XML)
    ns = {
        "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
        "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    }
    t = xml.record.truth
    assert root.findtext("cbc:CustomizationID", namespaces=ns) == "urn:peppol:pint:billing-1@ae-1"
    assert root.findtext("cbc:ID", namespaces=ns) == t.invoice_number
    assert root.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=ns) == t.total
    assert len(root.findall("cac:InvoiceLine", ns)) == len(t.lines)
    docx = next(s for s in ds.regular() if s.record.format == "docx")
    with zipfile.ZipFile(io.BytesIO(docx.render())) as z:
        document = z.read("word/document.xml").decode("utf-8")
    assert docx.record.truth.invoice_number in document


def test_no_em_dashes_or_real_domains(ds: Dataset) -> None:
    for path, text in manifests(DEFAULT_COUNT, DEFAULT_SEED).items():
        assert EM_DASH not in text, path
    for vendor in ds.vendors:
        assert all(d.endswith(".example") for d in vendor.email_domains)
        assert all(c.email.endswith(".example") for c in vendor.contacts)


def test_committed_manifests_match(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(["--check"]) == 0
    assert "match" in capsys.readouterr().out
    total = sum(p.stat().st_size for p in MANIFEST_DIR.iterdir())
    assert total < 5 * 1024 * 1024


def test_generate_writes_files(tmp_path: Path) -> None:
    out = tmp_path / "pay"
    assert cli(["generate", "--out", str(out), "--count", "120", "--seed", "11"]) == 0
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    for doc in index["documents"]:
        assert (out / doc["file"]).is_file(), doc["file"]
    for record in index["emails"]:
        assert (out / record["file"]).is_file()
    assert index["stats"]["invoices"] == 120
