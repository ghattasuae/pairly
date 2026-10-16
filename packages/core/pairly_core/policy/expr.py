# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""A small, safe expression language for deterministic policy rules.

Rules are written by tenant admins, so the language is designed to be impossible to
escape: there is no ``eval``, no ``exec``, no Python ``ast`` and no attribute access.
A hand-written tokenizer accepts a fixed set of tokens, a recursive descent parser
builds an AST of a handful of node types, a static type checker rejects anything that
is not well typed against the declared facts, and an interpreter walks the AST. Every
function is on an explicit whitelist. Anything else is a parse error.

Grammar (lowest precedence first)::

    expr     := or
    or       := and ("or" and)*
    and      := not ("and" not)*
    not      := "not" not | compare
    compare  := sum [ ("==" | "!=" | "<" | "<=" | ">" | ">=") sum
                    | "in" sum | "not" "in" sum
                    | "between" sum "and" sum ]
    sum      := product (("+" | "-") product)*
    product  := unary (("*" | "/") unary)*
    unary    := "-" unary | primary
    primary  := NUMBER | STRING | "true" | "false" | FACT | CALL | "(" expr ")"
              | "[" expr ("," expr)* "]"

Types: ``decimal`` (exact, ``Decimal``), ``string``, ``bool``, ``date``, ``duration``
(whole days) and ``list<decimal|string|date>``. Numbers are always ``Decimal``, never
float. Date math: ``date + days(30)``, ``due_date - invoice_date > days(45)``,
``days_between(a, b)``. ``today()`` is the evaluation's ``as_of`` date, which the caller
records, so a replay sees the same day.

Facts are dotted names (``vendor.is_new``) that must be declared in the rule set with a
type. A required fact that is missing makes the rule an ``error`` (fail closed); only
facts declared ``optional`` may be absent, and ``exists(fact)`` tests for them.

Limits: 2000 characters, 400 tokens, nesting depth 32, 100 list items, 500 characters
per string literal, 30 integer digits per number literal. Arithmetic runs in a bounded
decimal context; overflow and division by zero are evaluation errors.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import (
    ROUND_HALF_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import Any, Final

MAX_EXPR_CHARS: Final = 2000
MAX_TOKENS: Final = 400
MAX_DEPTH: Final = 32
MAX_LIST_ITEMS: Final = 100
MAX_STRING_CHARS: Final = 500
MAX_FACT_STRING_CHARS: Final = 10_000
MAX_FACT_LIST_ITEMS: Final = 1000
MAX_DAYS: Final = 36_500
# Results whose magnitude passes 10**MAX_MAGNITUDE are treated as overflow.
MAX_MAGNITUDE: Final = 30

DECIMAL: Final = "decimal"
STRING: Final = "string"
BOOL: Final = "bool"
DATE: Final = "date"
DURATION: Final = "duration"
SCALAR_TYPES: Final = frozenset({DECIMAL, STRING, BOOL, DATE})
LIST_TYPES: Final = frozenset({f"list<{t}>" for t in (DECIMAL, STRING, DATE)})
FACT_TYPES: Final = SCALAR_TYPES | LIST_TYPES
_ORDERED = frozenset({DECIMAL, DATE, DURATION})

FACT_PATH_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}(\.[a-z][a-z0-9_]{0,63}){0,3}$")

