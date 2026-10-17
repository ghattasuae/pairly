# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Deterministic post-validators, quote normalisation, stored specs and permissions."""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, ClassVar

import pytest

from pairly_core.extraction.evidence import find_normalized, normalize
from pairly_core.extraction.permissions import extraction_permission
from pairly_core.extraction.schema import StoredSpecs, compile_schema
from pairly_core.extraction.types import (
    Extracted,
    ExtractionModel,
    Iban,
    Money,
    Rule,
    TrnAE,
    json_value,
)
from pairly_core.extraction.validators import (
    FieldKind,
    InvalidValue,
    ValueSpec,
    find_dates,
    find_numbers,
    iban_valid,
    parse_money,
    parse_number,
    parse_value,
    shape_for,
    supported_by_quote,
)
from pairly_core.rbac import PermissionSpec, build_registry
from pairly_core.workflow.spec import GraphSpec

from .conftest import IBAN, Invoice, PassThroughInput


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,250.00", "1250.00"),
        ("1.250,00", "1250.00"),
        ("1250", "1250"),
        ("1,250", "1250"),
        ("12,5", "12.5"),
        ("1,234,567.89", "1234567.89"),
        ("1.234.567", "1234567"),
        ("(1,250.00)", "-1250.00"),
        ("-5", "-5"),
        ("250.00-", "-250.00"),
        ("1\u00a0250,50", "1250.50"),
        ("\u0661\u0662\u0663\u066b\u0664\u0665", "123.45"),
    ],
)
def test_parse_number(text: str, expected: str) -> None:
    value = parse_number(text)
    assert isinstance(value, Decimal)
    assert value == Decimal(expected)


@pytest.mark.parametrize("text", ["", "abc", "1,2,3", "1.2.3.4,5,6", "NaN", "1e5", "1.", "9" * 30])
def test_parse_number_rejects(text: str) -> None:
    with pytest.raises(InvalidValue):
        parse_number(text)


def test_money_is_exact_decimal_with_currency() -> None:
    assert parse_money("AED 1,250.10", None) == Money(amount=Decimal("1250.10"), currency="AED")
    assert parse_money("1250.10 usd", None) == Money(amount=Decimal("1250.10"), currency="USD")
    assert parse_money("99.99", "EUR").currency == "EUR"
    with pytest.raises(InvalidValue):
        parse_money("99.99", None)
    total = sum((parse_money(v, "AED").amount for v in ("0.10", "0.20", "0.30")), Decimal(0))
    assert total == Decimal("0.60")  # no float drift
    assert json_value(Money(amount=Decimal("0.10"), currency="AED")) == {
        "amount": "0.10",
        "currency": "AED",
    }


def test_dates() -> None:
    assert find_dates("Invoice Date: 07/10/2026") == [date(2026, 10, 7)]
    assert find_dates("Invoice Date: 07/10/2026", "MDY") == [date(2026, 7, 10)]
    assert find_dates("due 7 Oct 2026") == [date(2026, 10, 7)]
    assert find_dates("due October 7th, 2026") == [date(2026, 10, 7)]
    assert find_dates("2026-10-07") == [date(2026, 10, 7)]
    assert find_dates("31/02/2026") == []
    assert find_dates("07.10.26") == [date(2026, 10, 7)]
    spec = ValueSpec(FieldKind.DATE, Rule())
    assert parse_value(spec, "2026-10-07") == date(2026, 10, 7)
    with pytest.raises(InvalidValue):
        parse_value(spec, "2026-10-07 or 2026-10-08")


def test_shapes_iban_and_trn() -> None:
    assert iban_valid(IBAN)
    assert not iban_valid(IBAN[:-1] + "7")
    iban = ValueSpec(FieldKind.SHAPE, Rule(), shape=shape_for(Iban))
    assert parse_value(iban, "ae07 0331 2345 6789 0123 456") == IBAN
    with pytest.raises(InvalidValue):
        parse_value(iban, "AE00 0000")
    trn = ValueSpec(FieldKind.SHAPE, Rule(), shape=shape_for(TrnAE))
    assert parse_value(trn, "100-1234-5670-0003") == "100123456700003"
    assert supported_by_quote(iban, IBAN, "IBAN: AE07 0331 2345 6789 0123 456")
    # Spaces, tabs, hyphens and lower case are formatting; a dropped digit is not.
    assert parse_value(iban, " AE07\t0331-2345 6789 0123 456 ") == IBAN
    with pytest.raises(InvalidValue):
        parse_value(iban, IBAN[:-1])  # the live failure: 22 characters of 23
    shape = shape_for(Iban)
    assert shape is not None
    assert "as printed" in shape.description


