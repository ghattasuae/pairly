# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Structured e-invoices (UBL 2.1, Peppol BIS 3 and PINT AE; UN/CEFACT CII, also inside a
Factur-X PDF) to Pay's invoice fields, no model.

The engine's sandboxed e-invoice parsers (``pairly_core.documents.ubl``, ``.cii``,
``.facturx``) read every value exactly as the XML states it and keep it with its line of
text. This module maps those values onto ``PayInvoice`` paths as answers with that line
as the quote, and the engine (``extract_from_answers``) checks them exactly like a
model's answers: the quote must be in the sanitized text, the value must parse and be
backed by its quote. Confidence is 1: nothing was inferred. A value the XML does not
carry is simply absent (for example the VAT amount of a line), never computed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from pairly_core.documents.model import NormalizedDocument
from pairly_core.documents.ubl import DATA_KEY
from pairly_core.extraction.engine import ExtractionResult, FieldAnswer, extract_from_answers
from pairly_core.extraction.guarded import GuardedInput
from pairly_pay.invoice_schema import PayInvoice

SOURCE: Final = "pay_einvoice"
SOURCE_VERSION: Final = "v1"
FORMAT_KEY: Final = "einvoice:format"
SUPPORTED_FORMATS: Final = frozenset({"ubl-2.1-invoice", "un-cefact-cii"})

# e-invoice header key -> PayInvoice path.
HEADER: Final = {
    "supplier_name": "vendor_name",
    "supplier_trn": "vendor_trn",
    "invoice_number": "invoice_number",
    "invoice_date": "invoice_date",
    "due_date": "due_date",
    "payment_terms": "payment_terms",
    "currency": "currency",
    "po_number": "po_number",
    "subtotal": "subtotal",
    "vat_rate": "vat_rate",
    "vat_amount": "vat_amount",
    "total": "total",
}
BANK: Final = {"iban": "bank.iban", "account_name": "bank.account_name", "swift": "bank.swift"}
LINE: Final = {
    "description": "description",
    "quantity": "quantity",
    "unit_price": "unit_price",
    "vat_rate": "vat_rate",
    "line_total": "line_total",
}


def is_einvoice(doc: NormalizedDocument) -> bool:
    return doc.metadata.get(FORMAT_KEY) in SUPPORTED_FORMATS and DATA_KEY in doc.metadata


def _answer(path: str, item: Any) -> FieldAnswer | None:
    if not isinstance(item, Mapping):
        return None
    value, quote = item.get("value"), item.get("quote")
    if not isinstance(value, str) or not isinstance(quote, str):
        return None
    return FieldAnswer(path=path, value=value[:2000], quote=quote[:2000], confidence=1.0)


def einvoice_answers(doc: NormalizedDocument) -> list[FieldAnswer]:
    """The answers an e-invoice states. Raises ``ValueError`` when its data is unreadable."""
    data = json.loads(doc.metadata[DATA_KEY])
    if not isinstance(data, dict):
        raise ValueError("e-invoice data is not an object")
    answers: list[FieldAnswer] = []
    header = data.get("header") or {}
    bank = data.get("bank") or {}
    lines = data.get("lines") or []
    if not isinstance(header, dict) or not isinstance(bank, dict) or not isinstance(lines, list):
        raise ValueError("e-invoice data has the wrong shape")
    for key, path in HEADER.items():
        found = _answer(path, header.get(key))
        if found is not None:
            answers.append(found)
    for key, path in BANK.items():
        found = _answer(path, bank.get(key))
        if found is not None:
            answers.append(found)
    for index, line in enumerate(lines[:100]):
        if not isinstance(line, dict):
            continue
        for key, name in LINE.items():
            found = _answer(f"line_items[{index}].{name}", line.get(key))
            if found is not None:
                answers.append(found)
    return answers


def read_einvoice(doc: NormalizedDocument, guarded: GuardedInput) -> ExtractionResult[PayInvoice]:
    """The extraction result of a structured e-invoice (checked like any extraction)."""
    return extract_from_answers(
        doc,
        guarded,
        PayInvoice,
        einvoice_answers(doc),
        source=SOURCE,
        source_version=SOURCE_VERSION,
    )