_KEYWORDS = frozenset({"and", "or", "not", "in", "between", "true", "false"})

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<number>\d{1,30}(?:\.\d{1,20})?(?![\w.]))
  | (?P<string>'(?:[^'\\\n]|\\['\\])*'|"(?:[^"\\\n]|\\["\\])*")
  | (?P<name>[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)
  | (?P<op>==|!=|<=|>=|<|>|\+|-|\*|/|\(|\)|\[|\]|,)
    """,
    re.VERBOSE,
)


def _decimal_context() -> Context:
    return Context(
        prec=34,
        rounding=ROUND_HALF_UP,
        Emax=999_999,
        Emin=-999_999,
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


class ExprError(ValueError):
    """The expression is not valid (syntax, unknown name, wrong types). Safe to show."""


class EvalError(Exception):
    """Evaluation could not complete. ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MissingFactError(EvalError):
    def __init__(self, path: str) -> None:
        super().__init__("missing_fact", f"fact {path!r} is missing")
        self.path = path


# ------------------------------------------------------------------ tokens


@dataclass(frozen=True, slots=True)
class Token:
    kind: str  # number | string | name | keyword | op | end
    text: str
    pos: int


def tokenize(source: str) -> list[Token]:
    if not isinstance(source, str):
        raise ExprError("the expression must be text")
    if len(source) > MAX_EXPR_CHARS:
        raise ExprError(f"the expression is longer than {MAX_EXPR_CHARS} characters")
    tokens: list[Token] = []
    pos = 0
    while pos < len(source):
        match = _TOKEN_RE.match(source, pos)
        if match is None:
            raise ExprError(f"unexpected character {source[pos]!r} at position {pos}")
        kind = match.lastgroup or ""
        text = match.group()
        if kind == "name" and text in _KEYWORDS:
            kind = "keyword"
        if kind != "ws":
            tokens.append(Token(kind, text, pos))
            if len(tokens) > MAX_TOKENS:
                raise ExprError(f"the expression has more than {MAX_TOKENS} tokens")
        pos = match.end()
    tokens.append(Token("end", "", len(source)))
    return tokens


def _unquote(text: str) -> str:
    body = text[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            i += 1
            ch = body[i]
        out.append(ch)
        i += 1
    value = "".join(out)
    if len(value) > MAX_STRING_CHARS:
        raise ExprError(f"a string literal is longer than {MAX_STRING_CHARS} characters")
    return value


# ------------------------------------------------------------------ AST


@dataclass(frozen=True, slots=True)
class Literal:
    value: Decimal | str | bool | date
    type: str


@dataclass(frozen=True, slots=True)
class Fact:
    path: str
    type: str


@dataclass(frozen=True, slots=True)
class ListLit:
    items: tuple[Node, ...]
    type: str


@dataclass(frozen=True, slots=True)
class Unary:
    op: str  # "-" | "not"
    operand: Node
    type: str


@dataclass(frozen=True, slots=True)
class Binary:
    op: str  # + - * / == != < <= > >= and or in not_in
    left: Node
    right: Node
    type: str


@dataclass(frozen=True, slots=True)
class Between:
    value: Node
    low: Node
    high: Node
    type: str = BOOL


@dataclass(frozen=True, slots=True)
class Call:
    name: str
    args: tuple[Node, ...]
    type: str


Node = Literal | Fact | ListLit | Unary | Binary | Between | Call


@dataclass(frozen=True, slots=True)
class FactSpec:
    type: str
    optional: bool = False


# ------------------------------------------------------------------ functions

# name -> (argument type patterns, result type). "num+" means one or more decimals.
_FUNCTIONS: Final[dict[str, tuple[tuple[str, ...], str]]] = {
    "date": (("string_literal",), DATE),
    "days": ((DECIMAL,), DURATION),
    "days_between": ((DATE, DATE), DECIMAL),
    "today": ((), DATE),
    "abs": ((DECIMAL,), DECIMAL),
    "min": (("num+",), DECIMAL),
    "max": (("num+",), DECIMAL),
    "round": ((DECIMAL, DECIMAL), DECIMAL),
    "lower": ((STRING,), STRING),
    "len": (("string_or_list",), DECIMAL),
    "contains": ((STRING, STRING), BOOL),
    "starts_with": ((STRING, STRING), BOOL),
    "exists": (("fact",), BOOL),
}
FUNCTION_NAMES: Final = frozenset(_FUNCTIONS)


# ------------------------------------------------------------------ parser


class _Parser:
    def __init__(self, tokens: list[Token], facts: Mapping[str, FactSpec]) -> None:
        self.tokens = tokens
        self.facts = facts
        self.i = 0
        self.depth = 0

    # -- helpers
    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def _next(self) -> Token:
        token = self.tokens[self.i]
        self.i += 1
        return token

    def _accept(self, kind: str, text: str | None = None) -> Token | None:
        token = self.tok
        if token.kind == kind and (text is None or token.text == text):
            self.i += 1
            return token
        return None

    def _expect(self, kind: str, text: str) -> Token:
        token = self._accept(kind, text)
        if token is None:
            got = self.tok.text or "end of expression"
            raise ExprError(f"expected {text!r} at position {self.tok.pos}, got {got!r}")
        return token

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExprError(f"the expression is nested deeper than {MAX_DEPTH} levels")

    def _leave(self) -> None:
        self.depth -= 1

    # -- grammar
    def parse(self) -> Node:
        node = self._or()
        if self.tok.kind != "end":
            raise ExprError(f"unexpected {self.tok.text!r} at position {self.tok.pos}")
        return node

    def _or(self) -> Node:
        self._enter()
        node = self._and()
        while self._accept("keyword", "or"):
            node = _bool_op("or", node, self._and())
        self._leave()
        return node

    def _and(self) -> Node:
        node = self._not()
        while self._accept("keyword", "and"):
            node = _bool_op("and", node, self._not())
        return node

    def _not(self) -> Node:
        if self.tok.kind == "keyword" and self.tok.text == "not":
            # "not in" belongs to compare; a leading "not" is negation.
            self._next()
            self._enter()
            operand = self._not()
            self._leave()
            if operand.type != BOOL:
                raise ExprError("'not' needs a true/false value")
            return Unary("not", operand, BOOL)
        return self._compare()

    def _compare(self) -> Node:
        left = self._sum()
        token = self.tok
        if token.kind == "op" and token.text in {"==", "!=", "<", "<=", ">", ">="}:
            self._next()
            right = self._sum()
            return _compare(token.text, left, right)
        if token.kind == "keyword" and token.text == "in":
            self._next()
            return _membership("in", left, self._sum())
        if token.kind == "keyword" and token.text == "not":
            nxt = self.tokens[self.i + 1]
            if nxt.kind == "keyword" and nxt.text == "in":
                self.i += 2
                return _membership("not_in", left, self._sum())
        if token.kind == "keyword" and token.text == "between":
            self._next()
            low = self._sum()
            self._expect("keyword", "and")
            high = self._sum()
            if not (left.type == low.type == high.type) or left.type not in _ORDERED:
                raise ExprError("'between' needs three values of the same orderable type")
            return Between(left, low, high)
        return left

    def _sum(self) -> Node:
        node = self._product()
        while self.tok.kind == "op" and self.tok.text in {"+", "-"}:
            op = self._next().text
            node = _arith(op, node, self._product())
        return node

    def _product(self) -> Node:
        node = self._unary()
        while self.tok.kind == "op" and self.tok.text in {"*", "/"}:
            op = self._next().text
            node = _arith(op, node, self._unary())
        return node

    def _unary(self) -> Node:
        if self._accept("op", "-"):
            self._enter()
            operand = self._unary()
            self._leave()
            if operand.type not in {DECIMAL, DURATION}:
                raise ExprError("'-' needs a number or a duration")
            if isinstance(operand, Literal) and isinstance(operand.value, Decimal):
                return Literal(-operand.value, DECIMAL)
            return Unary("-", operand, operand.type)
        return self._primary()

    def _primary(self) -> Node:
        token = self.tok
        if token.kind == "number":
            self._next()
            return Literal(Decimal(token.text), DECIMAL)
        if token.kind == "string":
            self._next()
            return Literal(_unquote(token.text), STRING)
        if token.kind == "keyword" and token.text in {"true", "false"}:
            self._next()
            return Literal(token.text == "true", BOOL)
        if token.kind == "name":
            self._next()
            if self._accept("op", "("):
                return self._call(token)
            spec = self.facts.get(token.text)
            if spec is None:
                raise ExprError(f"unknown fact {token.text!r}; declare it under 'facts'")
            return Fact(token.text, spec.type)
        if self._accept("op", "("):
            self._enter()
            node = self._or()
            self._leave()
            self._expect("op", ")")
            return node
        if self._accept("op", "["):
            return self._list()
        got = token.text or "end of expression"
        raise ExprError(f"unexpected {got!r} at position {token.pos}")

    def _list(self) -> Node:
        items: list[Node] = []
        self._enter()
        while True:
            items.append(self._or())
            if len(items) > MAX_LIST_ITEMS:
                raise ExprError(f"a list has more than {MAX_LIST_ITEMS} items")
            if self._accept("op", ","):
                continue
            self._expect("op", "]")
            break
        self._leave()
        types = {item.type for item in items}
        if len(types) != 1 or (item_type := types.pop()) not in {DECIMAL, STRING, DATE}:
            raise ExprError("list items must all be numbers, all strings or all dates")
        return ListLit(tuple(items), f"list<{item_type}>")

    def _call(self, name_token: Token) -> Node:
        name = name_token.text
        signature = _FUNCTIONS.get(name)
        if signature is None:
            raise ExprError(f"unknown function {name!r}")
        args: list[Node] = []
        self._enter()
        if not self._accept("op", ")"):
            while True:
                args.append(self._or())
                if len(args) > 10:
                    raise ExprError(f"too many arguments for {name}()")
                if self._accept("op", ","):
                    continue
                self._expect("op", ")")
                break
        self._leave()
        return _check_call(name, tuple(args), signature)


def _bool_op(op: str, left: Node, right: Node) -> Node:
    if left.type != BOOL or right.type != BOOL:
        raise ExprError(f"'{op}' needs true/false values on both sides")
    return Binary(op, left, right, BOOL)


def _compare(op: str, left: Node, right: Node) -> Node:
    if left.type != right.type:
        raise ExprError(f"cannot compare {left.type} with {right.type} using '{op}'")
    if op in {"==", "!="}:
        if left.type not in SCALAR_TYPES | {DURATION}:
            raise ExprError(f"'{op}' needs single values, not lists")
    elif left.type not in _ORDERED:
        raise ExprError(f"'{op}' needs numbers, dates or durations")
    return Binary(op, left, right, BOOL)


def _membership(op: str, item: Node, container: Node) -> Node:
    if container.type not in LIST_TYPES or container.type != f"list<{item.type}>":
        raise ExprError(f"'in' needs a {item.type} value and a list of {item.type}")
    return Binary(op, item, container, BOOL)


def _arith(op: str, left: Node, right: Node) -> Node:
    lt, rt = left.type, right.type
    result: str | None = None
    if lt == DECIMAL and rt == DECIMAL:
        result = DECIMAL
    elif op == "+" and {lt, rt} == {DATE, DURATION}:
        result = DATE
    elif op in {"+", "-"} and lt == DURATION and rt == DURATION:
        result = DURATION
    elif op == "-" and lt == DATE and rt == DURATION:
        result = DATE
    elif op == "-" and lt == DATE and rt == DATE:
        result = DURATION
    if result is None:
        raise ExprError(f"cannot apply '{op}' to {lt} and {rt}")
    return Binary(op, left, right, result)


def _check_call(name: str, args: tuple[Node, ...], signature: tuple[tuple[str, ...], str]) -> Node:
    params, result = signature
    if params == ("num+",):
        if not args or any(a.type != DECIMAL for a in args):
            raise ExprError(f"{name}() needs one or more numbers")
        return Call(name, args, result)
    if len(args) != len(params):
        raise ExprError(f"{name}() takes {len(params)} argument(s)")
    for arg, param in zip(args, params, strict=True):
        if param == "string_literal":
            if not (isinstance(arg, Literal) and isinstance(arg.value, str)):
                raise ExprError(f"{name}() needs a quoted literal")
            try:
                parsed = date.fromisoformat(arg.value)
            except ValueError:
                raise ExprError(f"{name}() needs a date written YYYY-MM-DD") from None
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg.value):
                raise ExprError(f"{name}() needs a date written YYYY-MM-DD")
            return Literal(parsed, DATE)
        if param == "fact":
            if not isinstance(arg, Fact):
                raise ExprError(f"{name}() takes a fact name")
        elif param == "string_or_list":
            if arg.type != STRING and arg.type not in LIST_TYPES:
                raise ExprError(f"{name}() needs a string or a list")
        elif arg.type != param:
            raise ExprError(f"{name}() needs {', '.join(params)}")
    return Call(name, args, result)


def facts_in(node: Node, *, include_exists: bool = True) -> frozenset[str]:
    """Fact paths the expression references. ``exists(x)`` counts only when asked."""
    found: set[str] = set()

    def walk(n: Node) -> None:
        if isinstance(n, Fact):
            found.add(n.path)
        elif isinstance(n, ListLit):
            for item in n.items:
                walk(item)
        elif isinstance(n, Unary):
            walk(n.operand)
        elif isinstance(n, Binary):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, Between):
            walk(n.value)
            walk(n.low)
            walk(n.high)
        elif isinstance(n, Call):
            if n.name == "exists" and not include_exists:
                return
            for arg in n.args:
                walk(arg)

    walk(node)
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class Expression:
    """A parsed, type-checked boolean expression."""

    source: str
    root: Node
    facts: frozenset[str]

    def evaluate(self, env: Environment) -> bool:
        value = _eval(self.root, env)
        if not isinstance(value, bool):  # pragma: no cover - guaranteed by the checker
            raise EvalError("type_error", "the condition did not produce true or false")
        return value


