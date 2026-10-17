# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pay intake and extraction without a database (WP4.4, WP4.5).

* email rules on every synthetic email (marketing ignored, attachments, body invoices,
  forwarded chains) and the attachment rules;
* e-invoices: exact values, Decimal money, line items, no model;
* the recorded (scripted) extraction bundle: up to date, every request the engine
  sends for the eval subset finds its recording, quotes are located, money is Decimal,
  line items are read, and the scripted accuracy holds;
* few-shot hints: a correction changes the prompt inputs; bank details never become
  hints.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel

from pairly_core.documents.registry import ParseOptions, default_registry
from pairly_core.extraction.engine import ExtractionOutput, FewShotHint
from pairly_core.extraction.guarded import GuardedInput
from pairly_core.extraction.types import FieldFlag
from pairly_core.guardrails import ScanContext, scan_document
from pairly_core.ingestion.email import parse_email
from pairly_core.llm.bundle import parse_bundle
from pairly_core.llm.errors import RecordingMissing
from pairly_core.llm.router import ModelRouter
from pairly_core.llm.types import RouterResult, TaskSpec
from pairly_pay.einvoice import is_einvoice, read_einvoice
from pairly_pay.email_rules import classify_attachment, classify_email
from pairly_pay.invoice_graph import read_with_model
from pairly_pay.invoice_schema import PayInvoice
from pairly_pay.learning import hint_inputs, is_sensitive
from pairly_pay.matching import VendorMatch, clean_domains
from pairly_pay.recordings import recordings_text
from synth.pay.dataset import Dataset, build
from synth.pay.emails import render_email
from synth.pay.recordings import Item, accuracy, build_bundle, items

ORG = uuid.UUID("00000000-0000-4000-8000-000000000001")


@cache
def dataset() -> Dataset:
    return build()


@cache
def recorded() -> tuple[Item, ...]:
    return tuple(items(dataset()))


# ------------------------------------------------------------------ email rules


@cache
def _blob(doc_id: str) -> bytes:
    return dataset().doc(doc_id).render()


def _raw(email_id: str) -> bytes:
    ds = dataset()
    record = next(e for e in ds.emails if e.id == email_id)
    return render_email(record, ds, {i: _blob(i) for i in record.invoice_ids})


def _sample() -> list[Any]:
    """Every kind of synthetic email, a few each, without scans (slow to render)."""
    ds = dataset()
    taken: dict[str, int] = {}
    out = []
    for record in ds.emails:
        if any(ds.doc(i).record.format in {"png", "jpg", "scan_pdf"} for i in record.invoice_ids):
            continue
        if taken.get(record.kind, 0) >= 4:
            continue
        taken[record.kind] = taken.get(record.kind, 0) + 1
        out.append(record)
    assert {
        "body",
        "demo",
        "forwarded",
        "marketing",
        "multi_attachment",
        "single",
        "with_extra",
    } <= set(taken)
    return out


def test_synthetic_emails_are_classified_as_labelled() -> None:
    for record in _sample():
        verdict = classify_email(parse_email(_raw(record.id)))
        if record.ignore:
            assert verdict.ignored, (record.id, verdict)
        else:
            assert verdict.kind == "invoice", (record.id, verdict)


def test_attachment_rules() -> None:
    assert classify_attachment("Invoice_1327_26.pdf", "application/pdf", 50_000) == "invoice"
    assert classify_attachment("Terms_and_conditions.pdf", "application/pdf", 2_000) == "supporting"
    assert classify_attachment("company_logo.png", "image/png", 900) == "supporting"
    assert classify_attachment("scan.png", "image/png", 400_000) == "unclear"
    assert classify_attachment(None, "application/pdf", 10) == "unclear"


def test_unclear_email_goes_to_a_person() -> None:
    raw = (
        b"From: someone@vendor.example\r\nTo: pay@x.example\r\nSubject: Hello\r\n"
        b"Message-ID: <a@b>\r\n\r\nPlease see the details we discussed last week.\r\n"
    )
    verdict = classify_email(parse_email(raw))
    assert verdict.kind == "unsure"
    assert not verdict.ignored


def test_forwarded_chain_keeps_the_original_sender() -> None:
    record = next(e for e in dataset().emails if e.kind == "forwarded")
    parsed = parse_email(_raw(record.id))
    assert record.original_sender is not None
    assert record.original_sender in [f.sender for f in parsed.forwarded]
    assert clean_domains([record.original_sender.split("@")[1], "BAD DOMAIN", None]) == [
        record.original_sender.split("@")[1]
    ]


# ------------------------------------------------------------------ e-invoices


