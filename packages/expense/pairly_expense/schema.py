# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""What the workflow reads from a receipt: an extraction schema (``pairly_core.extraction``).

Every field is ``Extracted[T]``: the value comes with a confidence and the place in the
receipt it was read from. A value whose quote cannot be found in the text the model saw
gets confidence 0 and the ``no_evidence`` flag. Thresholds say how sure the model must
be before a value counts without a person checking it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, ClassVar, Final, Literal

from pairly_core.extraction.types import Extracted, ExtractionModel, Money, Rule

SCHEMA_NAME: Final = "expense.receipt"

Currency = Literal["AED", "USD", "EUR", "GBP", "SAR"]


class ExpenseCategory(StrEnum):
    MEALS = "meals"
    TRAVEL = "travel"
    LODGING = "lodging"
    TRANSPORT = "transport"
    OFFICE_SUPPLIES = "office_supplies"
    SOFTWARE = "software"
    OTHER = "other"


class ExpenseReceipt(ExtractionModel):
    schema_name: ClassVar[str] = SCHEMA_NAME
    schema_version: ClassVar[str] = "1"
    default_threshold: ClassVar[float] = 0.8
    schema_description: ClassVar[str] = (
        "A receipt for something an employee paid for and wants reimbursed."
    )

    merchant: Annotated[
        Extracted[str],
        Rule(required=True, threshold=0.8, max_length=200, description="Who was paid"),
    ]
    date: Annotated[
        Extracted[dt.date],
        Rule(required=True, threshold=0.85, date_order="DMY", description="Date of purchase"),
    ]
    total: Annotated[
        Extracted[Money],
        Rule(
            required=True,
            threshold=0.9,
            minimum=Decimal("0.01"),
            maximum=Decimal("1000000"),
            default_currency="AED",
            description="Total paid, tax included",
        ),
    ]
    currency: Annotated[
        Extracted[Currency],
        Rule(required=True, threshold=0.8, description="Currency of the total"),
    ]
    category: Annotated[
        Extracted[ExpenseCategory],
        Rule(required=True, threshold=0.6, description="Kind of expense"),
    ]
    description: Annotated[
        Extracted[str],
        Rule(threshold=0.6, max_length=300, description="What was bought, in a few words"),
    ]
