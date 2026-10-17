# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Recorded model responses for the Pay invoice extraction: an eval subset and the
demo trio (a recordings bundle). **The answers are scripted, not model output.**

The model router keys a recording on the exact request (``pairly_core.llm.hashing``):
task, prompt version and hash, provider, model, inputs and request settings. So this
builds, for each chosen invoice, the request the ``pay_invoice`` graph sends:

1. render the invoice (``synth.pay``) and parse it with the engine's parser for its
   type (the sandbox runs the same parsers): PDF, DOCX, or the email body as the engine
   stores it (``parse_email`` then the text parser);
2. scan it with the guardrails and build the ``GuardedInput`` (the sanitised text in its
   nonce-delimited data block, exactly what the model is shown);
3. ``inputs = {"schema": <PayInvoice description>, "document": <data block>}`` (no
   hints: a fresh organisation has no corrections yet);

and stores, as the response for the router's first choice (provider ``anthropic``, the
prompt's tier), answers **scripted from the synthetic ground truth**: each value with a
quote taken from a line of the text the model saw that backs it. A value no line backs
(for example an Arabic name in a PDF whose text layer is in visual order) is left out,
as an honest model would have to. Scans are not in the subset: OCR output depends on the
Tesseract build, so their requests would not match on another machine.

``accuracy`` replays the scripted answers through the engine's checks
(``extract_from_answers``, the same checks a recorded answer gets) and compares what
passes with the ground truth. It measures the evidence pipeline on scripted answers,
not a model. If a prompt, the schema, the catalog or a parser changes, the hashes
change: run ``uv run python -m synth.pay recordings`` again.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from pairly_core.documents.docx import DocxParser
from pairly_core.documents.model import NormalizedDocument
from pairly_core.documents.pdf import PdfParser
from pairly_core.documents.registry import ParseOptions
from pairly_core.documents.text import TextParser
from pairly_core.extraction.engine import (
    EXTRACT_PROMPT_VERSION,
    EXTRACT_TASK,
    ExtractionOutput,
    FieldAnswer,
    extract_from_answers,
)
from pairly_core.extraction.evidence import EvidenceLocator, normalize
from pairly_core.extraction.guarded import GuardedInput
from pairly_core.extraction.schema import compile_schema
from pairly_core.extraction.validators import (
    InvalidValue,
    find_dates,
    find_numbers,
    norm_text,
    parse_value,
    supported_by_quote,
)
from pairly_core.guardrails import ScanContext, scan_document
from pairly_core.ingestion.email import parse_email
from pairly_core.llm.bundle import BUNDLE_FORMAT, BUNDLE_VERSION, dumps_bundle, parse_bundle
from pairly_core.llm.catalog import load_catalog, load_prices
from pairly_core.llm.hashing import json_round_trip, request_hash, schema_hash
from pairly_core.llm.prompt_registry import PromptRegistry, core_prompt_root
from pairly_core.llm.router import USER_TEMPLATE_VERSION
from pairly_pay.invoice_schema import PayInvoice
from synth.pay.dataset import Dataset, DocSpec, build
from synth.pay.emails import render_email
from synth.pay.models import InvoiceTruth

PROVIDER: Final = "anthropic"
CONFIDENCE: Final = 0.97
# Per format and layout, how many invoices the eval subset takes (lowest ids first).
PER_LAYOUT: Final = {"pdf": 3, "docx": 2, "email_body": 3}
DEMO_KINDS: Final = ("clean", "near_duplicate", "bank_change")
PARSE: Final = ParseOptions(ocr_languages="eng")
LABELS: Final = {
    "subtotal": ("subtotal", "sub-total", "sub total"),
    "vat_amount": ("vat",),
    "total": ("total", "amount due", "balance due"),
    "invoice_number": ("invoice", "no", "number"),
    "invoice_date": ("invoice date", "date"),
    "due_date": ("due",),
    "po_number": ("po", "purchase order", "order"),
    "vendor_trn": ("trn", "tax"),
    "currency": ("total", "currency"),
    "payment_terms": ("terms",),
}


class RecordingError(RuntimeError):
    """A scripted answer does not fit its invoice."""


@dataclass
class Item:
    """One recorded invoice: what the model saw and the scripted answers."""

    spec: DocSpec
    filename: str
    data: bytes
    media_type: str
    doc: NormalizedDocument
    guarded: GuardedInput
    answers: list[FieldAnswer] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.spec.record.id


# ------------------------------------------------------------------ choosing


