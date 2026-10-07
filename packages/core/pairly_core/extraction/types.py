# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Building blocks of extraction schemas.

A workflow declares what it wants from a document as a Pydantic model whose fields are
``Extracted[T]`` values (or nested models, or lists of nested models for line items)::

    class InvoiceLine(ExtractionModel):
        description: Annotated[Extracted[str], Rule(required=True)]
        quantity: Extracted[Decimal]
        line_total: Annotated[Extracted[Money], Rule(required=True, threshold=0.9)]

    class Invoice(ExtractionModel):
        schema_name: ClassVar[str] = "pay.invoice"
        schema_version: ClassVar[str] = "1"

        invoice_number: Annotated[Extracted[str], Rule(required=True)]
        invoice_date: Annotated[Extracted[date], Rule(required=True, date_order="DMY")]
        vat_rate: Extracted[Percent]
        currency: Extracted[Literal["AED", "USD", "EUR"]]
        bank_iban: Extracted[Iban]
        line_items: Annotated[list[InvoiceLine], Rule(required=True, max_items=100)]

Supported value types: ``str``, ``int``, ``Decimal``, ``date``, ``Money`` (Decimal amount
plus ISO 4217 currency), ``Percent`` (a Decimal in percent units, ``5`` means 5%),
enumerations (``Literal`` of strings or a ``StrEnum``) and registered shape types
(``Iban``, ``TrnAE`` and any type added with ``register_shape``). Anything else is a
schema error raised when the schema is compiled, never at extraction time.

Every ``Extracted`` value carries the evidence for it: the spans of the original
document its quote was found at. A value whose quote cannot be found has confidence 0
and the ``no_evidence`` flag (no evidence, no credit).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field

from pairly_core.documents.model import BBox

# Shape types: plain strings with a deterministic format check (see ``validators``).
Iban = NewType("Iban", str)
TrnAE = NewType("TrnAE", str)
# A percentage in percent units: Decimal("5") is 5%.
Percent = NewType("Percent", Decimal)

DateOrder = Literal["DMY", "MDY", "YMD"]


class Money(BaseModel):
    """An exact amount with its ISO 4217 currency code. Never a float."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class FieldFlag(StrEnum):
    """Why a field needs a person to look at it."""

    # The quote is missing, too long, or cannot be found in the text the model saw.
    NO_EVIDENCE = "no_evidence"
    # The quote was found, but the value cannot be read from it.
    QUOTE_MISMATCH = "quote_mismatch"
    # The value failed its type's deterministic check (date, amount, IBAN, enum...).
    INVALID_VALUE = "invalid_value"
    # Confidence below the field's threshold.
    LOW_CONFIDENCE = "low_confidence"
    # A required field (or a required list) has no value.
    MISSING = "missing"
    # The model returned the same field more than once; the first answer is kept.
    DUPLICATE_OUTPUT = "duplicate_output"


class SourceSpan(BaseModel):
    """Where a value came from: one page's part of a located quote.

    ``start`` and ``end`` are character offsets into ``NormalizedDocument.text`` (the
    original document, pages joined by a blank line). ``quote`` is the exact original
    text at those offsets. ``bboxes`` are the boxes to highlight on ``page`` (one per
    line); empty when the document has no layout (DOCX, plain text).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: int | None
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: str
    bboxes: list[BBox] = Field(default_factory=list)


class Extracted[T](BaseModel):
    """One extracted value with its confidence and evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: T | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    spans: list[SourceSpan] = Field(default_factory=list)
    flags: list[FieldFlag] = Field(default_factory=list)
    # The model's raw answer, kept when the value could not be parsed.
    raw: str | None = None

    @property
    def needs_correction(self) -> bool:
        return bool(self.flags)


@dataclass(frozen=True, slots=True)
class Rule:
    """Per-field rules, attached with ``Annotated[Extracted[T], Rule(...)]``.

    ``threshold``: minimum confidence to accept the value without a person (defaults to
    the schema's ``default_threshold``). ``required``: a missing value routes the whole
    extraction to a person. ``max_items`` applies to lists, ``max_length`` to text,
    ``minimum``/``maximum`` to numbers (inclusive), ``date_order`` to ambiguous dates
    such as 03/04/2026, ``default_currency`` to amounts printed without a currency.
    """

    required: bool = False
    threshold: float | None = None
    description: str | None = None
    max_items: int = 200
    max_length: int = 500
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    date_order: DateOrder = "DMY"
    default_currency: str | None = None

    def __post_init__(self) -> None:
        if self.threshold is not None and not 0 <= self.threshold <= 1:
            raise ValueError("threshold must be within [0, 1]")
        if not 1 <= self.max_items <= 1000:
            raise ValueError("max_items must be 1..1000")
        if not 1 <= self.max_length <= 10_000:
            raise ValueError("max_length must be 1..10000")


class ExtractionModel(BaseModel):
    """Base class of extraction schemas and of their nested (line item) models.

    The top-level schema sets ``schema_name`` (``<workflow>.<name>``) and
    ``schema_version``; both are stored with every extraction.
    ``route_optional_issues``: when True (the default, fail closed), an optional field
    that has a value but needs correction also sends the extraction to a person. An
    optional field that is simply absent never does.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_name: ClassVar[str] = ""
    schema_version: ClassVar[str] = ""
    default_threshold: ClassVar[float] = 0.7
    route_optional_issues: ClassVar[bool] = True
    schema_description: ClassVar[str] = ""


def json_value(value: Any) -> Any:
    """A typed field value as plain JSON (exact: Decimals and dates become strings)."""
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Money):
        return {"amount": str(value.amount), "currency": value.currency}
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    raise TypeError(f"unsupported value type {type(value).__name__}")
