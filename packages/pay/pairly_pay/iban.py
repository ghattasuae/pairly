# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""IBAN validation (ISO 13616) and bank code extraction. Pure functions, no I/O.

``validate_iban(text)`` returns an ``IbanCheck``: the normalised IBAN (spaces and dashes
removed, upper case) and, when it is valid, its country, check digits, BBAN and the
national bank code. When it is not, ``error`` says why, in this order:

* ``format``: not ``CC`` + two digits + letters and digits;
* ``country``: a country with no IBAN in the registry (``COUNTRIES``);
* ``length``: the wrong length for that country;
* ``structure``: the BBAN does not match the country's pattern (digits where the
  registry says digits), checked for the countries in ``BBAN_PATTERNS``;
* ``checksum``: the ISO 7064 mod 97-10 check fails (``mod 97 != 1``).

``COUNTRIES`` holds the total IBAN length and where the bank code sits in the BBAN
(``bank_start``, ``bank_end``) per country of the SWIFT IBAN registry. A bank code range
of ``(0, 0)`` means the registry has no separate bank code we rely on.

``validate_bic`` checks a SWIFT/BIC code's shape (8 or 11 characters) and returns its
country, so a caller can flag a BIC whose country differs from the IBAN's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

IbanError = Literal["format", "country", "length", "structure", "checksum"]


@dataclass(frozen=True, slots=True)
class CountryFormat:
    length: int
    bank_start: int
    bank_end: int


def _c(length: int, bank_start: int = 0, bank_end: int = 0) -> CountryFormat:
    return CountryFormat(length, bank_start, bank_end)


# Total IBAN length and bank code position inside the BBAN, per the SWIFT IBAN registry.
COUNTRIES: Final[dict[str, CountryFormat]] = {
    "AD": _c(24, 0, 4),
    "AE": _c(23, 0, 3),
    "AL": _c(28, 0, 3),
    "AT": _c(20, 0, 5),
    "AZ": _c(28, 0, 4),
    "BA": _c(20, 0, 3),
    "BE": _c(16, 0, 3),
    "BG": _c(22, 0, 4),
    "BH": _c(22, 0, 4),
    "BR": _c(29, 0, 8),
    "BY": _c(28, 0, 4),
    "CH": _c(21, 0, 5),
    "CR": _c(22, 0, 4),
    "CY": _c(28, 0, 3),
    "CZ": _c(24, 0, 4),
    "DE": _c(22, 0, 8),
    "DK": _c(18, 0, 4),
    "DO": _c(28, 0, 4),
    "EE": _c(20, 0, 2),
    "EG": _c(29, 0, 4),
    "ES": _c(24, 0, 4),
    "FI": _c(18, 0, 3),
    "FO": _c(18, 0, 4),
    "FR": _c(27, 0, 5),
    "GB": _c(22, 0, 4),
    "GE": _c(22, 0, 2),
    "GI": _c(23, 0, 4),
    "GL": _c(18, 0, 4),
    "GR": _c(27, 0, 3),
    "GT": _c(28, 0, 4),
    "HR": _c(21, 0, 7),
    "HU": _c(28, 0, 3),
    "IE": _c(22, 0, 4),
    "IL": _c(23, 0, 3),
    "IQ": _c(23, 0, 4),
    "IS": _c(26, 0, 2),
    "IT": _c(27, 1, 6),
    "JO": _c(30, 0, 4),
    "KW": _c(30, 0, 4),
    "KZ": _c(20, 0, 3),
    "LB": _c(28, 0, 4),
    "LC": _c(32, 0, 4),
    "LI": _c(21, 0, 5),
    "LT": _c(20, 0, 5),
    "LU": _c(20, 0, 3),
    "LV": _c(21, 0, 4),
    "LY": _c(25, 0, 3),
    "MC": _c(27, 0, 5),
    "MD": _c(24, 0, 2),
    "ME": _c(22, 0, 3),
    "MK": _c(19, 0, 3),
    "MR": _c(27, 0, 5),
    "MT": _c(31, 0, 4),
    "MU": _c(30, 0, 6),
    "NL": _c(18, 0, 4),
    "NO": _c(15, 0, 4),
    "OM": _c(23, 0, 3),
    "PK": _c(24, 0, 4),
    "PL": _c(28, 0, 3),
    "PS": _c(29, 0, 4),
    "PT": _c(25, 0, 4),
    "QA": _c(29, 0, 4),
    "RO": _c(24, 0, 4),
    "RS": _c(22, 0, 3),
    "SA": _c(24, 0, 2),
    "SC": _c(31, 0, 6),
    "SD": _c(18, 0, 2),
    "SE": _c(24, 0, 3),
    "SI": _c(19, 0, 2),
    "SK": _c(24, 0, 4),
    "SM": _c(27, 1, 6),
    "ST": _c(25, 0, 4),
    "SV": _c(28, 0, 4),
    "TL": _c(23, 0, 3),
    "TN": _c(24, 0, 2),
    "TR": _c(26, 0, 5),
    "UA": _c(29, 0, 6),
    "VA": _c(22, 0, 3),
    "VG": _c(24, 0, 4),
    "XK": _c(20, 0, 2),
}

