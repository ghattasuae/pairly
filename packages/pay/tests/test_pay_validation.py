# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pay validation without a database: IBAN (ISO 13616), SWIFT/BIC, TRN and the TRN
verification hook, vendor name matching, and the vendor master of the synthetic data
(``synth/pay``) passing every check."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pairly_pay.dedupe import SIMILAR, name_key, name_similarity
from pairly_pay.iban import (
    COUNTRIES,
    checksum_holds,
    format_iban,
    mask_iban,
    normalize_iban,
    validate_bic,
    validate_iban,
)
from pairly_pay.trn import (
    DEFAULT_VERIFIER,
    NotVerifiedTrnVerifier,
    TrnVerifier,
    check_trn_format,
    normalize_trn,
)
from pairly_pay.vendors import (
    BankInput,
    InvalidBankDetailsError,
    InvalidTrnError,
    check_bank,
    check_trn,
)
from synth.pay.banking import BANKS, check_digits, make_iban

VENDORS = Path(__file__).resolve().parents[3] / "synth" / "pay" / "manifests" / "vendors.json"


def _vendors() -> list[dict[str, object]]:
    data = json.loads(VENDORS.read_text(encoding="utf-8"))
    return [*data["vendors"], *data["unknown_vendors"]]


# ------------------------------------------------------------------ IBAN


def test_every_synthetic_vendor_iban_is_valid_with_its_bank_code() -> None:
    by_bic = {b.bic: b for b in BANKS}
    vendors = _vendors()
    assert len(vendors) >= 30
    for vendor in vendors:
        bank = vendor["bank"]
        assert isinstance(bank, dict)
        check = validate_iban(str(bank["iban"]))
        assert check.valid, (vendor["id"], check.error)
        assert check.country == bank["country"]
        synthetic = by_bic[str(bank["bic"])]
        assert check.bank_code == synthetic.code, vendor["id"]
        assert validate_bic(str(bank["bic"])).valid


@pytest.mark.parametrize("bank", BANKS, ids=lambda b: b.key)
def test_generated_ibans_of_every_synthetic_bank(bank: object) -> None:
    import random

    rng = random.Random(7)  # noqa: S311 (synthetic test data)
    for _ in range(20):
        iban = make_iban(bank, rng)  # type: ignore[arg-type]
        check = validate_iban(format_iban(iban))
        assert check.valid
        assert check.bank_code == bank.code  # type: ignore[attr-defined]


def test_iban_normalisation() -> None:
    iban = "AE07 0331 2345 6789 0123 456"
    assert normalize_iban(iban) == "AE070331234567890123456"
    assert normalize_iban("iban: ae07-0331-2345-6789-0123-456") == "AE070331234567890123456"
    # Invisible characters pasted from an email are dropped, not kept.
    assert normalize_iban("AE07\u200b0331\u00a02345") == "AE0703312345"


def test_known_published_example_ibans() -> None:
    # The registry's own example IBANs (public, fictitious accounts).
    examples = {
        "AE070331234567890123456": ("AE", "033"),
        "GB29NWBK60161331926819": ("GB", "NWBK"),
        "DE89370400440532013000": ("DE", "37040044"),
        "SA0380000000608010167519": ("SA", "80"),
        "NL91ABNA0417164300": ("NL", "ABNA"),
        "FR1420041010050500013M02606": ("FR", "20041"),
    }
    for iban, (country, bank) in examples.items():
        check = validate_iban(iban)
        assert check.valid, (iban, check.error)
        assert (check.country, check.bank_code) == (country, bank)


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("", "format"),
        ("not an iban", "format"),
        ("AE0X0331234567890123456", "format"),
        ("ZZ070331234567890123456", "country"),
        ("AE07033123456789012345", "length"),
        ("AE0703312345678901234567", "length"),
        ("AE07033123456789012345A", "structure"),
        ("GB29NWBK6016133192681X", "structure"),
        ("AE080331234567890123456", "checksum"),
        ("AE070331234567890123465", "checksum"),
        ("X" * 100, "format"),
    ],
)
def test_invalid_ibans(text: str, error: str) -> None:
    check = validate_iban(text)
    assert not check.valid
    assert check.error == error


def test_one_changed_digit_always_fails_the_checksum() -> None:
    iban = "AE070331234567890123456"
    for i in range(4, len(iban)):
        for d in "0123456789":
            if d != iban[i]:
                assert not checksum_holds(iban[:i] + d + iban[i + 1 :])


def test_country_table_matches_the_generator() -> None:
    from synth.pay.banking import IBAN_LENGTHS

    for country, length in IBAN_LENGTHS.items():
        assert COUNTRIES[country].length == length
    assert check_digits("AE", "0331234567890123456") == "07"


