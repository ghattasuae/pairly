# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The recorded model responses for the expense demo receipts (a recordings bundle).

The model router keys a recording on the exact request (``pairly_core.llm.hashing``):
task, prompt version and hash, provider, model, the inputs and the request settings. So
this builds, for each synthetic receipt, the same request the expense graph sends in
``extract``:

1. parse the receipt PDF with the PDF parser (the sandbox runs the same parser),
2. scan it with the guardrails and build the ``GuardedInput`` (the sanitised text in its
   nonce-delimited data block, exactly what the model is shown),
3. ``inputs = {"schema": <compiled schema description>, "document": <data block>}``,

and stores the scripted answer of ``synth.expense.receipts`` as the response for the
router's first choice (provider ``anthropic``, the prompt's tier). Each entry is
checked by ``pairly_core.llm.bundle.parse_bundle`` (its hash is recomputed), and every
quote is checked against the text the model saw: visible quotes must be found, quotes
of hidden text must not.

If a prompt, the schema, the catalog or the parser changes, the hashes change: run
``uv run python -m synth.expense`` again (the acceptance tests fail closed otherwise,
because a recorded run with no matching recording sends the note to a person).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Final

from pairly_core.documents.model import NormalizedDocument
from pairly_core.documents.pdf import PdfParser
from pairly_core.documents.registry import ParseOptions
from pairly_core.extraction.engine import EXTRACT_PROMPT_VERSION, EXTRACT_TASK, ExtractionOutput
from pairly_core.extraction.evidence import EvidenceLocator
from pairly_core.extraction.guarded import GuardedInput
from pairly_core.extraction.schema import compile_schema
from pairly_core.guardrails import ScanContext, scan_document
from pairly_core.llm.bundle import BUNDLE_FORMAT, BUNDLE_VERSION, dumps_bundle, parse_bundle
from pairly_core.llm.catalog import load_catalog, load_prices
from pairly_core.llm.hashing import json_round_trip, request_hash, schema_hash
from pairly_core.llm.prompt_registry import PromptRegistry, core_prompt_root
from pairly_core.llm.router import USER_TEMPLATE_VERSION
from pairly_expense.schema import ExpenseReceipt
from synth.expense.receipts import RECEIPTS, Receipt, receipt_pdf

PROVIDER: Final = "anthropic"


class RecordingError(RuntimeError):
    """A scripted answer does not fit its receipt (a quote is wrong)."""


def parse_receipt(receipt: Receipt) -> NormalizedDocument:
    with tempfile.TemporaryDirectory(prefix="pairly-synth-") as tmp:
        path = Path(tmp) / receipt.filename
        path.write_bytes(receipt_pdf(receipt))
        return PdfParser().parse(path, ParseOptions(ocr_languages="eng"))


def guarded_input(receipt: Receipt) -> tuple[NormalizedDocument, GuardedInput]:
    doc = parse_receipt(receipt)
    report = scan_document(doc, context=ScanContext(filename=receipt.filename))
    return doc, GuardedInput.build(doc, report)


def extract_inputs(guarded: GuardedInput) -> dict[str, Any]:
    """The router inputs ``pairly_core.extraction.engine.extract`` sends (no hints)."""
    return {"schema": compile_schema(ExpenseReceipt).describe(), "document": guarded.data_block}


def _check_quotes(receipt: Receipt, doc: NormalizedDocument, guarded: GuardedInput) -> None:
    locator = EvidenceLocator(doc, guarded)
    for answer in receipt.answers:
        found = locator.locate(answer.quote) is not None
        if answer.quote in receipt.unlocatable:
            if found:
                raise RecordingError(f"{receipt.key}: hidden quote {answer.path} was found")
        elif not found:
            raise RecordingError(f"{receipt.key}: quote of {answer.path} is not in the text")


def entry_for(receipt: Receipt) -> dict[str, Any]:
    doc, guarded = guarded_input(receipt)
    _check_quotes(receipt, doc, guarded)
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
    inputs = json_round_trip(extract_inputs(guarded))
    s_hash = schema_hash(ExtractionOutput)
    response = receipt.model_output()
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


def build_bundle() -> str:
    entries = sorted((entry_for(r) for r in RECEIPTS), key=lambda e: e["request_hash"])
    text = dumps_bundle({"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "entries": entries})
    parse_bundle(text)  # fails closed on any entry whose hash does not match
    return text