def _parsed_xml(spec_id: str) -> tuple[Any, GuardedInput]:
    spec = dataset().doc(spec_id)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inv.xml"
        path.write_bytes(spec.render())
        doc = default_registry().get("application/xml").parse(path, ParseOptions())
    report = scan_document(doc, context=ScanContext(filename="inv.xml"))
    return doc, GuardedInput.build(doc, report)


def test_einvoice_is_read_exactly_without_a_model() -> None:
    specs = [s for s in dataset().regular() if s.record.format == "xml"][:10]
    assert specs
    for spec in specs:
        doc, guarded = _parsed_xml(spec.record.id)
        assert is_einvoice(doc)
        result = read_einvoice(doc, guarded)
        assert not result.needs_human, (spec.record.id, result.reasons)
        assert result.model_call.model is None
        data = result.data
        assert data is not None
        truth = spec.record.truth
        assert data.invoice_number.value == truth.invoice_number
        assert data.total.value == Decimal(truth.total)
        assert isinstance(data.total.value, Decimal)
        assert data.subtotal.value == Decimal(truth.subtotal)
        assert data.vat_amount.value == Decimal(truth.vat_amount)
        assert data.vendor_trn.value == truth.vendor_trn
        assert [li.line_total.value for li in data.line_items] == [
            Decimal(x.line_total) for x in truth.lines
        ]
        assert all(f.confidence == 1.0 for f in result.fields if f.value is not None)
        # Evidence: every value points at its line of the document.
        assert all(f.spans for f in result.fields if f.value is not None)


@pytest.mark.parametrize("fmt", ["cii", "factur-x", "peppol-bis3"])
def test_other_einvoice_formats_are_read_exactly_without_a_model(fmt: str) -> None:
    from synth.pay.einvoices import render_sample, samples

    chosen = [(s, spec) for s, spec in samples(dataset()) if s.format == fmt]
    assert chosen
    for sample, spec in chosen:
        name = sample.file.rsplit("/", 1)[-1]
        media = "application/pdf" if name.endswith(".pdf") else "application/xml"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_bytes(render_sample(sample, spec.doc))
            doc = default_registry().get(media).parse(path, ParseOptions(ocr_languages="eng"))
        report = scan_document(doc, context=ScanContext(filename=name))
        guarded = GuardedInput.build(doc, report)
        assert is_einvoice(doc), sample.id
        result = read_einvoice(doc, guarded)
        assert not result.needs_human, (sample.id, result.reasons)
        assert result.model_call.model is None
        data = result.data
        assert data is not None
        truth = sample.truth
        assert data.invoice_number.value == truth.invoice_number
        assert data.total.value == Decimal(truth.total)
        assert data.vat_amount.value == Decimal(truth.vat_amount)
        assert data.vendor_trn.value == truth.vendor_trn
        assert [li.line_total.value for li in data.line_items] == [
            Decimal(x.line_total) for x in truth.lines
        ]
        assert all(f.spans for f in result.fields if f.value is not None)


def test_einvoice_with_an_injected_note_needs_a_person() -> None:
    attack = next(
        s
        for s in dataset().docs
        if s.record.format == "xml" and s.record.attack and s.record.attack.payloads
    )
    doc, guarded = _parsed_xml(attack.record.id)
    result = read_einvoice(doc, guarded)
    assert result.needs_human
    assert any(r.startswith("guardrails_flagged") for r in result.reasons)


# ------------------------------------------------------------------ recordings


class BundleRouter:
    """Answers from the recordings bundle when the engine's inputs are exactly the
    recorded inputs, like the real router does by request hash; else nothing."""

    def __init__(self) -> None:
        self.entries = parse_bundle(recordings_text())
        self.inputs: list[dict[str, Any]] = []

    async def complete[T: BaseModel](
        self,
        *,
        task: TaskSpec,
        inputs: dict[str, Any],
        schema: type[T],
        org_id: uuid.UUID,
        context: Any = None,
    ) -> RouterResult[T]:
        self.inputs.append(inputs)
        plain = json.loads(json.dumps(inputs))
        for entry in self.entries:
            if entry["request"]["inputs"] == plain:
                return RouterResult(
                    value=schema.model_validate(entry["response"]),
                    call_id=uuid.uuid4(),
                    chain_id=uuid.uuid4(),
                    task=entry["task"],
                    prompt_version=entry["prompt_version"],
                    prompt_hash=entry["prompt_hash"],
                    provider=entry["provider"],
                    model=entry["model"],
                    tier=entry["tier"],
                    request_hash=entry["request_hash"],
                    input_tokens=0,
                    output_tokens=0,
                    cost_micros=0,
                    total_cost_micros=0,
                    latency_ms=0,
                    confidence=None,
                    from_recording=True,
                )
        raise RecordingMissing("no recording for this request")