def demo_specs(ds: Dataset) -> list[DocSpec]:
    by_kind = {s.record.demo: s for s in ds.docs if s.record.demo}
    return [by_kind[k] for k in DEMO_KINDS]


def subset_specs(ds: Dataset) -> list[DocSpec]:
    """The eval subset: per format and layout, the first invoices by id; then the demo
    trio. Regular invoices only (attacks belong to the checks of WP4.6)."""
    chosen: list[DocSpec] = []
    taken: dict[tuple[str, str], int] = {}
    for spec in sorted(ds.regular(), key=lambda s: s.record.id):
        fmt, layout = spec.record.format, spec.record.layout
        if fmt not in PER_LAYOUT:
            continue
        if taken.get((fmt, layout), 0) >= PER_LAYOUT[fmt]:
            continue
        taken[(fmt, layout)] = taken.get((fmt, layout), 0) + 1
        chosen.append(spec)
    ids = {s.record.id for s in chosen}
    chosen += [s for s in demo_specs(ds) if s.record.id not in ids]
    return chosen


# ------------------------------------------------------------------ parsing


def _email_body(spec: DocSpec, ds: Dataset) -> bytes:
    """The body the engine stores for an invoice written in an email (``ingest_email``)."""
    email = next(e for e in ds.emails if e.body_invoice_id == spec.record.id)
    blobs = {i: ds.doc(i).render() for i in email.invoice_ids}
    raw = render_email(email, ds, blobs)
    return parse_email(raw).text_body.strip().encode("utf-8")


def materialise(spec: DocSpec, ds: Dataset) -> Item:
    fmt = spec.record.format
    if fmt == "email_body":
        data, name, media, parser = _email_body(spec, ds), "email-body.txt", "text/plain", "text"
    elif fmt == "docx":
        name = Path(spec.record.file).name
        data, media, parser = (
            spec.render(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
        )
    elif fmt == "pdf":
        data, name, media, parser = (
            spec.render(),
            Path(spec.record.file).name,
            "application/pdf",
            "pdf",
        )
    else:
        raise RecordingError(f"{spec.record.id}: format {fmt} is not recorded")
    with tempfile.TemporaryDirectory(prefix="pairly-synth-pay-") as tmp:
        path = Path(tmp) / name
        path.write_bytes(data)
        if parser == "pdf":
            doc = PdfParser().parse(path, PARSE)
        elif parser == "docx":
            doc = DocxParser().parse(path, PARSE)
        else:
            doc = TextParser().parse(path, PARSE)
    report = scan_document(doc, context=ScanContext(filename=name))
    return Item(spec, name, data, media, doc, GuardedInput.build(doc, report))


# ------------------------------------------------------------------ scripting answers


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.split("\n") if line.strip()]


def _has_label(line: str, labels: Iterable[str]) -> bool:
    folded = normalize(line).text
    return any(label in folded for label in labels)


class _Script:
    def __init__(self, item: Item) -> None:
        self.item = item
        self.lines = _lines(item.guarded.text)
        self.locator = EvidenceLocator(item.doc, item.guarded)
        self.schema = compile_schema(PayInvoice)
        self.answers: list[FieldAnswer] = []

    def _backs(self, path: str, value: str, quote: str) -> bool:
        leaf = self.schema.leaf_for(path)
        if leaf is None:
            raise RecordingError(f"unknown path {path}")
        try:
            typed = parse_value(leaf.value, value)
        except InvalidValue:
            return False
        if len(quote) > 240 or self.locator.locate(quote) is None:
            return False
        return supported_by_quote(leaf.value, typed, quote)

    def answer(
        self,
        path: str,
        value: str | None,
        candidates: Iterable[str],
        *,
        labels: Iterable[str] = (),
        last: bool = False,
    ) -> str | None:
        if value is None or value == "":
            return None
        options = [line for line in candidates if self._backs(path, value, line)]
        labelled = [line for line in options if _has_label(line, labels)]
        pool = labelled or options
        if not pool:
            return None
        quote = pool[-1] if last else pool[0]
        self.answers.append(FieldAnswer(path=path, value=value, quote=quote, confidence=CONFIDENCE))
        return quote

    # what kind of line can back a value
    def with_number(self, value: str) -> list[str]:
        target = Decimal(value).normalize()
        return [ln for ln in self.lines if target in {n.normalize() for n in find_numbers(ln)}]

    def with_text(self, value: str) -> list[str]:
        target = norm_text(value)
        return [ln for ln in self.lines if target and target in norm_text(ln)]

    def with_date(self, value: str) -> list[str]:
        from datetime import date

        target = date.fromisoformat(value)
        return [ln for ln in self.lines if target in find_dates(ln, "DMY")]

    def row_windows(self, start: int, most: int = 12) -> list[str]:
        """The description's line, then that line with the next ones, shortest first."""
        out = []
        for size in range(1, most + 1):
            window = " ".join(self.lines[start : start + size])
            if len(window) > 240 or start + size > len(self.lines):
                break
            out.append(window)
        return out

    def with_compact(self, value: str) -> list[str]:
        return [ln for ln in self.lines if value in "".join(ln.split()).upper()]


