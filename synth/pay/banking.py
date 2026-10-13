# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Synthetic banks, IBANs and TRNs.

* **Banks**: a small table of made-up banks. Their national bank codes were chosen
  outside the ranges real banks use where the format allows it (UAE codes 9xx, German
  bank codes starting 99, French bank codes starting 99, Saudi codes 9x, letter codes
  starting XQ or XZ). BICs follow the same pattern (``XQ..``, ``XZ..``). No real bank
  is named or coded on purpose.
* **IBANs** have the right length and structure for their country and valid ISO 13616
  check digits (mod 97), so a validator accepts them, but the account numbers are
  seeded random digits at synthetic banks: they cannot belong to anyone. French IBANs
  also carry a valid RIB key.
* **TRNs** (UAE VAT numbers) are 15 seeded random digits starting ``19``. They have
  the shape of a TRN and are not checked against, or taken from, any registry. A random
  number can never be proven to differ from every real TRN, which is why the prefix is
  fixed and the whole thing is documented as random.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Bank:
    key: str
    name: str
    country: str
    code: str  # national bank code as it sits in the BBAN
    bic: str


BANKS: Final[tuple[Bank, ...]] = (
    Bank("ae_sahil", "Sahil Commercial Bank (synthetic)", "AE", "917", "XQSHAEAD"),
    Bank("ae_noora", "Noora National Bank (synthetic)", "AE", "926", "XQNNAEAD"),
    Bank("ae_marjan", "Marjan Islamic Bank (synthetic)", "AE", "934", "XQMIAEAD"),
    Bank("ae_qasr", "Qasr Investment Bank (synthetic)", "AE", "948", "XQQIAEAA"),
    Bank("ae_rimal", "Rimal Bank of Commerce (synthetic)", "AE", "955", "XQRBAEAS"),
    Bank("ae_layan", "Layan Digital Bank (synthetic)", "AE", "963", "XQLDAEAD"),
    Bank("sa_dana", "Dana Saudi Bank (synthetic)", "SA", "97", "XQDSSARI"),
    Bank("bh_lulu", "Lulu Gulf Bank (synthetic)", "BH", "XQLG", "XQLGBHBM"),
    Bank("gb_thames", "Ashgrove Mutual Bank (synthetic)", "GB", "XQAM", "XQAMGB2L"),
    Bank("gb_kestrel", "Kestrel Trade Bank (synthetic)", "GB", "XZKT", "XZKTGB2L"),
    Bank("de_lindau", "Lindauer Handelsbank (synthetic)", "DE", "99070011", "XQLHDEFF"),
    Bank("fr_vigne", "Banque des Vignes (synthetic)", "FR", "99021", "XQBVFRPP"),
    Bank("nl_polder", "Polder Zakenbank (synthetic)", "NL", "XQPZ", "XQPZNL2A"),
)
BANK_BY_KEY: Final = {b.key: b for b in BANKS}

# BBAN length by country (ISO 13616 registry), for validation.
IBAN_LENGTHS: Final = {"AE": 23, "SA": 24, "BH": 22, "GB": 22, "DE": 22, "FR": 27, "NL": 18}


def _numeric(text: str) -> str:
    return "".join(str(int(c, 36)) if c.isalpha() else c for c in text.upper())


def check_digits(country: str, bban: str) -> str:
    remainder = int(_numeric(bban + country + "00")) % 97
    return f"{98 - remainder:02d}"


def iban_is_valid(iban: str) -> bool:
    """ISO 13616: known country length, alphanumeric, ``mod 97 == 1``."""
    text = iban.replace(" ", "").upper()
    if len(text) < 5 or not text.isalnum() or text[:2] not in IBAN_LENGTHS:
        return False
    if len(text) != IBAN_LENGTHS[text[:2]] or not text[2:4].isdigit():
        return False
    return int(_numeric(text[4:] + text[:4])) % 97 == 1


def _digits(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(string.digits) for _ in range(n))


def _rib_key(bank: str, branch: str, account: str) -> str:
    value = 89 * int(bank) + 15 * int(branch) + 3 * int(account)
    return f"{97 - value % 97:02d}"


def make_bban(bank: Bank, rng: random.Random) -> str:
    country = bank.country
    if country == "AE":
        return bank.code + _digits(rng, 16)
    if country == "SA":
        return bank.code + _digits(rng, 18)
    if country == "BH":
        return bank.code + _digits(rng, 14)
    if country == "GB":
        return bank.code + "99" + _digits(rng, 4) + _digits(rng, 8)
    if country == "DE":
        return bank.code + _digits(rng, 10)
    if country == "FR":
        branch, account = "0" + _digits(rng, 4), _digits(rng, 11)
        return bank.code + branch + account + _rib_key(bank.code, branch, account)
    if country == "NL":
        return bank.code + _digits(rng, 10)
    raise ValueError(f"no IBAN format for {country}")


def make_iban(bank: Bank, rng: random.Random) -> str:
    bban = make_bban(bank, rng)
    iban = bank.country + check_digits(bank.country, bban) + bban
    if not iban_is_valid(iban):  # pragma: no cover (a broken generator)
        raise RuntimeError(f"generated an invalid IBAN for {bank.key}")
    return iban


def format_iban(iban: str) -> str:
    """Print form: groups of four."""
    return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))


def make_trn(rng: random.Random) -> str:
    return "19" + _digits(rng, 13)


def trn_is_well_formed(trn: str) -> bool:
    return len(trn) == 15 and trn.isdigit()