def compile_expression(source: str, facts: Mapping[str, FactSpec]) -> Expression:
    """Parse and type-check ``source`` against the declared ``facts``. Must be boolean."""
    tokens = tokenize(source)
    if len(tokens) == 1:
        raise ExprError("the expression is empty")
    root = _Parser(tokens, facts).parse()
    if root.type != BOOL:
        raise ExprError("the condition must be true or false")
    return Expression(source, root, facts_in(root))


# ------------------------------------------------------------------ facts


def lookup(facts: Mapping[str, Any], path: str) -> object:
    """Dotted path lookup; ``None`` when any part is missing."""
    value: object = facts
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("bool")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        number = Decimal(repr(value))
    elif isinstance(value, str) and len(value) <= 64:
        number = Decimal(value.strip())
    else:
        raise ValueError("not a number")
    if not number.is_finite() or (number != 0 and number.adjusted() > MAX_MAGNITUDE):
        raise ValueError("out of range")
    return number


def _to_date(value: object) -> date:
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    raise ValueError("not a date")


def _coerce_scalar(value: object, type_: str) -> Decimal | str | bool | date:
    if type_ == DECIMAL:
        return _to_decimal(value)
    if type_ == STRING:
        if not isinstance(value, str) or len(value) > MAX_FACT_STRING_CHARS:
            raise ValueError("not a string")
        return value
    if type_ == BOOL:
        if not isinstance(value, bool):
            raise ValueError("not true or false")
        return value
    if type_ == DATE:
        return _to_date(value)
    raise ValueError(f"unknown type {type_}")  # pragma: no cover


