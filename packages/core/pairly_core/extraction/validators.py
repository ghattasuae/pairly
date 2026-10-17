# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Deterministic post-validators: turn a model's answer into a typed, checked value.

The model answers every field as text. Nothing it says is trusted as typed data: each
answer goes through the parser of the field's kind, and a value that does not parse is
dropped (``invalid_value``). Then the value must be readable from its quote
(``supported_by_quote``), so a model cannot quote one thing and answer another.

* Numbers: ``Decimal`` only, never float. ``1,250.00``, ``1.250,00``, ``1 250``,
  ``(1,250.00)`` and Arabic-Indic digits are read; when both separators appear, the last
  one is the decimal separator; a single comma followed by exactly three digits is a
  thousands separator.
* Dates: ISO ``2026-10-07``, numeric ``07/10/2026`` (``Rule.date_order``, default DMY),
  and English month names (``7 Oct 2026``, ``October 7, 2026``).
* Money: an ISO 4217 code plus an amount (``AED 1,250.00`` or ``1250 AED``); the field's
  ``default_currency`` when the text has no code.
* Shapes: a registry of named string formats with a normaliser and a check
  (``iban``: ISO 13616 with the mod-97 checksum; ``trn_ae``: 15 digits). Workflows add
  their own with ``register_shape``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pairly_core.extraction.types import Money, Rule

MAX_NUMBER_DIGITS = 24


class FieldKind(StrEnum):
    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    MONEY = "money"
    PERCENT = "percent"
    ENUM = "enum"
    SHAPE = "shape"


class InvalidValue(ValueError):  # noqa: N818  (reads as a result, not a crash)
    """The model's answer does not parse as the field's type."""


@dataclass(frozen=True, slots=True)
class Shape:
    name: str
    normalize: Callable[[str], str]
    check: Callable[[str], bool]
    description: str


_SHAPES: dict[Any, Shape] = {}


