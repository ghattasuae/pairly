# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The Xero, QuickBooks Online and Zoho Books journal files: exact header rows, the same
balanced entries as the generic export, and a refusal (no file) when the journal does
not balance, an account has no mapping or a value does not fit the format (no
database)."""

from __future__ import annotations

import csv
import dataclasses
import io
import json
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from pairly_pay.api.journal import PayJournalExportIn, _mapping_audit, export_filename
from pairly_pay.journal import (
    AP_CONTROL,
    BANK,
    DISCOUNT_RECEIVED,
    SUSPENSE,
    VAT_INPUT,
    CodedLine,
    InvoiceFact,
    JournalLine,
    PaymentFact,
    build_journal,
    to_csv,
)
from pairly_pay.journal_formats import (
    ExportRefusedError,
    MissingAccountMappingError,
    PayJournalFormat,
    UnbalancedJournalError,
    UnrepresentableValueError,
    render,
)

D = Decimal
DAY = date(2026, 9, 15)
DOC = uuid.UUID("00000000-0000-4000-8000-000000000001")
PLACEHOLDERS = {
    AP_CONTROL: "800",
    VAT_INPUT: "820",
    BANK: "090",
    DISCOUNT_RECEIVED: "270",
    SUSPENSE: "999",
}
ACCOUNTS = {**PLACEHOLDERS, "6100": "6100", "6200": "6200"}
VENDOR_FORMATS = (PayJournalFormat.XERO, PayJournalFormat.QUICKBOOKS, PayJournalFormat.ZOHO_BOOKS)
HEADERS = {
    PayJournalFormat.XERO: (
        "Narration,Date,Description,AccountCode,TaxRate,Amount,"
        "TrackingName1,TrackingOption1,TrackingName2,TrackingOption2"
    ),
    PayJournalFormat.QUICKBOOKS: (
        "JournalNo,JournalDate,Currency,Memo,AccountName,Debits,Credits,"
        "Description,Name,Location,Class"
    ),
    PayJournalFormat.ZOHO_BOOKS: (
        "Journal Date,Journal Number,Reference Number,Notes,Journal Type,Currency,"
        "Account,Description,Contact Name,Debit,Credit"
    ),
}


def _invoice(
    vendor: str | None = "Synthetic Supplies LLC",
    currency: str = "AED",
    total: str = "1050.00",
    vat: str = "50.00",
    lines: tuple[CodedLine, ...] | None = None,
) -> InvoiceFact:
    return InvoiceFact(
        id=uuid.uuid4(),
        reference="INV-ABC123",
        document_id=DOC,
        vendor_name=vendor,
        currency=currency,
        day=DAY,
        total=D(total),
        vat=D(vat),
        subtotal=None,
        lines=lines
        if lines is not None
        else (
            CodedLine("6100", "CC-IT", D("600.00"), D(5), D("30.00")),
            CodedLine("6200", None, D("400.00"), D(5), D("20.00")),
        ),
    )


def _payment(vendor: str | None = "Synthetic Supplies LLC") -> PaymentFact:
    return PaymentFact(
        uuid.uuid4(), "INV-ABC123", DOC, vendor, "AED", DAY, D("1029.00"), D("21.00")
    )


def _journal(vendor: str | None = "Synthetic Supplies LLC") -> list[JournalLine]:
    return build_journal([_invoice(vendor)], [_payment(vendor)])


def _rows(content: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content.decode("utf-8"))))


def _file(fmt: PayJournalFormat, lines: list[JournalLine], **kw: object) -> list[list[str]]:
    accounts = kw.pop("accounts", ACCOUNTS)
    return _rows(
        render(lines, fmt, accounts=accounts, number_prefix="PAIRLY-2026-09-", **kw)  # type: ignore[arg-type]
    )


def _debit_credit(fmt: PayJournalFormat, row: list[str]) -> tuple[Decimal, Decimal]:
    if fmt == PayJournalFormat.XERO:
        amount = D(row[5])
        return (amount, D(0)) if amount > 0 else (D(0), -amount)
    debit, credit = (row[5], row[6]) if fmt == PayJournalFormat.QUICKBOOKS else (row[9], row[10])
    return D(debit or "0"), D(credit or "0")


# ------------------------------------------------------------------ the files


@pytest.mark.parametrize("fmt", VENDOR_FORMATS)
def test_the_header_row_is_the_vendor_template(fmt: PayJournalFormat) -> None:
    content = render(_journal(), fmt, accounts=ACCOUNTS)
    assert content.decode("utf-8").split("\r\n")[0] == HEADERS[fmt]
    assert render([], fmt, accounts={}) == (HEADERS[fmt] + "\r\n").encode("utf-8")


@pytest.mark.parametrize("fmt", VENDOR_FORMATS)
def test_every_entry_balances_with_the_same_lines_as_the_generic_file(
    fmt: PayJournalFormat,
) -> None:
    lines = _journal()
    rows = _file(fmt, lines)[1:]
    assert len(rows) == len(lines) == len(_rows(to_csv(lines))) - 1
    debit: dict[int, Decimal] = defaultdict(Decimal)
    credit: dict[int, Decimal] = defaultdict(Decimal)
    for x, row in zip(lines, rows, strict=True):
        d, c = _debit_credit(fmt, row)
        assert (d, c) == (x.debit, x.credit)
        debit[x.entry] += d
        credit[x.entry] += c
    assert debit == credit
    assert set(debit) == {1, 2}


def test_xero_rows() -> None:
    rows = _file(PayJournalFormat.XERO, _journal(), tax_rate="No VAT")
    assert rows[1] == [
        "AP_INVOICE INV-ABC123", "15/09/2026", "INV-ABC123 Synthetic Supplies LLC", "6100",
        "No VAT", "600.00", "Cost Centre", "CC-IT", "", "",
    ]  # fmt: skip
    # Later lines of the journal leave Narration and Date blank; credits are negative.
    assert [r[:2] for r in rows[2:5]] == [["", ""], ["", ""], ["", ""]]
    assert [r[3] for r in rows[1:5]] == ["6100", "6200", "820", "800"]
    assert [r[5] for r in rows[1:5]] == ["600.00", "400.00", "50.00", "-1050.00"]
    assert rows[2][6:8] == ["", ""]  # no cost centre, no tracking
    # The payment is a new journal.
    assert rows[5][:2] == ["AP_PAYMENT INV-ABC123", "15/09/2026"]
    assert [r[5] for r in rows[5:]] == ["1050.00", "-1029.00", "-21.00"]


def test_quickbooks_rows() -> None:
    rows = _file(PayJournalFormat.QUICKBOOKS, _journal())
    assert rows[1] == [
        "PAIRLY-2026-09-1", "15/09/2026", "AED", "AP_INVOICE INV-ABC123", "6100", "600.00",
        "", "INV-ABC123 Synthetic Supplies LLC", "Synthetic Supplies LLC", "", "CC-IT",
    ]  # fmt: skip
    assert rows[4][4:7] == ["800", "", "1050.00"]
    assert {r[0] for r in rows[5:]} == {"PAIRLY-2026-09-2"}


def test_zoho_books_rows() -> None:
    rows = _file(PayJournalFormat.ZOHO_BOOKS, _journal())
    assert rows[1] == [
        "15/09/2026", "PAIRLY-2026-09-1", "INV-ABC123", "AP_INVOICE INV-ABC123", "Both",
        "AED", "6100", "INV-ABC123 Synthetic Supplies LLC", "Synthetic Supplies LLC",
        "600.00", "",
    ]  # fmt: skip
    assert rows[4][6:] == [
        "800", "INV-ABC123 Synthetic Supplies LLC", "Synthetic Supplies LLC", "", "1050.00"
    ]  # fmt: skip


@pytest.mark.parametrize("fmt", VENDOR_FORMATS)
def test_an_arabic_vendor_name_is_written_as_utf8(fmt: PayJournalFormat) -> None:
    vendor = "مؤسسة التوريدات الاصطناعية"
    content = render(_journal(vendor), fmt, accounts=ACCOUNTS)
    text = content.decode("utf-8")
    assert vendor in text
    assert all(
        vendor in row[2] or vendor in row[7] or vendor in row[8] for row in _rows(content)[1:]
    )


@pytest.mark.parametrize("fmt", VENDOR_FORMATS)
def test_a_formula_in_a_vendor_or_an_account_is_neutralised(fmt: PayJournalFormat) -> None:
    accounts = {**ACCOUNTS, AP_CONTROL: "=HYPERLINK(1)"}
    payment = PaymentFact(uuid.uuid4(), "-2+3", DOC, "@SUM(A1)", "AED", DAY, D("10.00"), D(0))
    for vendor in ("=cmd|' /C calc'!A0", "+1", "-1", "@x", "\tTab", "\rCR"):
        lines = build_journal([_invoice(vendor)], [payment])
        rows = _file(fmt, lines, accounts=accounts)[1:]
        for row in rows:
            for cell in row:
                if cell and not _is_number(cell):
                    assert not cell.startswith(("=", "+", "-", "@", "\t", "\r")), (fmt, cell)
        assert any(cell == "'=HYPERLINK(1)" for row in rows for cell in row)


def _is_number(cell: str) -> bool:
    try:
        D(cell)
    except ArithmeticError:
        return False
    return True


# ------------------------------------------------------------------ refusals


@pytest.mark.parametrize("fmt", [*VENDOR_FORMATS, PayJournalFormat.GENERIC])
def test_an_unbalanced_entry_is_refused(fmt: PayJournalFormat) -> None:
    lines = _journal()
    lines[0] = dataclasses.replace(lines[0], debit=lines[0].debit + D("0.01"))
    with pytest.raises(UnbalancedJournalError):
        render(lines, fmt, accounts=ACCOUNTS if fmt != PayJournalFormat.GENERIC else None)
    # Balanced, but with negative amounts: refused too.
    first = _journal()[0]
    negative = [
        dataclasses.replace(first, debit=D("-5.00"), credit=D(0)),
        dataclasses.replace(first, debit=D(0), credit=D("-5.00")),
    ]
    with pytest.raises(UnbalancedJournalError):
        render(negative, fmt, accounts=ACCOUNTS if fmt != PayJournalFormat.GENERIC else None)


@pytest.mark.parametrize("fmt", VENDOR_FORMATS)
def test_a_line_without_an_account_mapping_is_refused(fmt: PayJournalFormat) -> None:
    accounts = {k: v for k, v in ACCOUNTS.items() if k not in (BANK, "6200")}
    with pytest.raises(MissingAccountMappingError) as err:
        render(_journal(), fmt, accounts=accounts)
    assert err.value.accounts == ("6200", BANK)
    assert err.value.code == "journal_account_unmapped"
    with pytest.raises(MissingAccountMappingError):
        render(_journal(), fmt, accounts={})


@pytest.mark.parametrize(
    ("fmt", "refused"),
    [
        (PayJournalFormat.XERO, True),
        (PayJournalFormat.QUICKBOOKS, True),
        (PayJournalFormat.ZOHO_BOOKS, False),  # Zoho Books keeps the KWD third place
    ],
)
def test_an_amount_with_three_decimals(fmt: PayJournalFormat, refused: bool) -> None:
    lines = build_journal(
        [
            _invoice(
                currency="KWD",
                total="105.556",
                vat="5.026",
                lines=(CodedLine("6100", None, D("100.530"), D(5), D("5.026")),),
            )
        ],
        [],
    )
    if refused:
        with pytest.raises(UnrepresentableValueError):
            render(lines, fmt, accounts=ACCOUNTS)
        # Three places that are a whole number of fils at two places are fine.
        two = [dataclasses.replace(x, debit=D("100.530"), credit=D(0)) for x in lines[:1]]
        two.append(dataclasses.replace(lines[2], debit=D(0), credit=D("100.530")))
        assert len(_file(fmt, two)) == 3
    else:
        rows = _file(fmt, lines)
        assert [(r[9], r[10]) for r in rows[1:]] == [
            ("100.530", ""), ("5.026", ""), ("", "105.556")
        ]  # fmt: skip


def test_xero_refuses_a_journal_in_several_currencies() -> None:
    lines = build_journal([_invoice(), _invoice(currency="USD")], [])
    with pytest.raises(UnrepresentableValueError):
        render(lines, PayJournalFormat.XERO, accounts=ACCOUNTS)
    # QuickBooks and Zoho Books carry the currency on every line.
    for fmt in (PayJournalFormat.QUICKBOOKS, PayJournalFormat.ZOHO_BOOKS):
        assert {
            r[2 if fmt == PayJournalFormat.QUICKBOOKS else 5] for r in _file(fmt, lines)[1:]
        } == {
            "AED",
            "USD",
        }


@pytest.mark.parametrize("fmt", VENDOR_FORMATS)
def test_a_nul_character_is_refused(fmt: PayJournalFormat) -> None:
    with pytest.raises(UnrepresentableValueError):
        render(_journal("Bad\x00Name"), fmt, accounts=ACCOUNTS)


def test_every_refusal_is_one_typed_error_with_a_code() -> None:
    for cls in (UnbalancedJournalError, MissingAccountMappingError, UnrepresentableValueError):
        assert issubclass(cls, ExportRefusedError)
    codes = {UnbalancedJournalError.code, MissingAccountMappingError.code}
    assert codes | {UnrepresentableValueError.code} == {
        "journal_unbalanced",
        "journal_account_unmapped",
        "journal_value_unrepresentable",
    }


def test_the_generic_format_is_the_unchanged_generic_file() -> None:
    lines = _journal("=cmd")
    assert render(lines, PayJournalFormat.GENERIC) == to_csv(lines)


# ------------------------------------------------------------------ the request


def test_the_request_defaults_to_generic_and_checks_its_options() -> None:
    body = PayJournalExportIn.model_validate({"period": "2026-09"})
    assert body.format == PayJournalFormat.GENERIC
    assert body.accounts is None
    ok = PayJournalExportIn.model_validate(
        {"period": "2026-09", "format": "xero", "accounts": PLACEHOLDERS, "tax_rate": "No VAT"}
    )
    assert ok.format == PayJournalFormat.XERO
    bad: list[dict[str, object]] = [
        {"period": "2026-09", "format": "sage"},
        {"period": "2026-09", "accounts": {"BANK": "090"}},  # generic takes none
        {"period": "2026-09", "format": "zoho_books", "tax_rate": "No VAT"},
        {"period": "2026-09", "format": "xero", "accounts": {"BANK": "0\n90"}},
        {"period": "2026-09", "format": "xero", "accounts": {"BANK": ""}},
        {"period": "2026-09", "format": "xero", "accounts": {"X" * 33: "1"}},
        {"period": "2026-09", "format": "xero", "accounts": {str(i): "1" for i in range(501)}},
    ]
    for payload in bad:
        with pytest.raises(ValidationError):
            PayJournalExportIn.model_validate(payload)


def test_the_file_name_names_the_format() -> None:
    names: dict[PayJournalFormat, str] = {
        f: export_filename("2026-09", f) for f in PayJournalFormat
    }
    assert names == {
        PayJournalFormat.GENERIC: "journal-2026-09.csv",
        PayJournalFormat.XERO: "journal-2026-09-xero.csv",
        PayJournalFormat.QUICKBOOKS: "journal-2026-09-quickbooks.csv",
        PayJournalFormat.ZOHO_BOOKS: "journal-2026-09-zoho_books.csv",
    }
    assert all(len(f.value) <= 16 for f in PayJournalFormat)  # pay_journal_exports.format


def test_the_audit_keeps_the_placeholder_matches_and_a_digest_of_all() -> None:
    assert _mapping_audit({}) == {}
    worst = {**dict.fromkeys(PLACEHOLDERS, "ح" * 150), **{f"G{i}": "ح" * 150 for i in range(495)}}
    kept = _mapping_audit(dict(sorted(worst.items())))
    assert kept["accounts"] == dict.fromkeys(PLACEHOLDERS, "ح" * 150)
    assert kept["accounts_count"] == 500
    assert len(kept["accounts_sha256"]) == 64
    # Well under the 16 KiB audit payload limit, even with escaped Arabic.
    assert len(json.dumps(kept)) < 8 * 1024