def coerce_fact(value: object, type_: str) -> Any:
    """Convert a caller value to the declared type, or raise ``ValueError``."""
    if type_ in LIST_TYPES:
        if not isinstance(value, list | tuple) or len(value) > MAX_FACT_LIST_ITEMS:
            raise ValueError("not a list")
        item_type = type_[5:-1]
        return tuple(_coerce_scalar(v, item_type) for v in value)
    return _coerce_scalar(value, type_)


class Environment:
    """Typed facts for one evaluation, coerced lazily and cached."""

    def __init__(
        self, facts: Mapping[str, Any], specs: Mapping[str, FactSpec], *, as_of: date
    ) -> None:
        self._facts = facts
        self._specs = specs
        self._cache: dict[str, Any] = {}
        self.as_of = as_of

    def present(self, path: str) -> bool:
        return lookup(self._facts, path) is not None

    def get(self, path: str) -> Any:
        if path in self._cache:
            return self._cache[path]
        spec = self._specs.get(path)
        if spec is None:  # pragma: no cover - the parser only admits declared facts
            raise EvalError("unknown_fact", f"fact {path!r} is not declared")
        raw = lookup(self._facts, path)
        if raw is None:
            raise MissingFactError(path)
        try:
            value = coerce_fact(raw, spec.type)
        except (ValueError, ArithmeticError):
            raise EvalError("invalid_fact", f"fact {path!r} is not a valid {spec.type}") from None
        self._cache[path] = value
        return value

    def used(self) -> dict[str, Any]:
        return dict(self._cache)