class Terms(StrEnum):
    NET_30 = "net_30"
    ON_RECEIPT = "on_receipt"


def test_enum_percent_integer_text() -> None:
    enum = ValueSpec(FieldKind.ENUM, Rule(), choices=("net_30", "on_receipt"), enum_type=Terms)
    assert parse_value(enum, "Net 30") is Terms.NET_30
    with pytest.raises(InvalidValue):
        parse_value(enum, "net 60")
    percent = ValueSpec(FieldKind.PERCENT, Rule())
    assert parse_value(percent, "5 %") == Decimal("5")
    with pytest.raises(InvalidValue):
        parse_value(percent, "150")
    integer = ValueSpec(FieldKind.INTEGER, Rule(minimum=Decimal(1), maximum=Decimal(10)))
    assert parse_value(integer, "3") == 3
    for bad in ("2.5", "0", "11"):
        with pytest.raises(InvalidValue):
            parse_value(integer, bad)
    text = ValueSpec(FieldKind.TEXT, Rule(max_length=5))
    with pytest.raises(InvalidValue):
        parse_value(text, "too long")
    assert supported_by_quote(text, "Acme", "From: ACME  Trading")
    assert not supported_by_quote(text, "Acme", "From: Other")
    number = ValueSpec(FieldKind.DECIMAL, Rule())
    assert supported_by_quote(number, Decimal("2362.5"), "Total 2,362.50")
    assert not supported_by_quote(number, Decimal("2362.5"), "Total 2,362.55")
    assert find_numbers("(1,000.00)") == [Decimal("1000.00")]


def test_normalised_find_maps_back_to_raw_offsets() -> None:
    raw = "Total\u00a0due:\n\n  AED\u200b 2,362.50"
    found = find_normalized(normalize(raw), "total due: aed 2,362.50")
    assert found == (0, len(raw))
    assert find_normalized(normalize(raw), "   ") is None
    assert find_normalized(normalize(raw), "AED 9") is None
    assert PassThroughInput(raw).to_original(0, 99) is None


def test_stored_specs_round_trip() -> None:
    compiled = compile_schema(Invoice)
    specs = StoredSpecs.from_json(compiled.field_specs())
    resolved = specs.value_spec("line_items[4].line_total")
    assert resolved is not None
    pattern, spec = resolved
    assert pattern == "line_items[].line_total"
    assert spec.kind is FieldKind.MONEY
    assert parse_value(spec, "12.00").currency == "AED"  # default currency kept
    assert specs.value_spec("line_items[50].line_total") is None
    assert specs.value_spec("line_items.line_total") is None
    assert specs.value_spec("bank_iban") is not None
    assert specs.value_spec("x[") is None


def test_extraction_permission_rule() -> None:
    @dataclasses.dataclass(frozen=True)
    class Toy:
        id: str = "toy"
        name: str = "Toy"
        permissions: tuple[PermissionSpec, ...] = (
            PermissionSpec("toy.extraction.correct", "x", frozenset({"admin"})),
        )
        graphs: tuple[GraphSpec, ...] = ()

    registry = build_registry([Toy()])
    assert extraction_permission(registry, "toy", "correct") == "toy.extraction.correct"
    assert extraction_permission(registry, "toy", "read") == "core.extraction.read"
    assert extraction_permission(registry, "pay", "correct") == "core.extraction.correct"
    assert "core.extraction.correct" not in registry.permissions_for_role("agent")
    with pytest.raises(ValueError, match="unknown"):
        extraction_permission(registry, "toy", "delete")


class StrEnumSchema(ExtractionModel):
    schema_name: ClassVar[str] = "pay.terms"
    schema_version: ClassVar[str] = "1"
    terms: Annotated[Extracted[Terms], Rule(description="Payment terms")]


def test_strenum_schema_description() -> None:
    described = compile_schema(StrEnumSchema).describe()
    assert described["fields"][0] == {
        "path": "terms",
        "required": False,
        "type": "enum",
        "choices": ["net_30", "on_receipt"],
        "description": "Payment terms",
    }
