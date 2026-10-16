# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The rule expression language: parsing, typing, evaluation and safety."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from pairly_core.policy.expr import (
    MAX_DEPTH,
    MAX_EXPR_CHARS,
    Environment,
    EvalError,
    ExprError,
    FactSpec,
    MissingFactError,
    coerce_fact,
    compile_expression,
    format_value,
    json_value,
    tokenize,
)

FACTS = {
    "amount": FactSpec("decimal"),
    "currency": FactSpec("string"),
    "category": FactSpec("string"),
    "invoice_date": FactSpec("date"),
    "due_date": FactSpec("date"),
    "vendor.is_new": FactSpec("bool"),
    "tags": FactSpec("list<string>"),
    "po_number": FactSpec("string", optional=True),
    "quantity": FactSpec("decimal"),
}

SAMPLE: dict[str, Any] = {
    "amount": "12500.50",
    "currency": "AED",
    "category": "software",
    "invoice_date": "2026-09-01",
    "due_date": "2026-10-15",
    "vendor": {"is_new": True},
    "tags": ["saas", "annual"],
    "quantity": 3,
}
AS_OF = date(2026, 10, 7)


def run(source: str, facts: dict[str, Any] | None = None) -> bool:
    expr = compile_expression(source, FACTS)
    return expr.evaluate(Environment(SAMPLE if facts is None else facts, FACTS, as_of=AS_OF))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("amount > 10000", True),
        ("amount >= 12500.50 and amount <= 12500.50", True),
        ("amount == 12500.5", True),
        ("amount != 12500.5", False),
        ("category == 'software' and currency == \"AED\"", True),
        ("category in ['software', 'hardware']", True),
        ("category not in ['software', 'hardware']", False),
        ("'saas' in tags", True),
        ("not ('travel' in tags)", True),
        ("amount between 10000 and 20000", True),
        ("amount * quantity > 37000", True),
        ("amount / 2 == 6250.25", True),
        ("-amount < 0", True),
        ("amount - 500.50 == 12000", True),
        ("vendor.is_new", True),
        ("vendor.is_new == false", False),
        ("due_date - invoice_date > days(30)", True),
        ("invoice_date + days(44) == due_date", True),
        ("due_date - days(44) == invoice_date", True),
        ("days_between(invoice_date, due_date) == 44", True),
        ("invoice_date < date('2026-10-01')", True),
        ("today() - invoice_date >= days(36)", True),
        ("due_date between today() and today() + days(30)", True),
        ("abs(-5) == 5 and min(1, 2, 3) == 1 and max(1, 2) == 2", True),
        ("round(amount, 0) == 12501", True),
        ("lower('ABC') == 'abc' and len(tags) == 2 and len(category) == 8", True),
        ("contains(category, 'soft') and starts_with(currency, 'AE')", True),
        ("exists(po_number) or amount < 100", False),
        ("true and not false", True),
        ("amount > 1 or amount > 2 and false", True),
        ("(amount > 1 or amount > 2) and false", False),
        ("category == 'برمجيات' or currency == 'AED'", True),
    ],
)
def test_evaluates(source: str, expected: bool) -> None:
    assert run(source) is expected


def test_optional_fact_present_and_absent() -> None:
    assert run("exists(po_number) and po_number == 'PO-1'", SAMPLE | {"po_number": "PO-1"})
    assert not run("exists(po_number) and po_number == 'PO-1'")


def test_missing_fact_raises() -> None:
    expr = compile_expression("amount > 10", FACTS)
    with pytest.raises(MissingFactError) as info:
        expr.evaluate(Environment({}, FACTS, as_of=AS_OF))
    assert info.value.path == "amount"
    assert info.value.code == "missing_fact"


@pytest.mark.parametrize(
    ("facts", "code"),
    [
        ({"amount": "ten"}, "invalid_fact"),
        ({"amount": True}, "invalid_fact"),
        ({"amount": "NaN"}, "invalid_fact"),
        ({"amount": "1e400"}, "invalid_fact"),
        ({"amount": [1]}, "invalid_fact"),
    ],
)
def test_malformed_fact(facts: dict[str, Any], code: str) -> None:
    with pytest.raises(EvalError) as info:
        run("amount > 10", facts)
    assert info.value.code == code


def test_division_by_zero_and_overflow() -> None:
    with pytest.raises(EvalError) as info:
        run("amount / (quantity - 3) > 1")
    assert info.value.code == "division_by_zero"
    big = "999999999999999999999999999999"
    with pytest.raises(EvalError) as info:
        run(f"{big} * {big} * {big} > 1")
    assert info.value.code == "overflow"
    with pytest.raises(EvalError):
        run("date('9999-12-01') + days(60) > invoice_date")


def test_days_needs_whole_numbers() -> None:
    with pytest.raises(EvalError):
        run("invoice_date + days(1.5) > invoice_date")
    with pytest.raises(EvalError):
        run("invoice_date + days(99999) > invoice_date")