def register_shape(
    type_: Any, name: str, normalize: Callable[[str], str], check: Callable[[str], bool], doc: str
) -> None:
    """Register a ``NewType`` over ``str`` as a shape-checked field type."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
        raise ValueError("invalid shape name")
    _SHAPES[type_] = Shape(name=name, normalize=normalize, check=check, description=doc)


def shape_for(type_: Any) -> Shape | None:
    return _SHAPES.get(type_)


def shape_named(name: str) -> Shape | None:
    for shape in _SHAPES.values():
        if shape.name == name:
            return shape
    return None


def _compact_upper(text: str) -> str:
    return re.sub(r"[\s\-]", "", text).upper()


def iban_valid(value: str) -> bool:
    """ISO 13616 shape plus the ISO 7064 mod-97 checksum."""
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", value):
        return False
    rearranged = value[4:] + value[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def trn_ae_valid(value: str) -> bool:
    """UAE Tax Registration Number shape: 15 digits."""
    return re.fullmatch(r"[0-9]{15}", value) is not None


def _register_builtin_shapes() -> None:
    from pairly_core.extraction.types import Iban, TrnAE

    # The model copies the IBAN as printed (spaces kept); parsing compacts it. Asked
    # for a compact form, small models drop a digit of a 23-character UAE IBAN.
    register_shape(
        Iban,
        "iban",
        _compact_upper,
        iban_valid,
        "IBAN (ISO 13616), copied exactly as printed with its spaces, "
        "e.g. AE07 0331 2345 6789 0123 456",
    )
    register_shape(TrnAE, "trn_ae", _compact_upper, trn_ae_valid, "UAE TRN, 15 digits")


_register_builtin_shapes()


# ------------------------------------------------------------------ text helpers

_DIGITS = str.maketrans(
    {
        **{chr(0x0660 + i): str(i) for i in range(10)},  # Arabic-Indic
        **{chr(0x06F0 + i): str(i) for i in range(10)},  # Extended Arabic-Indic
        "\u066b": ".",  # Arabic decimal separator
        "\u066c": ",",  # Arabic thousands separator
        "\u2212": "-",  # minus sign
    }
)


def ascii_digits(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_DIGITS)


def norm_key(text: str) -> str:
    return re.sub(r"[\s\-_]+", "_", ascii_digits(text).strip().casefold())


# ------------------------------------------------------------------ numbers

_NUMBER_TOKEN = re.compile(r"\(?[-+]?\d(?:[\d.,\u00a0\u202f']*\d)?\)?-?")


def parse_number(text: str) -> Decimal:
    """Parse one number written the way invoices and CVs write numbers."""
    s = ascii_digits(text).strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s.endswith("-"):
        negative, s = True, s[:-1].strip()
    if s.startswith(("-", "+")):
        negative, s = negative or s[0] == "-", s[1:].strip()
    s = re.sub(r"[\s\u00a0\u202f']", "", s)
    if not re.fullmatch(r"\d[\d.,]*", s) or s[-1] in ".,":
        raise InvalidValue("not a number")
    commas, dots = s.count(","), s.count(".")
    if commas and dots:
        decimal_sep = "," if s.rfind(",") > s.rfind(".") else "."
        group_sep = "." if decimal_sep == "," else ","
        if s.count(decimal_sep) > 1:
            raise InvalidValue("ambiguous number")
        s = s.replace(group_sep, "").replace(decimal_sep, ".")
    elif commas:
        tail = s.rsplit(",", 1)[1]
        if commas == 1 and len(tail) != 3:
            s = s.replace(",", ".")
        else:
            _check_groups(s, ",")
            s = s.replace(",", "")
    elif dots > 1:
        _check_groups(s, ".")
        s = s.replace(".", "")
    digits = s.replace(".", "")
    if len(digits) > MAX_NUMBER_DIGITS:
        raise InvalidValue("number too long")
    try:
        value = Decimal(s)
    except InvalidOperation:
        raise InvalidValue("not a number") from None
    if not value.is_finite():
        raise InvalidValue("not a finite number")
    return -value if negative else value


def _check_groups(s: str, sep: str) -> None:
    head, *groups = s.split(sep)
    if not (1 <= len(head) <= 3) or any(len(g) != 3 for g in groups):
        raise InvalidValue("bad digit grouping")


def find_numbers(text: str) -> list[Decimal]:
    """Every number that can be read in ``text`` (absolute values)."""
    found: list[Decimal] = []
    for match in _NUMBER_TOKEN.finditer(ascii_digits(text)):
        token = match.group(0).strip()
        candidates = [token, token.strip("()-+")]
        for candidate in candidates:
            try:
                found.append(abs(parse_number(candidate)))
                break
            except InvalidValue:
                continue
    return found


def _check_range(value: Decimal, rule: Rule) -> None:
    if rule.minimum is not None and value < rule.minimum:
        raise InvalidValue("below the minimum")
    if rule.maximum is not None and value > rule.maximum:
        raise InvalidValue("above the maximum")


# ------------------------------------------------------------------ dates

_MONTHS = {
    name: index
    for index, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}
_MONTH_RE = "(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_DATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("iso", re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")),
    ("numeric", re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?!\d)")),
    (
        "dmy_text",
        re.compile(
            r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s\-]+" + _MONTH_RE + r"[\s\-,]+(\d{4})(?!\d)",
            re.I,
        ),
    ),
    (
        "mdy_text",
        re.compile(
            r"\b" + _MONTH_RE + r"[\s\-]+(\d{1,2})(?:st|nd|rd|th)?,?[\s\-]+(\d{4})(?!\d)", re.I
        ),
    ),
)


def _year(text: str) -> int:
    year = int(text)
    return 2000 + year if len(text) == 2 else year


def _make_date(year: int, month: int, day: int) -> date | None:
    if not 1900 <= year <= 2200:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_dates(text: str, order: str = "DMY") -> list[date]:
    """Every date that can be read in ``text``."""
    s = ascii_digits(text)
    found: list[date] = []
    for kind, pattern in _DATE_PATTERNS:
        for m in pattern.finditer(s):
            parsed: date | None
            if kind == "iso":
                parsed = _make_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif kind == "numeric":
                a, b, y = int(m.group(1)), int(m.group(2)), _year(m.group(3))
                parsed = _make_date(y, a, b) if order == "MDY" else _make_date(y, b, a)
            elif kind == "dmy_text":
                month = _MONTHS[m.group(2).lower()]
                parsed = _make_date(int(m.group(3)), month, int(m.group(1)))
            else:
                month = _MONTHS[m.group(1).lower()]
                parsed = _make_date(int(m.group(3)), month, int(m.group(2)))
            if parsed is not None:
                found.append(parsed)
    return found


def parse_date(text: str, order: str = "DMY") -> date:
    dates = set(find_dates(text, order))
    if len(dates) != 1:
        raise InvalidValue("not a single date")
    return dates.pop()


# ------------------------------------------------------------------ money

_CURRENCY_HEAD = re.compile(r"^([A-Za-z]{3})(?=[\s\d(+-])\s*(.+)$")
_CURRENCY_TAIL = re.compile(r"^(.+?)\s*([A-Za-z]{3})$")


def parse_money(text: str, default_currency: str | None) -> Money:
    s = ascii_digits(text).strip()
    currency: str | None = None
    head = _CURRENCY_HEAD.match(s)
    tail = _CURRENCY_TAIL.match(s)
    if head:
        currency, s = head.group(1).upper(), head.group(2)
    elif tail:
        s, currency = tail.group(1), tail.group(2).upper()
    currency = currency or default_currency
    if currency is None:
        raise InvalidValue("amount without a currency")
    return Money(amount=parse_number(s), currency=currency)


# ------------------------------------------------------------------ specs


@dataclass(frozen=True, slots=True)
class ValueSpec:
    """How to parse and check one leaf field."""

    kind: FieldKind
    rule: Rule
    choices: tuple[str, ...] = ()
    enum_type: type[StrEnum] | None = None
    shape: Shape | None = None

    def to_json(self) -> dict[str, Any]:
        """Everything needed to parse a value again later (stored with an extraction)."""
        rule = self.rule
        return {
            "kind": self.kind.value,
            "choices": list(self.choices),
            "shape": self.shape.name if self.shape else None,
            "max_length": rule.max_length,
            "minimum": None if rule.minimum is None else str(rule.minimum),
            "maximum": None if rule.maximum is None else str(rule.maximum),
            "date_order": rule.date_order,
            "default_currency": rule.default_currency,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ValueSpec:
        """Rebuild a spec stored by ``to_json``. Raises ``ValueError`` when it cannot."""
        kind = FieldKind(data["kind"])
        shape = None
        if kind is FieldKind.SHAPE:
            shape = shape_named(str(data.get("shape")))
            if shape is None:
                raise ValueError("unknown shape")
        rule = Rule(
            max_length=int(data.get("max_length", 500)),
            minimum=None if data.get("minimum") is None else Decimal(str(data["minimum"])),
            maximum=None if data.get("maximum") is None else Decimal(str(data["maximum"])),
            date_order=data.get("date_order", "DMY"),
            default_currency=data.get("default_currency"),
        )
        return cls(kind, rule, choices=tuple(str(c) for c in data.get("choices", ())), shape=shape)

    def describe(self) -> dict[str, Any]:
        """The part of the schema description the model sees for this field."""
        out: dict[str, Any] = {"type": self.kind.value}
        if self.choices:
            out["choices"] = list(self.choices)
        if self.shape is not None:
            out["format"] = self.shape.description
        if self.kind is FieldKind.MONEY:
            out["format"] = "ISO 4217 code then the amount, e.g. AED 1250.00"
        elif self.kind is FieldKind.DATE:
            out["format"] = "YYYY-MM-DD"
        elif self.kind is FieldKind.PERCENT:
            out["format"] = "a number in percent units, e.g. 5 for 5%"
        return out


def parse_value(spec: ValueSpec, raw: str) -> Any:
    """Parse ``raw`` as ``spec``'s type or raise ``InvalidValue``."""
    text = raw.strip()
    if not text:
        raise InvalidValue("empty")
    rule = spec.rule
    match spec.kind:
        case FieldKind.TEXT:
            if len(text) > rule.max_length:
                raise InvalidValue("too long")
            return text
        case FieldKind.INTEGER:
            number = parse_number(text)
            if number != number.to_integral_value():
                raise InvalidValue("not a whole number")
            _check_range(number, rule)
            return int(number)
        case FieldKind.DECIMAL:
            number = parse_number(text)
            _check_range(number, rule)
            return number
        case FieldKind.PERCENT:
            number = parse_number(re.sub(r"\s*(%|percent)$", "", text, flags=re.I))
            _check_range(number, rule)
            if not Decimal(0) <= number <= Decimal(100):
                raise InvalidValue("percent outside 0..100")
            return number
        case FieldKind.DATE:
            return parse_date(text, rule.date_order)
        case FieldKind.MONEY:
            money = parse_money(text, rule.default_currency)
            _check_range(money.amount, rule)
            return money
        case FieldKind.ENUM:
            key = norm_key(text)
            for choice in spec.choices:
                if norm_key(choice) == key:
                    return spec.enum_type(choice) if spec.enum_type else choice
            raise InvalidValue("not one of the allowed values")
        case FieldKind.SHAPE:
            if spec.shape is None:  # pragma: no cover - compile_schema guarantees it
                raise InvalidValue("no shape")
            value = spec.shape.normalize(ascii_digits(text))
            if not spec.shape.check(value):
                raise InvalidValue(f"not a valid {spec.shape.name}")
            return value
    raise InvalidValue("unsupported kind")  # pragma: no cover