def script_answers(item: Item) -> list[FieldAnswer]:
    """Answers a careful reader gives, from the ground truth, each with a backing line."""
    truth: InvoiceTruth = item.spec.record.truth
    s = _Script(item)
    s.answer("vendor_name", truth.vendor_name, s.with_text(truth.vendor_name))
    if truth.vendor_trn:
        s.answer(
            "vendor_trn",
            truth.vendor_trn,
            s.with_compact(truth.vendor_trn),
            labels=LABELS["vendor_trn"],
        )
    s.answer(
        "invoice_number",
        truth.invoice_number,
        s.with_text(truth.invoice_number),
        labels=LABELS["invoice_number"],
    )
    s.answer(
        "invoice_date",
        truth.invoice_date,
        s.with_date(truth.invoice_date),
        labels=("invoice date", "date"),
    )
    s.answer("due_date", truth.due_date, s.with_date(truth.due_date), labels=LABELS["due_date"])
    s.answer(
        "payment_terms",
        truth.payment_terms,
        s.with_text(truth.payment_terms),
        labels=LABELS["payment_terms"],
    )
    s.answer(
        "currency",
        truth.currency,
        s.with_text(truth.currency),
        labels=LABELS["currency"],
        last=True,
    )
    if truth.po_number:
        s.answer(
            "po_number", truth.po_number, s.with_text(truth.po_number), labels=LABELS["po_number"]
        )
    s.answer(
        "subtotal",
        truth.subtotal,
        s.with_number(truth.subtotal),
        labels=LABELS["subtotal"],
        last=True,
    )
    s.answer(
        "vat_amount",
        truth.vat_amount,
        s.with_number(truth.vat_amount),
        labels=LABELS["vat_amount"],
        last=True,
    )
    s.answer("vat_rate", truth.vat_rate, s.with_number(truth.vat_rate), labels=("vat",))
    s.answer("total", truth.total, s.with_number(truth.total), labels=LABELS["total"], last=True)
    if truth.bank is not None:
        bank = truth.bank
        s.answer("bank.iban", bank.iban, s.with_compact(bank.iban))
        s.answer("bank.account_name", bank.account_name, s.with_text(bank.account_name))
        s.answer("bank.bank_name", bank.bank_name, s.with_text(bank.bank_name))
        s.answer("bank.swift", bank.bic, s.with_compact(bank.bic))
    for index, line in enumerate(truth.lines):
        prefix = f"line_items[{index}]."
        described = s.with_text(line.description)
        quote = s.answer(prefix + "description", line.description, described)
        start = s.lines.index(quote) if quote is not None else None
        for name, value in (
            ("quantity", line.quantity),
            ("unit_price", line.unit_price),
            ("vat_rate", line.vat_rate),
            ("vat_amount", line.vat_amount),
            ("line_total", line.line_total),
        ):
            # Table cells are often one line each: the row is the description's line
            # and the lines after it, as far as needed to reach the value.
            windows = s.row_windows(start) if start is not None else []
            s.answer(prefix + name, value, windows + s.with_number(value))
    return s.answers


# ------------------------------------------------------------------ the bundle


def extract_inputs(guarded: GuardedInput) -> dict[str, Any]:
    """The router inputs ``extract`` sends for a Pay invoice (no hints)."""
    return {"schema": compile_schema(PayInvoice).describe(), "document": guarded.data_block}