def test_round_places_checked() -> None:
    with pytest.raises(EvalError):
        run("round(amount, 11) > 1")


# ------------------------------------------------------------------ safety

INJECTIONS = [
    "__import__('os').system('id')",
    "os.system('id')",
    "amount.__class__",
    "amount.__class__.__mro__",
    "eval('1')",
    "exec('import os')",
    "open('/etc/passwd')",
    "getattr(amount, 'real')",
    "lambda: 1",
    "[x for x in tags]",
    "amount if true else 0",
    "amount > 1; import os",
    "amount > 1 # comment",
    "category == 'a' + 'b'",
    "amount ** 2 > 1",
    "amount > 1 and `id`",
    "{'a': 1}",
    "amount > 1\nimport os",
    "category.upper() == 'X'",
    "f'{amount}' == '1'",
    "Amount > 1",
    "amount@1",
    "1 if amount else 2",
    "amount > 0x10",
    "amount > 1e3",
    "amount > 1_000",
    "tags[0] == 'a'",
    "globals()",
    "__builtins__",
    "self",
    "today",
    "date(2026)",
    "date('2026-1-1') > invoice_date",
    "exists('po_number')",
    "days('1') > days(1)",
]


@pytest.mark.parametrize("source", INJECTIONS)
def test_code_injection_rejected(source: str) -> None:
    with pytest.raises(ExprError):
        compile_expression(source, FACTS)


@pytest.mark.parametrize(
    "source",
    [
        "amount",  # not boolean
        "amount > 'x'",
        "category > 'a'",
        "amount and true",
        "category in [1, 2]",
        "[1, 'a'] == [1]",
        "tags == tags",
        "invoice_date + invoice_date > invoice_date",
        "invoice_date * 2 > invoice_date",
        "-category == 'x'",
        "not amount",
        "unknown_fact > 1",
        "amount between 'a' and 'b'",
        "nofunc(1)",
        "abs('a') > 1",
        "min() > 1",
        "contains(1, 2)",
        "len(amount) > 1",
        "amount >",
        "(amount > 1",
        "amount > 1)",
        "",
        "   ",
        "'unterminated",
        "1 2",
    ],
)
def test_type_and_syntax_errors(source: str) -> None:
    with pytest.raises(ExprError):
        compile_expression(source, FACTS)


def test_limits() -> None:
    with pytest.raises(ExprError, match="longer than"):
        compile_expression("amount > 1 or " * 200 + "true", FACTS)
    assert len("amount > 1 or " * 200) > MAX_EXPR_CHARS
    deep = "(" * (MAX_DEPTH + 2) + "true" + ")" * (MAX_DEPTH + 2)
    with pytest.raises(ExprError, match="nested"):
        compile_expression(deep, FACTS)
    nots = "not " * (MAX_DEPTH + 2) + "true"
    with pytest.raises(ExprError, match="nested"):
        compile_expression(nots, FACTS)
    many = "category in [" + ", ".join(f"'c{i}'" for i in range(101)) + "]"
    with pytest.raises(ExprError):
        compile_expression(many, FACTS)
    with pytest.raises(ExprError, match="string literal"):
        compile_expression("category == '" + "x" * 501 + "'", FACTS)
    with pytest.raises(ExprError, match="tokens"):
        compile_expression(" + ".join(["1"] * 250) + " > 1", FACTS)
    with pytest.raises(ExprError):
        tokenize(123)  # type: ignore[arg-type]


def test_string_escapes() -> None:
    assert run("'it\\'s' == \"it's\"")
    assert run("contains('a\\\\b', '\\\\')")


def test_facts_listed_and_exists_counted() -> None:
    expr = compile_expression("exists(po_number) and amount > 1", FACTS)
    assert expr.facts == frozenset({"po_number", "amount"})


def test_coerce_and_format_helpers() -> None:
    assert coerce_fact(1.5, "decimal") == Decimal("1.5")
    assert coerce_fact(["2026-01-02"], "list<date>") == (date(2026, 1, 2),)
    with pytest.raises(ValueError, match="date"):
        coerce_fact("02/01/2026", "date")
    with pytest.raises(ValueError, match="string"):
        coerce_fact(5, "string")
    with pytest.raises(ValueError, match="true or false"):
        coerce_fact("yes", "bool")
    with pytest.raises(ValueError, match="list"):
        coerce_fact("a", "list<string>")
    assert format_value(Decimal("10.50")) == "10.50"
    assert format_value(True) == "true"
    assert format_value((Decimal(1), "a")) == "1, a"
    assert format_value(date(2026, 1, 2)) == "2026-01-02"
    assert json_value((Decimal("1.0"), date(2026, 1, 2), None)) == ["1.0", "2026-01-02", None]