# ------------------------------------------------------------------ interpreter


def _check_magnitude(value: Decimal) -> Decimal:
    if not value.is_finite() or (value != 0 and value.adjusted() > MAX_MAGNITUDE):
        raise EvalError("overflow", "a number is too large")
    return value


def _days(value: Decimal) -> timedelta:
    if value != value.to_integral_value() or abs(value) > MAX_DAYS:
        raise EvalError("invalid_duration", "days() needs a whole number of days within range")
    return timedelta(days=int(value))


def _eval(node: Node, env: Environment) -> Any:
    try:
        with localcontext(_decimal_context()):
            return _eval_node(node, env)
    except (ArithmeticError, OverflowError) as exc:
        if isinstance(exc, DivisionByZero | ZeroDivisionError):
            raise EvalError("division_by_zero", "division by zero") from None
        raise EvalError("arithmetic_error", "the arithmetic could not be completed") from None


def _eval_node(node: Node, env: Environment) -> Any:
    if isinstance(node, Literal):
        return node.value
    if isinstance(node, Fact):
        return env.get(node.path)
    if isinstance(node, ListLit):
        return tuple(_eval_node(item, env) for item in node.items)
    if isinstance(node, Unary):
        value = _eval_node(node.operand, env)
        if node.op == "not":
            return not value
        return -value
    if isinstance(node, Between):
        value = _eval_node(node.value, env)
        return bool(_eval_node(node.low, env) <= value <= _eval_node(node.high, env))
    if isinstance(node, Call):
        return _call(node, env)
    return _binary(node, env)