def test_masking_shows_country_and_last_four_only() -> None:
    assert mask_iban("AE070331234567890123456") == "AE** **** 3456"
    assert "0331" not in mask_iban("AE070331234567890123456")


def test_bic_shape() -> None:
    assert validate_bic("XQSHAEAD").country == "AE"
    assert validate_bic("xqsh ae ad xxx").valid
    for bad in ("XQSH", "1QSHAEAD", "XQSHAEADXX", "XQSH-AE"):
        assert not validate_bic(bad).valid


def test_check_bank_details() -> None:
    ok = check_bank(
        BankInput(
            iban="AE07 0331 2345 6789 0123 456",
            account_name="  Seltus   IT Solutions LLC ",
            bank_name="Sahil Commercial Bank (synthetic)",
            swift="XQSHAEAD",
        )
    )
    assert ok.iban == "AE070331234567890123456"
    assert ok.account_name == "Seltus IT Solutions LLC"
    assert ok.bank_code == "033"
    assert ok.last4 == "3456"
    assert ok.warnings == ()
    foreign = check_bank(
        BankInput(iban="AE070331234567890123456", account_name="Seltus", swift="XQAMGB2L")
    )
    assert foreign.warnings == ("swift_country_differs",)
    with pytest.raises(InvalidBankDetailsError, match="iban_checksum"):
        check_bank(BankInput(iban="AE080331234567890123456", account_name="Seltus"))
    with pytest.raises(InvalidBankDetailsError, match="account_name_missing"):
        check_bank(BankInput(iban="AE070331234567890123456", account_name=" "))
    with pytest.raises(InvalidBankDetailsError, match="swift_format"):
        check_bank(BankInput(iban="AE070331234567890123456", account_name="Seltus", swift="X"))


# ------------------------------------------------------------------ TRN


def test_every_synthetic_trn_is_well_formed() -> None:
    trns = [str(v["trn"]) for v in _vendors() if v["trn"]]
    assert len(trns) >= 25
    for trn in trns:
        assert check_trn_format(trn).valid
        assert check_trn(trn) == trn


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("19842740184998", "length"),
        ("1984274018499812", "length"),
        ("19842740184998A", "not_digits"),
        ("TRN198427401849", "not_digits"),
    ],
)
def test_invalid_trns(text: str, error: str) -> None:
    assert check_trn_format(text).error == error


def test_trn_normalisation() -> None:
    assert normalize_trn("198-4274-0184-9981") == "198427401849981"
    assert normalize_trn("198 4274 0184 9981") == "198427401849981"
    # Arabic-Indic digits typed on an Arabic keyboard.
    arabic = "".join(chr(0x0660 + int(d)) for d in "198427401849981")
    assert check_trn_format(arabic).trn == "198427401849981"
    assert check_trn(None) is None
    assert check_trn("  ") is None
    with pytest.raises(InvalidTrnError, match="trn_length"):
        check_trn("1234")


def test_trn_verifier_never_says_verified() -> None:
    """The hook exists; with no official source connected nothing reads as verified."""
    assert isinstance(DEFAULT_VERIFIER, TrnVerifier)
    assert isinstance(NotVerifiedTrnVerifier(), TrnVerifier)
    result = asyncio.run(DEFAULT_VERIFIER.verify("198427401849981"))
    assert result.status == "not_verified"
    assert result.reason == "no_official_source_configured"
    assert asyncio.run(DEFAULT_VERIFIER.verify("123")).status == "invalid"


# ------------------------------------------------------------------ names


def test_name_keys_drop_legal_forms_and_punctuation() -> None:
    assert name_key("Seltus IT Solutions L.L.C.") == name_key("SELTUS IT SOLUTIONS LLC")
    assert name_key("Seltus IT Solutions FZE") == "seltus it solutions"
    assert name_key("Ashgrove Mutual Ltd.") == "ashgrove mutual"
    arabic = "سلتس لحلول ذ.م.م"
    assert name_key(arabic) == "سلتس لحلول"


@pytest.mark.parametrize(
    ("a", "b", "similar"),
    [
        ("Seltus IT Solutions LLC", "Seltus I.T. Solutions", True),
        ("Seltus IT Solutions LLC", "Seltis IT Solutions LLC", True),
        ("Solutions Seltus IT", "Seltus IT Solutions", True),
        ("Seltus IT Solutions LLC", "Delta IT Solutions LLC", False),
        ("Noora Catering", "Rimal Logistics", False),
    ],
)
def test_similar_names(a: str, b: str, similar: bool) -> None:
    score = name_similarity(name_key(a), name_key(b))
    assert (score >= SIMILAR) is similar, score


def test_synthetic_vendor_names_are_not_duplicates_of_each_other() -> None:
    keys = [name_key(str(v["legal_name"])) for v in _vendors()]
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            assert name_similarity(a, b) < SIMILAR, (a, b)