def entry_for(item: Item) -> dict[str, Any]:
    prompt = PromptRegistry([core_prompt_root()]).get(EXTRACT_TASK, EXTRACT_PROMPT_VERSION)
    model = load_catalog(load_prices()).model_for(PROVIDER, prompt.tier)
    if model is None:
        raise RecordingError(f"no {PROVIDER} model for tier {prompt.tier}")
    # Mirrors ModelRouter's request settings: part of the request hash.
    settings = {
        "temperature": prompt.temperature if model.supports_temperature else None,
        "max_output_tokens": prompt.max_output_tokens,
        "effort": model.effort,
        "user_template": USER_TEMPLATE_VERSION,
    }
    inputs = json_round_trip(extract_inputs(item.guarded))
    s_hash = schema_hash(ExtractionOutput)
    response = {
        "fields": [a.model_dump(mode="json") for a in item.answers],
        "confidence": CONFIDENCE,
    }
    ExtractionOutput.model_validate(response)
    return {
        "request_hash": request_hash(
            task=prompt.task,
            prompt_version=prompt.version,
            prompt_hash=prompt.hash,
            provider=PROVIDER,
            model=model.model,
            inputs=inputs,
            schema_hash=s_hash,
            settings=settings,
        ),
        "task": prompt.task,
        "prompt_version": prompt.version,
        "prompt_hash": prompt.hash,
        "provider": PROVIDER,
        "model": model.model,
        "tier": prompt.tier,
        "schema_name": ExtractionOutput.__name__,
        "schema_hash": s_hash,
        "request": {"inputs": inputs, "settings": settings},
        "response": json_round_trip(response),
        "output_complete": True,
        "stop_reason": "end_turn",
        "status": "ok",
    }


def items(ds: Dataset | None = None) -> Iterator[Item]:
    ds = ds or build()
    for spec in subset_specs(ds):
        item = materialise(spec, ds)
        item.answers = script_answers(item)
        yield item


def build_bundle(recorded: Iterable[Item] | None = None) -> str:
    entries = sorted((entry_for(i) for i in (recorded or items())), key=lambda e: e["request_hash"])
    text = dumps_bundle({"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "entries": entries})
    parse_bundle(text)  # fails closed on any entry whose hash does not match
    return text


# ------------------------------------------------------------------ accuracy


@dataclass
class Accuracy:
    invoices: int = 0
    header_total: int = 0
    header_correct: int = 0
    line_total: int = 0
    line_correct: int = 0
    totals_correct: int = 0
    vat_correct: int = 0
    ready: int = 0
    by_field: dict[str, list[int]] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        def pct(a: int, b: int) -> float:
            return round(100 * a / b, 1) if b else 0.0

        return {
            "scripted": True,
            "invoices": self.invoices,
            "header_field_accuracy": pct(self.header_correct, self.header_total),
            "line_item_field_accuracy": pct(self.line_correct, self.line_total),
            "total_accuracy": pct(self.totals_correct, self.invoices),
            "vat_accuracy": pct(self.vat_correct, self.invoices),
            "extraction_ok": self.ready,
            "by_field": {k: pct(v[0], v[1]) for k, v in sorted(self.by_field.items())},
        }


def _expected(truth: InvoiceTruth) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from datetime import date

    header: dict[str, Any] = {
        "vendor_name": truth.vendor_name,
        "vendor_trn": truth.vendor_trn,
        "invoice_number": truth.invoice_number,
        "invoice_date": date.fromisoformat(truth.invoice_date),
        "due_date": date.fromisoformat(truth.due_date),
        "currency": truth.currency,
        "po_number": truth.po_number,
        "subtotal": Decimal(truth.subtotal),
        "vat_amount": Decimal(truth.vat_amount),
        "vat_rate": Decimal(truth.vat_rate),
        "total": Decimal(truth.total),
    }
    lines = [
        {
            "description": line.description,
            "quantity": Decimal(line.quantity),
            "unit_price": Decimal(line.unit_price),
            "vat_rate": Decimal(line.vat_rate),
            "vat_amount": Decimal(line.vat_amount),
            "line_total": Decimal(line.line_total),
        }
        for line in truth.lines
    ]
    return {k: v for k, v in header.items() if v is not None}, lines


def accuracy(recorded: Iterable[Item]) -> Accuracy:
    """Replay the scripted answers through the engine's checks and score them."""
    acc = Accuracy()
    for item in recorded:
        result = extract_from_answers(
            item.doc, item.guarded, PayInvoice, item.answers, source="eval", source_version="1"
        )
        acc.invoices += 1
        acc.ready += int(not result.needs_human)
        got = {f.path: f.value for f in result.fields if not f.flags}
        header, lines = _expected(item.spec.record.truth)
        for name, want in header.items():
            ok = got.get(name) == want
            acc.header_total += 1
            acc.header_correct += ok
            stats = acc.by_field.setdefault(name, [0, 0])
            stats[0] += ok
            stats[1] += 1
        acc.totals_correct += got.get("total") == header["total"]
        acc.vat_correct += got.get("vat_amount") == header["vat_amount"]
        for index, line in enumerate(lines):
            for name, want in line.items():
                ok = got.get(f"line_items[{index}].{name}") == want
                acc.line_total += 1
                acc.line_correct += ok
    return acc