def test_recordings_bundle_is_up_to_date() -> None:
    assert build_bundle(recorded()) == recordings_text(), (
        "run: uv run python -m synth.pay recordings"
    )


def test_recordings_cover_the_subset_and_the_demo_trio() -> None:
    ids = {i.id for i in recorded()}
    demo = {s.record.id for s in dataset().docs if s.record.demo}
    assert demo <= ids
    formats = {i.spec.record.format for i in recorded()}
    assert formats == {"pdf", "docx", "email_body"}
    assert len(parse_bundle(recordings_text())) == len(recorded())


async def test_recorded_extraction_reads_quotes_decimals_and_lines() -> None:
    router = BundleRouter()
    for item in recorded():
        result = await read_with_model(
            item.doc,
            item.guarded,
            router=cast(ModelRouter, router),
            org_id=ORG,
            invoice_id=uuid.uuid4(),
            hints=(),
        )
        assert result.model_call.from_recording, item.id
        data = result.data
        assert data is not None
        truth = item.spec.record.truth
        assert data.total.value == Decimal(truth.total), item.id
        assert isinstance(data.total.value, Decimal)
        assert len(data.line_items) == len(truth.lines), item.id
        for value in result.fields:
            if value.value is None:
                continue
            assert FieldFlag.NO_EVIDENCE not in value.flags, (item.id, value.path)
            for span in value.spans:
                # The quote is the exact original text at its offsets.
                assert item.doc.text[span.start : span.end] == span.quote


async def test_guardrail_flagged_demo_invoice_needs_a_person() -> None:
    router = BundleRouter()
    (bank_change,) = [i for i in recorded() if i.spec.record.demo == "bank_change"]
    result = await read_with_model(
        bank_change.doc,
        bank_change.guarded,
        router=cast(ModelRouter, router),
        org_id=ORG,
        invoice_id=uuid.uuid4(),
        hints=(),
    )
    assert result.needs_human
    assert any(r.startswith("guardrails_flagged") for r in result.reasons)
    # The hidden instruction never reached the model.
    assert "approve immediately" not in router.inputs[-1]["document"].lower()


def test_scripted_accuracy_on_the_recorded_subset() -> None:
    report = accuracy(recorded()).as_json()
    assert report["scripted"] is True
    assert report["invoices"] >= 30
    assert report["header_field_accuracy"] >= 95.0
    assert report["line_item_field_accuracy"] >= 95.0
    assert report["total_accuracy"] == 100.0
    assert report["vat_accuracy"] == 100.0


# ------------------------------------------------------------------ learning


async def test_a_correction_changes_the_next_prompt_inputs() -> None:
    router = BundleRouter()
    item = recorded()[0]
    model = cast(ModelRouter, router)
    invoice_id = uuid.uuid4()
    await read_with_model(
        item.doc, item.guarded, router=model, org_id=ORG, invoice_id=invoice_id, hints=()
    )
    hint = FewShotHint("invoice_number", "MON/26/1329", "MON-26-1329")
    result = await read_with_model(
        item.doc, item.guarded, router=model, org_id=ORG, invoice_id=invoice_id, hints=(hint,)
    )
    before, after = router.inputs
    assert "reviewer_corrections" not in before
    assert after["reviewer_corrections"] == hint_inputs([hint])
    assert {k: v for k, v in after.items() if k != "reviewer_corrections"} == before
    # A changed request has no recording: fail closed, a person reads it.
    assert result.needs_human


def test_bank_details_are_never_hints() -> None:
    assert is_sensitive("bank.iban")
    assert is_sensitive("bank.account_name")
    assert not is_sensitive("invoice_number")


@pytest.mark.parametrize(
    ("match", "reasons"),
    [
        (VendorMatch(None, None), ["vendor_not_found"]),
        (VendorMatch(uuid.uuid4(), "trn", "active"), []),
        (VendorMatch(uuid.uuid4(), "name", "active"), ["vendor_weak_match:name"]),
        (VendorMatch(uuid.uuid4(), "trn", "pending"), ["vendor_not_active:pending"]),
        (
            VendorMatch(None, None, candidates={"trn": ["a"], "iban": ["b"]}, conflict=True),
            ["vendor_conflict:iban,trn"],
        ),
    ],
)
def test_vendor_match_reasons(match: VendorMatch, reasons: list[str]) -> None:
    assert match.reasons == reasons


def test_schema_compiles_with_line_items_and_bank() -> None:
    from pairly_core.extraction.schema import compile_schema

    compiled = compile_schema(PayInvoice)
    paths = {f["path"] for f in compiled.describe()["fields"]}
    assert {"line_items[]", "bank"} <= {p.split(".")[0] for p in paths} | paths
    assert ExtractionOutput.model_fields