def _numbers_in(quote: str) -> set[Decimal]:
    return {n.normalize() for n in find_numbers(quote)}


def supported_by_quote(spec: ValueSpec, value: Any, quote: str) -> bool:
    """True when ``value`` can be read from ``quote`` (the evidence backs the answer)."""
    match spec.kind:
        case FieldKind.TEXT:
            return norm_text(str(value)) in norm_text(quote)
        case FieldKind.INTEGER | FieldKind.DECIMAL | FieldKind.PERCENT:
            return abs(Decimal(value)).normalize() in _numbers_in(quote)
        case FieldKind.MONEY:
            return abs(value.amount).normalize() in _numbers_in(quote)
        case FieldKind.DATE:
            return value in find_dates(quote, spec.rule.date_order)
        case FieldKind.SHAPE:
            if spec.shape is None:  # pragma: no cover
                return False
            return str(value) in spec.shape.normalize(ascii_digits(quote))
        case FieldKind.ENUM:
            # Enumerations are an interpretation of the text (``Net 30`` -> ``net_30``),
            # so the located quote is the evidence; the value is checked by parse_value.
            return True
    return False  # pragma: no cover


def norm_text(text: str) -> str:
    """Comparison form of text: NFKC, ASCII digits, case folded, single spaces."""
    return " ".join(ascii_digits(text).casefold().split())
