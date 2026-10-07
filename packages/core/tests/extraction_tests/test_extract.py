# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``extract`` with scripted providers: evidence, thresholds, line items, fail closed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, ClassVar

import pytest

from pairly_core.documents.model import NormalizedDocument, ParseWarning
from pairly_core.extraction.engine import ExtractionStatus, FewShotHint, extract
from pairly_core.extraction.evidence import EvidenceLocator
from pairly_core.extraction.guarded import GuardedInput, UnguardedInputError
from pairly_core.extraction.schema import SchemaError, compile_schema
from pairly_core.extraction.types import Extracted, ExtractionModel, FieldFlag, Money, Rule
from pairly_core.guardrails import Finding, FindingType, GuardrailReport, Severity, Span
from pairly_core.guardrails.report import text_digest
from pairly_core.guardrails.sanitize import ReportMismatchError
from pairly_core.llm.errors import ErrorKind, ProviderError
from pairly_core.llm.settings import ModelMode
from pairly_core.llm.types import CallContext, TaskSpec

from .conftest import (
    GOOD_FIELDS,
    HAIKU,
    IBAN,
    INVOICE_LINES,
    LUNA,
    ORG,
    SOL,
    SONNET,
    Harness,
    Invoice,
    PassThroughInput,
    answer,
    guarded,
    make_doc,
    make_harness,
    output,
)


async def run(
    harness: Harness,
    doc: NormalizedDocument,
    *,
    sanitized: Any = None,
    hints: tuple[FewShotHint, ...] = (),
) -> Any:
    return await extract(
        doc,
        sanitized or guarded(doc),
        Invoice,
        router=harness.router,
        org_id=ORG,
        context=CallContext(workflow="pay", entity_type="document", entity_id="doc_1"),
        hints=hints,
    )