def _binary(node: Binary, env: Environment) -> Any:
    op = node.op
    if op == "and":
        return bool(_eval_node(node.left, env)) and bool(_eval_node(node.right, env))
    if op == "or":
        return bool(_eval_node(node.left, env)) or bool(_eval_node(node.right, env))
    left = _eval_node(node.left, env)
    right = _eval_node(node.right, env)
    if op in {"in", "not_in"}:
        found = left in right
        return found if op == "in" else not found
    comparisons: dict[str, Callable[[Any, Any], bool]] = {
        "==": lambda a, b: bool(a == b),
        "!=": lambda a, b: bool(a != b),
        "<": lambda a, b: bool(a < b),
        "<=": lambda a, b: bool(a <= b),
        ">": lambda a, b: bool(a > b),
        ">=": lambda a, b: bool(a >= b),
    }
    if op in comparisons:
        return comparisons[op](left, right)
    if op == "+":
        result = left + right
    elif op == "-":
        result = left - right
    elif op == "*":
        result = left * right
    else:
        if right == 0:
            raise EvalError("division_by_zero", "division by zero")
        result = left / right
    if isinstance(result, Decimal):
        return _check_magnitude(result)
    if isinstance(result, timedelta) and abs(result.days) > MAX_DAYS * 10:
        raise EvalError("overflow", "a duration is too large")
    return result


def _call(node: Call, env: Environment) -> Any:
    name = node.name
    if name == "exists":
        fact = node.args[0]
        assert isinstance(fact, Fact)  # noqa: S101 (checked by the parser)
        if not env.present(fact.path):
            return False
        env.get(fact.path)  # present but malformed is an error, not "absent"
        return True
    if name == "today":
        return env.as_of
    args: Sequence[Any] = [_eval_node(a, env) for a in node.args]
    if name == "days":
        return _days(args[0])
    if name == "days_between":
        return Decimal((args[1] - args[0]).days)
    if name == "abs":
        return abs(args[0])
    if name == "min":
        return min(args)
    if name == "max":
        return max(args)
    if name == "round":
        places = args[1]
        if places != places.to_integral_value() or not 0 <= places <= 10:
            raise EvalError("invalid_argument", "round() needs 0 to 10 decimal places")
        return args[0].quantize(Decimal(1).scaleb(-int(places)), rounding=ROUND_HALF_UP)
    if name == "lower":
        return str(args[0]).casefold()
    if name == "len":
        return Decimal(len(args[0]))
    if name == "contains":
        return args[1] in args[0]
    if name == "starts_with":
        return bool(str(args[0]).startswith(args[1]))
    raise EvalError("unknown_function", f"unknown function {name}")  # pragma: no cover


def format_value(value: object) -> str:
    """Human-readable form of a fact value for reason texts."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return f"{value.days} days"
    if isinstance(value, tuple | list):
        return ", ".join(format_value(v) for v in value)
    return str(value)


def json_value(value: object) -> Any:
    """JSON-safe form of a coerced fact value (exact numbers as strings)."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [json_value(v) for v in value]
    return str(value)