# BBAN shapes for the countries Pairly's customers pay most (GCC, UK and the EU members
# in the synthetic data). Other countries get the length and checksum checks only.
BBAN_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "AE": re.compile(r"^\d{19}$"),
    "SA": re.compile(r"^\d{2}[A-Z0-9]{18}$"),
    "BH": re.compile(r"^[A-Z]{4}[A-Z0-9]{14}$"),
    "QA": re.compile(r"^[A-Z]{4}[A-Z0-9]{21}$"),
    "KW": re.compile(r"^[A-Z]{4}[A-Z0-9]{22}$"),
    "OM": re.compile(r"^\d{3}[A-Z0-9]{16}$"),
    "JO": re.compile(r"^[A-Z]{4}\d{4}[A-Z0-9]{18}$"),
    "EG": re.compile(r"^\d{25}$"),
    "GB": re.compile(r"^[A-Z]{4}\d{14}$"),
    "DE": re.compile(r"^\d{18}$"),
    "FR": re.compile(r"^\d{10}[A-Z0-9]{11}\d{2}$"),
    "NL": re.compile(r"^[A-Z]{4}\d{10}$"),
}

_SHAPE: Final = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{1,30}$")
_STRIP: Final = re.compile(r"[\s\-\u00a0\u200b-\u200f\u2060\ufeff]")
_BIC: Final = re.compile(r"^([A-Z]{4})([A-Z]{2})([A-Z0-9]{2})([A-Z0-9]{3})?$")
MAX_INPUT: Final = 64


@dataclass(frozen=True, slots=True)
class IbanCheck:
    iban: str
    error: IbanError | None = None
    country: str | None = None
    check_digits: str | None = None
    bban: str | None = None
    bank_code: str | None = None

    @property
    def valid(self) -> bool:
        return self.error is None


def normalize_iban(text: str) -> str:
    """Upper case without spaces, dashes or invisible characters; an ``IBAN`` prefix
    (as printed on invoices) is dropped."""
    value = _STRIP.sub("", text or "").upper()
    return value.removeprefix("IBAN:").removeprefix("IBAN")


def _numeric(text: str) -> str:
    return "".join(str(ord(c) - 55) if c.isalpha() else c for c in text)


def checksum_holds(iban: str) -> bool:
    """ISO 7064 mod 97-10 over the rearranged IBAN (``mod 97 == 1``)."""
    return int(_numeric(iban[4:] + iban[:4])) % 97 == 1


def validate_iban(text: str) -> IbanCheck:
    if not isinstance(text, str) or len(text) > MAX_INPUT:
        return IbanCheck(iban="", error="format")
    iban = normalize_iban(text)
    if not _SHAPE.fullmatch(iban):
        return IbanCheck(iban=iban, error="format")
    country = iban[:2]
    fmt = COUNTRIES.get(country)
    if fmt is None:
        return IbanCheck(iban=iban, error="country", country=country)
    if len(iban) != fmt.length:
        return IbanCheck(iban=iban, error="length", country=country)
    bban = iban[4:]
    pattern = BBAN_PATTERNS.get(country)
    if pattern is not None and not pattern.fullmatch(bban):
        return IbanCheck(iban=iban, error="structure", country=country)
    if not checksum_holds(iban):
        return IbanCheck(iban=iban, error="checksum", country=country)
    bank_code = bban[fmt.bank_start : fmt.bank_end] or None
    return IbanCheck(
        iban=iban, country=country, check_digits=iban[2:4], bban=bban, bank_code=bank_code
    )


def mask_iban(iban: str) -> str:
    """For lists and messages: the country and the last four characters only."""
    value = normalize_iban(iban)
    if len(value) < 8:
        return "****"
    return f"{value[:2]}** **** {value[-4:]}"


def format_iban(iban: str) -> str:
    """Print form: groups of four."""
    value = normalize_iban(iban)
    return " ".join(value[i : i + 4] for i in range(0, len(value), 4))


@dataclass(frozen=True, slots=True)
class BicCheck:
    bic: str
    valid: bool
    country: str | None = None


def validate_bic(text: str) -> BicCheck:
    value = _STRIP.sub("", text or "").upper()[:16]
    match = _BIC.fullmatch(value)
    if match is None:
        return BicCheck(bic=value, valid=False)
    return BicCheck(bic=value, valid=True, country=match.group(2))