def with_fields(*replacements: dict[str, Any], drop: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    by_path = {f["path"]: f for f in GOOD_FIELDS if f["path"] not in drop}
    for item in replacements:
        by_path[item["path"]] = item
    return list(by_path.values())


async def test_happy_path_values_spans_and_metadata(
    harness: Harness, invoice_doc: NormalizedDocument
) -> None:
    harness.anthropic.script(HAIKU, output(GOOD_FIELDS))
    result = await run(harness, invoice_doc)
    assert result.status is ExtractionStatus.OK, result.reasons
    assert result.reasons == ()
    data = result.data
    assert data.invoice_number.value == "INV-2026-0042"
    assert data.invoice_date.value == date(2026, 10, 7)
    assert data.vendor_trn.value == "100123456700003"
    assert data.bank_iban.value == IBAN
    assert data.currency.value == "AED"
    assert data.vat_rate.value == Decimal("5")
    # Money is exact: Decimal with its currency, never a float.
    assert data.total.value == Money(amount=Decimal("2362.50"), currency="AED")
    assert isinstance(data.total.value.amount, Decimal)
    assert data.total.confidence == pytest.approx(0.95)
    # Line items.
    assert [li.description.value for li in data.line_items] == [
        "Printer paper A4",
        "Toner cartridge",
    ]
    assert data.line_items[1].line_total.value.amount == Decimal("2000.00")
    # Evidence: exact original text, offsets into the document, page and boxes.
    (span,) = data.invoice_number.spans
    assert span.page == 1
    assert invoice_doc.text[span.start : span.end] == "Invoice No: INV-2026-0042"
    assert span.quote == "Invoice No: INV-2026-0042"
    assert len(span.bboxes) == 1
    assert span.bboxes[0].top == pytest.approx(72.0 + 2 * 14.0)
    # The IBAN quote spans the printed groups; one merged line box.
    (iban_span,) = data.bank_iban.spans
    assert invoice_doc.text[iban_span.start : iban_span.end] == "AE07 0331 2345 6789 0123 456"
    assert len(iban_span.bboxes) == 1
    # Model call metadata for the decision record.
    meta = result.model_call
    assert meta.prompt_task == "core_extract"
    assert meta.prompt_version == "v1"
    assert meta.model == HAIKU
    assert len(meta.call_ids) == 1
    assert meta.call_ids[0] == harness.store.rows[0]["id"]
    # Inputs carry the schema description and the sanitized text only.
    request = harness.store.rows[0]["request"]["inputs"]
    # A clean document: the data block holds the whole text, between nonce tags.
    assert request["document"] == guarded(invoice_doc).data_block
    assert invoice_doc.text in request["document"]
    paths = [f["path"] for f in request["schema"]["fields"]]
    assert "line_items[]" in paths
    assert result.field("line_items[1].quantity").value == Decimal("2")


async def test_quote_not_in_document_is_no_evidence_no_credit(
    harness: Harness, invoice_doc: NormalizedDocument
) -> None:
    fields = with_fields(answer("total", "AED 9999.00", "Total: AED 9,999.00", 0.99))
    harness.anthropic.script(HAIKU, output(fields))
    result = await run(harness, invoice_doc)
    total = result.field("total")
    assert total.flags == (FieldFlag.NO_EVIDENCE,)
    assert total.confidence == 0.0
    assert total.spans == ()
    assert result.status is ExtractionStatus.NEEDS_HUMAN
    assert "no_evidence:total" in result.reasons
    assert result.data.total.confidence == 0.0
    assert result.data.total.needs_correction


async def test_missing_quote_and_too_long_quote_are_no_evidence(
    harness: Harness, invoice_doc: NormalizedDocument
) -> None:
    fields = with_fields(
        answer("invoice_number", "INV-2026-0042", None, 0.99),
        answer("vendor_trn", "100123456700003", invoice_doc.text, 0.99),
    )
    harness.anthropic.script(HAIKU, output(fields))
    result = await run(harness, invoice_doc)
    assert result.field("invoice_number").flags == (FieldFlag.NO_EVIDENCE,)
    assert result.field("vendor_trn").flags == (FieldFlag.NO_EVIDENCE,)


async def test_quote_found_but_value_not_in_it_is_mismatch(
    harness: Harness, invoice_doc: NormalizedDocument
) -> None:
    fields = with_fields(answer("total", "AED 2250.00", "Total: AED 2,362.50", 0.99))
    harness.anthropic.script(HAIKU, output(fields))
    result = await run(harness, invoice_doc)
    total = result.field("total")
    assert FieldFlag.QUOTE_MISMATCH in total.flags
    assert total.confidence == 0.0
    assert total.spans  # the quote itself was found
    assert result.status is ExtractionStatus.NEEDS_HUMAN


async def test_normalised_matching_whitespace_case_and_typography(
    harness: Harness,
) -> None:
    doc = make_doc(["Invoice\u00a0No:   INV\u20132026\u20130042", *INVOICE_LINES[3:]])
    fields = with_fields(answer("invoice_number", "INV-2026-0042", "invoice no: inv-2026-0042"))
    harness.anthropic.script(HAIKU, output(fields))
    result = await run(harness, doc)
    item = result.field("invoice_number")
    assert FieldFlag.NO_EVIDENCE not in item.flags
    (span,) = item.spans
    # The span points at the exact original characters, not the normalised form.
    assert span.quote == "Invoice\u00a0No:   INV\u20132026\u20130042"


def _report_removing(doc: NormalizedDocument, start: int, end: int) -> GuardrailReport:
    return GuardrailReport(
        findings=(
            Finding(
                id="g1",
                type=FindingType.HIDDEN_TEXT,
                code="white_on_white",
                severity=Severity.HIGH,
                detector="test",
                evidence=doc.text[start:end],
                spans=(Span(start=start, end=end),),
                remove=True,
            ),
        ),
        text_sha256=text_digest(doc.text),
        text_length=len(doc.text),
    )


async def test_quote_only_in_removed_content_is_no_evidence(
    harness: Harness, invoice_doc: NormalizedDocument
) -> None:
    """Evidence must come from what the model saw, not from content guardrails removed."""
    original = invoice_doc.text
    hidden = "TRN: 100123456700003\n"
    cut = original.index(hidden)
    sanitized = GuardedInput.build(
        invoice_doc, _report_removing(invoice_doc, cut, cut + len(hidden))
    )
    assert hidden not in sanitized.text
    harness.anthropic.script(HAIKU, output(GOOD_FIELDS))
    result = await run(harness, invoice_doc, sanitized=sanitized)
    assert result.field("vendor_trn").flags == (FieldFlag.NO_EVIDENCE,)
    # Fields after the removed block map back through the guardrails' offset map.
    total = result.field("total")
    assert not total.flags
    assert original[total.spans[0].start : total.spans[0].end] == "Total: AED 2,362.50"
    # A flagged guardrail report always sends the extraction to a person.
    assert result.status is ExtractionStatus.NEEDS_HUMAN
    assert "guardrails_flagged:g1" in result.reasons
    # The model saw the sanitised text as a nonce-delimited data block, never the hidden line.
    (row,) = harness.store.rows
    assert row["request"]["inputs"]["document"] == sanitized.data_block
    assert "100123456700003" not in sanitized.data_block


def test_range_straddling_removed_content_maps_to_nothing(invoice_doc: NormalizedDocument) -> None:
    original = invoice_doc.text
    hidden = "TRN: 100123456700003\n"
    cut = original.index(hidden)
    sanitized = GuardedInput.build(
        invoice_doc, _report_removing(invoice_doc, cut, cut + len(hidden))
    )
    # Sanitised [cut - 3, cut + 3) joins text from both sides of the removed line.
    assert sanitized.to_original(cut - 3, cut + 3) is None
    assert sanitized.to_original(0, 5) == (0, 5)
    assert sanitized.to_original(0, 10_000) is None


def test_bad_offset_map_is_never_trusted(invoice_doc: NormalizedDocument) -> None:
    @dataclass(frozen=True)
    class Shifted:
        text: str

        def to_original(self, start: int, end: int) -> tuple[int, int] | None:
            return start + 3, end + 3

    locator = EvidenceLocator(invoice_doc, Shifted(invoice_doc.text))
    assert locator.locate("Total: AED 2,362.50") is None
    assert (
        EvidenceLocator(invoice_doc, PassThroughInput.of(invoice_doc)).locate("Total: AED 2,362.50")
        is not None
    )


async def test_extract_refuses_input_not_from_the_guardrails(
    harness: Harness, invoice_doc: NormalizedDocument
) -> None:
    with pytest.raises(UnguardedInputError):
        await run(harness, invoice_doc, sanitized=PassThroughInput.of(invoice_doc))
    other = make_doc(["Another document", "Total: AED 1.00"])
    with pytest.raises(UnguardedInputError):
        await run(harness, invoice_doc, sanitized=guarded(other))
    with pytest.raises(ReportMismatchError):
        GuardedInput.build(invoice_doc, guarded(other).report)
    with pytest.raises(TypeError):
        GuardedInput(  # type: ignore[call-arg]
            guarded(invoice_doc).model_input,
            guarded(invoice_doc).report,
            text_digest(invoice_doc.text),
            len(invoice_doc.text),
            object(),
        )
    assert harness.anthropic.requests == []


async def test_invalid_values_are_dropped_not_guessed(
    harness: Harness, invoice_doc: NormalizedDocument
) -> None:
    fields = with_fields(
        answer("bank_iban", "AE070331234567890123457", "AE07 0331 2345 6789 0123 456"),
        answer("invoice_date", "2026-02-30", "Invoice Date: 07/10/2026"),
        answer("currency", "GBP", "Total: AED 2,362.50"),
        answer("vendor_trn", "1001234567", "TRN: 100123456700003"),
    )
    harness.anthropic.script(HAIKU, output(fields))
    result = await run(harness, invoice_doc)
    for path in ("bank_iban", "invoice_date", "currency", "vendor_trn"):
        item = result.field(path)
        assert FieldFlag.INVALID_VALUE in item.flags, path
        assert item.value is None
        assert item.confidence == 0.0
        assert item.raw is not None
    assert "invalid_value:invoice_date" in result.reasons
    # Optional fields with a bad answer also route to a person (fail closed).
    assert "invalid_value:bank_iban" in result.reasons


async def test_thresholds_per_field_route_to_human(
    harness: Harness, invoice_doc: NormalizedDocument
) -> None:
    fields = with_fields(
        answer("invoice_number", "INV-2026-0042", "Invoice No: INV-2026-0042", 0.79),
        answer("vat_rate", "5", "VAT 5%", 0.75),
    )
    harness.anthropic.script(HAIKU, output(fields))
    result = await run(harness, invoice_doc)
    number = result.field("invoice_number")
    assert number.flags == (FieldFlag.LOW_CONFIDENCE,)
    assert number.threshold == 0.8
    assert number.confidence == pytest.approx(0.79)
    assert not result.field("vat_rate").flags  # 0.75 >= default 0.7
    assert result.status is ExtractionStatus.NEEDS_HUMAN
    assert result.reasons == ("low_confidence:invoice_number",)


async def test_missing_required_fields_and_line_items(
    harness: Harness, invoice_doc: NormalizedDocument
) -> None:
    fields = [f for f in GOOD_FIELDS if not f["path"].startswith(("line_items", "total"))]
    harness.anthropic.script(HAIKU, output(fields))
    result = await run(harness, invoice_doc)
    assert result.status is ExtractionStatus.NEEDS_HUMAN
    assert "missing:total" in result.reasons
    assert "missing:line_items" in result.reasons
    assert result.data.line_items == []


async def test_optional_absent_field_does_not_route(
    harness: Harness, invoice_doc: NormalizedDocument
) -> None:
    harness.anthropic.script(HAIKU, output(with_fields(drop=("bank_iban", "vat_rate"))))
    result = await run(harness, invoice_doc)
    assert result.status is ExtractionStatus.OK
    assert result.data.bank_iban.value is None
    assert result.field("bank_iban").flags == ()


async def test_line_items_are_compacted_and_required_subfields_checked(
    harness: Harness, invoice_doc: NormalizedDocument
) -> None:
    fields = [f for f in GOOD_FIELDS if not f["path"].startswith("line_items")]
    fields += [
        answer("line_items[3].description", "Toner cartridge", "Toner cartridge"),
        answer("line_items[3].quantity", "2", "Toner cartridge 2"),
        answer("line_items[3].line_total", "2,000.00", "1,000.00 2,000.00"),
        answer("line_items[7].description", "Printer paper A4", "Printer paper A4"),
        answer("line_items[99].description", "Out of range", "Total"),
        answer("unknown_field", "x", "Total"),
    ]
    harness.anthropic.script(HAIKU, output(fields))
    result = await run(harness, invoice_doc)
    items = result.data.line_items
    assert len(items) == 2
    # Default currency from the schema rule when the answer has none.
    assert items[0].line_total.value == Money(amount=Decimal("2000.00"), currency="AED")
    assert result.field("line_items[0].quantity").value == Decimal("2")
    assert result.field("line_items[1].description").value == "Printer paper A4"
    assert "missing:line_items[1].quantity" in result.reasons
    assert set(result.ignored_paths) == {"line_items[99].description", "unknown_field"}


async def test_duplicate_answers_keep_first_and_flag(
    harness: Harness, invoice_doc: NormalizedDocument
) -> None:
    fields = [*GOOD_FIELDS, answer("total", "AED 1.00", "Total: AED 2,362.50")]
    harness.anthropic.script(HAIKU, output(fields))
    result = await run(harness, invoice_doc)
    total = result.field("total")
    assert total.value == Money(amount=Decimal("2362.50"), currency="AED")
    assert FieldFlag.DUPLICATE_OUTPUT in total.flags
    assert total.confidence == 0.0


@pytest.mark.parametrize(
    "failure",
    [
        ProviderError(ErrorKind.SERVER_ERROR, "boom"),
        "not json at all",
        {"fields": "wrong"},
    ],
)
async def test_model_failure_fails_closed(invoice_doc: NormalizedDocument, failure: Any) -> None:
    harness = make_harness()
    for model in (HAIKU, SONNET):
        harness.anthropic.script(model, failure)
    for model in (LUNA, SOL):
        harness.openai.script(model, failure)
    result = await run(harness, invoice_doc)
    assert result.status is ExtractionStatus.NEEDS_HUMAN
    assert result.data is None
    assert result.reasons[0].startswith("model_unavailable:")
    assert all(f.value is None and f.confidence == 0.0 for f in result.fields)
    assert result.field("total").flags == (FieldFlag.MISSING,)


async def test_empty_text_needs_human_without_a_model_call(harness: Harness) -> None:
    doc = make_doc([""], warnings=[ParseWarning(code="no_text", message="no text")])
    result = await run(harness, doc)
    assert result.status is ExtractionStatus.NEEDS_HUMAN
    assert result.reasons == ("no_text",)
    assert harness.store.rows == []


async def test_document_review_warning_routes_to_human(harness: Harness) -> None:
    doc = make_doc(INVOICE_LINES, warnings=[ParseWarning(code="low_ocr_confidence", message="low")])
    harness.anthropic.script(HAIKU, output(GOOD_FIELDS))
    result = await run(harness, doc)
    assert result.status is ExtractionStatus.NEEDS_HUMAN
    assert result.reasons == ("document_needs_review:low_ocr_confidence",)


async def test_recorded_mode_replays_the_same_result(invoice_doc: NormalizedDocument) -> None:
    recording = make_harness(ModelMode.RECORD)
    recording.anthropic.script(HAIKU, output(GOOD_FIELDS))
    first = await run(recording, invoice_doc)
    replay = make_harness(ModelMode.RECORDED, store=recording.store)
    second = await run(replay, invoice_doc)
    assert second.model_call.from_recording
    assert replay.anthropic.requests == []
    assert [(f.path, f.json_value, f.flags) for f in second.fields] == [
        (f.path, f.json_value, f.flags) for f in first.fields
    ]
    # A request never recorded fails closed in recorded mode.
    other = make_doc(INVOICE_LINES[:-1])
    missing = await run(replay, other)
    assert missing.status is ExtractionStatus.NEEDS_HUMAN
    assert missing.reasons == ("model_unavailable:model_recording_missing",)


async def test_hints_are_sent_as_data(harness: Harness, invoice_doc: NormalizedDocument) -> None:
    harness.anthropic.script(HAIKU, output(GOOD_FIELDS))
    hint = FewShotHint("invoice_number", "INV-0042", "INV-2026-0042")
    await run(harness, invoice_doc, hints=(hint,))
    inputs = harness.store.rows[0]["request"]["inputs"]
    assert inputs["reviewer_corrections"] == [
        {"field": "invoice_number", "was": "INV-0042", "corrected_to": "INV-2026-0042"}
    ]


async def test_spans_across_pages(harness: Harness) -> None:
    doc = make_doc(INVOICE_LINES[:6], INVOICE_LINES[6:])
    harness.anthropic.script(HAIKU, output(GOOD_FIELDS))
    result = await run(harness, doc)
    (span,) = result.field("total").spans
    assert span.page == 2
    assert doc.text[span.start : span.end] == "Total: AED 2,362.50"


async def test_task_confidence_threshold_escalates(invoice_doc: NormalizedDocument) -> None:
    harness = make_harness()
    harness.anthropic.script(HAIKU, output(GOOD_FIELDS, confidence=0.3))
    harness.anthropic.script(SONNET, output(GOOD_FIELDS, confidence=0.95))
    task = TaskSpec(name="core_extract", prompt_version="v1", confidence_threshold=0.8)
    result = await extract(
        invoice_doc,
        guarded(invoice_doc),
        Invoice,
        task,
        router=harness.router,
        org_id=ORG,
    )
    assert result.model_call.model == SONNET
    assert len(result.model_call.call_ids) == 2


def test_schema_errors() -> None:
    class NoName(ExtractionModel):
        a: Extracted[str]

    with pytest.raises(SchemaError):
        compile_schema(NoName)

    class Floaty(ExtractionModel):
        schema_name: ClassVar[str] = "pay.floaty"
        schema_version: ClassVar[str] = "1"
        amount: Extracted[float]

    with pytest.raises(SchemaError, match="unsupported"):
        compile_schema(Floaty)

    class Plain(ExtractionModel):
        schema_name: ClassVar[str] = "pay.plain"
        schema_version: ClassVar[str] = "1"
        amount: Decimal

    with pytest.raises(SchemaError, match="Extracted"):
        compile_schema(Plain)


def test_schema_description_and_paths() -> None:
    compiled = compile_schema(Invoice)
    described = compiled.describe()
    by_path = {f["path"]: f for f in described["fields"]}
    assert by_path["currency"]["choices"] == ["AED", "USD", "EUR"]
    assert by_path["invoice_date"]["format"] == "YYYY-MM-DD"
    assert by_path["line_items[]"]["max_items"] == 50
    assert compiled.leaf_for("line_items[3].quantity") is not None
    assert compiled.leaf_for("line_items[50].quantity") is None
    assert compiled.leaf_for("line_items.quantity") is None
    assert compiled.leaf_for("total[0]") is None
    assert compiled.leaf_for("nope") is None


class Nested(ExtractionModel):
    iban: Annotated[Extracted[str], Rule(required=True)]


class WithGroup(ExtractionModel):
    schema_name: ClassVar[str] = "pay.with_group"
    schema_version: ClassVar[str] = "2"
    bank: Annotated[Nested, Rule(required=False)]
    amount: Annotated[Extracted[int], Rule(required=True, minimum=Decimal(1))]


async def test_nested_optional_group_and_integer_range(
    harness: Harness, invoice_doc: NormalizedDocument
) -> None:
    harness.anthropic.script(HAIKU, output([answer("amount", "10", "Printer paper A4 10")]))
    result = await extract(
        invoice_doc,
        guarded(invoice_doc),
        WithGroup,
        router=harness.router,
        org_id=ORG,
    )
    # bank.iban is required only within an optional group that is absent.
    assert result.status is ExtractionStatus.OK, result.reasons
    assert result.data is not None
    assert result.data.amount.value == 10
    assert result.data.bank.iban.value is None
