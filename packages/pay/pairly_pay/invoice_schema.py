# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""What Pay reads from an invoice: the extraction schema (brief 4.3).

Every field is ``Extracted[T]``: a value with a confidence and the place in the invoice
it was read from (quotes and spans). A value whose quote cannot be found in the text the
model saw gets confidence 0 and the ``no_evidence`` flag. Amounts are exact ``Decimal``
values in the invoice currency (never floats); the currency is its own field, so an
amount printed without a currency is still read.

Thresholds are per field. Money that decides what is paid (subtotal, VAT, total) and
the invoice number (duplicate checks) must be read with high confidence; descriptive
text may be less sure. Any required field below its threshold, and any optional field
that has a value but needs correction, sends the invoice to a person
(``route_optional_issues``).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, ClassVar, Final, Literal

from pairly_core.extraction.types import (
    Extracted,
    ExtractionModel,
    Iban,
    Percent,
    Rule,
    TrnAE,
)

SCHEMA_NAME: Final = "pay.invoice"
SCHEMA_VERSION: Final = "1"

Currency = Literal["AED", "USD", "EUR", "GBP", "SAR", "BHD", "OMR", "KWD", "QAR"]

_ZERO: Final = Decimal("0")
_MAX_AMOUNT: Final = Decimal("1000000000")
_MAX_QUANTITY: Final = Decimal("10000000")


def _amount(description: str, *, required: bool, threshold: float) -> Rule:
    return Rule(
        required=required,
        threshold=threshold,
        minimum=_ZERO,
        maximum=_MAX_AMOUNT,
        description=description,
    )


class InvoiceLine(ExtractionModel):
    description: Annotated[
        Extracted[str],
        Rule(required=True, threshold=0.7, max_length=300, description="What was sold"),
    ]
    quantity: Annotated[
        Extracted[Decimal],
        Rule(threshold=0.8, minimum=_ZERO, maximum=_MAX_QUANTITY, description="Quantity"),
    ]
    unit_price: Annotated[
        Extracted[Decimal], _amount("Price of one unit", required=False, threshold=0.8)
    ]
    vat_rate: Annotated[
        Extracted[Percent], Rule(threshold=0.8, description="VAT rate of the line, in percent")
    ]
    vat_amount: Annotated[
        Extracted[Decimal], _amount("VAT amount of the line", required=False, threshold=0.8)
    ]
    line_total: Annotated[
        Extracted[Decimal],
        _amount("Line total before VAT (quantity times unit price)", required=True, threshold=0.85),
    ]


class BankOnInvoice(ExtractionModel):
    """Bank details printed on the invoice. Never trusted for payment: WP4.6 compares
    them with the vendor master and opens a change ticket when they differ."""

    iban: Annotated[Extracted[Iban], Rule(threshold=0.9, description="IBAN printed")]
    account_name: Annotated[
        Extracted[str], Rule(threshold=0.7, max_length=200, description="Account holder name")
    ]
    bank_name: Annotated[
        Extracted[str], Rule(threshold=0.6, max_length=160, description="Bank name")
    ]
    swift: Annotated[
        Extracted[str], Rule(threshold=0.8, max_length=11, description="SWIFT or BIC code")
    ]


class PayInvoice(ExtractionModel):
    schema_name: ClassVar[str] = SCHEMA_NAME
    schema_version: ClassVar[str] = SCHEMA_VERSION
    default_threshold: ClassVar[float] = 0.8
    schema_description: ClassVar[str] = (
        "A supplier's invoice (tax invoice) sent to us for payment. Read it as printed; "
        "dates as YYYY-MM-DD, amounts as plain numbers in the invoice currency."
    )

    vendor_name: Annotated[
        Extracted[str],
        Rule(required=True, threshold=0.8, max_length=200, description="Supplier name"),
    ]
    vendor_trn: Annotated[
        Extracted[TrnAE], Rule(threshold=0.9, description="Supplier VAT number (TRN), 15 digits")
    ]
    invoice_number: Annotated[
        Extracted[str],
        Rule(required=True, threshold=0.9, max_length=64, description="Invoice number"),
    ]
    invoice_date: Annotated[
        Extracted[dt.date],
        Rule(required=True, threshold=0.85, date_order="DMY", description="Invoice date"),
    ]
    due_date: Annotated[
        Extracted[dt.date], Rule(threshold=0.8, date_order="DMY", description="Due date")
    ]
    payment_terms: Annotated[
        Extracted[str], Rule(threshold=0.6, max_length=120, description="Payment terms")
    ]
    currency: Annotated[
        Extracted[Currency], Rule(required=True, threshold=0.85, description="Invoice currency")
    ]
    po_number: Annotated[
        Extracted[str], Rule(threshold=0.8, max_length=64, description="Purchase order number")
    ]
    subtotal: Annotated[
        Extracted[Decimal], _amount("Total before VAT", required=True, threshold=0.9)
    ]
    vat_rate: Annotated[Extracted[Percent], Rule(threshold=0.8, description="VAT rate, in percent")]
    vat_amount: Annotated[Extracted[Decimal], _amount("Total VAT", required=True, threshold=0.9)]
    total: Annotated[
        Extracted[Decimal], _amount("Total to pay, VAT included", required=True, threshold=0.95)
    ]
    bank: BankOnInvoice
    line_items: Annotated[list[InvoiceLine], Rule(required=True, max_items=100)]


# Paths whose values never become few-shot hints or leave the extraction tables: bank
# details (a learned IBAN would teach the model someone's account number).
SENSITIVE_PREFIXES: Final = ("bank.",)
# Header fields copied onto the invoice row (the current value, corrections included).
HEADER_FIELDS: Final = (
    "vendor_name",
    "vendor_trn",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "po_number",
    "subtotal",
    "vat_amount",
    "total",
)
