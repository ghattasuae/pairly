# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The journal export in the import layouts of Xero, QuickBooks Online and Zoho Books.

Each format is a rendering of the same ``JournalLine`` entries as the generic export
(``pairly_pay.journal``); nothing is recomputed. Before a single row is written the
renderer checks the whole journal and refuses the file (``ExportRefusedError``) when:

* an entry's debits and credits differ, or an amount is negative
  (``UnbalancedJournalError``);
* a line's account has no mapping to an account of the accounting system
  (``MissingAccountMappingError``; the placeholders ``AP_CONTROL``, ``VAT_INPUT``,
  ``BANK``, ``DISCOUNT_RECEIVED`` and ``SUSPENSE`` always need one);
* a value cannot be written in the format: an amount with more decimals than it takes,
  a journal in several currencies where the format has no currency column, or a NUL
  character in a text cell (``UnrepresentableValueError``).

So a file is either complete and balanced, or not made at all. Text cells go through
``safe_cell``, the guard of the generic export, so a vendor name such as ``=cmd`` is
never run by a spreadsheet. Dates are written day first (``dd/mm/yyyy``), the setting of
UAE and UK organisations; amounts have a dot for decimals and no thousands separator.

The files were checked against the vendors' published templates and help pages, not
imported into a live vendor account.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from pairly_pay.checks.settings import minor_unit
from pairly_pay.journal import JournalLine, safe_cell, to_csv


class PayJournalFormat(StrEnum):
    GENERIC = "generic"
    XERO = "xero"
    QUICKBOOKS = "quickbooks"
    ZOHO_BOOKS = "zoho_books"


XERO_HEADER: Final = (
    "Narration",
    "Date",
    "Description",
    "AccountCode",
    "TaxRate",
    "Amount",
    "TrackingName1",
    "TrackingOption1",
    "TrackingName2",
    "TrackingOption2",
)
QUICKBOOKS_HEADER: Final = (
    "JournalNo",
    "JournalDate",
    "Currency",
    "Memo",
    "AccountName",
    "Debits",
    "Credits",
    "Description",
    "Name",
    "Location",
    "Class",
)
ZOHO_BOOKS_HEADER: Final = (
    "Journal Date",
    "Journal Number",
    "Reference Number",
    "Notes",
    "Journal Type",
    "Currency",
    "Account",
    "Description",
    "Contact Name",
    "Debit",
    "Credit",
)
# The Xero tracking category the cost centre goes to (it must exist in the organisation).
XERO_COST_CENTRE_TRACKING: Final = "Cost Centre"
# Zoho Books journal type: "Both" posts to the accrual and the cash basis.
ZOHO_JOURNAL_TYPE: Final = "Both"
DATE_FORMAT: Final = "%d/%m/%Y"


class ExportRefusedError(ValueError):
    """The journal cannot be written in the format; no file is made."""

    code: str = "journal_export_refused"


class UnbalancedJournalError(ExportRefusedError):
    code = "journal_unbalanced"


class MissingAccountMappingError(ExportRefusedError):
    code = "journal_account_unmapped"

    def __init__(self, accounts: Sequence[str]) -> None:
        self.accounts = tuple(accounts)
        super().__init__("no account mapping for " + ", ".join(self.accounts))


class UnrepresentableValueError(ExportRefusedError):
    code = "journal_value_unrepresentable"


@dataclass(frozen=True, slots=True)
class _Line:
    """One journal line, checked, with its mapped account and its amounts as text."""

    src: JournalLine
    account: str
    debit: str
    credit: str
    amount: str  # debit positive, credit negative


def check_balanced(lines: Sequence[JournalLine]) -> None:
    """Every entry's debits equal its credits, and no amount is negative."""
    debit: dict[int, Decimal] = defaultdict(Decimal)
    credit: dict[int, Decimal] = defaultdict(Decimal)
    for x in lines:
        if x.debit < 0 or x.credit < 0:
            raise UnbalancedJournalError(f"entry {x.entry} has a negative amount")
        debit[x.entry] += x.debit
        credit[x.entry] += x.credit
    for entry in debit:
        if debit[entry] != credit[entry]:
            raise UnbalancedJournalError(
                f"entry {entry} debits {debit[entry]} credits {credit[entry]}"
            )


def _amount(value: Decimal, places: int) -> str:
    step = Decimal(1).scaleb(-places)
    if not value.is_finite() or value != value.quantize(step):
        raise UnrepresentableValueError(f"{value} has more than {places} decimal places")
    return format(value.quantize(step), "f")


def _text(value: str | None) -> str:
    text = value or ""
    if "\x00" in text:
        raise UnrepresentableValueError("a text cell has a NUL character")
    return safe_cell(text)


def _prepare(
    lines: Sequence[JournalLine],
    accounts: Mapping[str, str],
    places: Callable[[str], int],
) -> list[_Line]:
    check_balanced(lines)
    missing = sorted({x.account for x in lines if not accounts.get(x.account)})
    if missing:
        raise MissingAccountMappingError(missing)
    out: list[_Line] = []
    for x in lines:
        n = places(x.currency)
        out.append(
            _Line(
                src=x,
                account=_text(accounts[x.account]),
                debit=_amount(x.debit, n) if x.debit or not x.credit else "",
                credit=_amount(x.credit, n) if x.credit else "",
                amount=_amount(x.debit - x.credit, n),
            )
        )
    return out


def _narration(x: JournalLine) -> str:
    return _text(f"{x.journal} {x.reference}")


def _description(x: JournalLine) -> str:
    return _text(" ".join(p for p in (x.reference, x.vendor) if p))


def _write(header: Sequence[str], rows: Sequence[Sequence[str]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def to_xero(
    lines: Sequence[JournalLine], accounts: Mapping[str, str], *, tax_rate: str | None = None
) -> bytes:
    """Xero manual journal import (Accounting, Manual journals, Import).

    Source: Xero Central, "Add, import and post manual journals",
    https://central.xero.com/s/article/Add-import-and-post-manual-journals-UK
    (tried 2026-10-19: the page renders in the browser only and could not be fetched).
    Layout from memory, verify before relying on it. Cross-checked on 2026-10-19 against
    a third-party copy of the article (https://clickentry.co.uk/add-import-and-post-manual-journals/)
    and the Xero ManualJournals API (debits positive, credits negative, two tracking
    categories at most).

    One row per line: Narration, Date, Description, AccountCode, TaxRate, Amount and two
    tracking pairs. Narration and Date are on the first row of each journal only; rows
    with both left blank belong to the journal above. Amount is the debit as a positive
    number and the credit as a negative one, at most 2 decimal places (Xero amounts),
    no thousands separator. AccountCode is the mapped account code. TaxRate is
    ``tax_rate`` (the name of a Xero tax rate, the same on every line) or blank:
    Pairly already posts the VAT on its own line, so choose a rate with no tax. The cost
    centre goes to the tracking category ``Cost Centre``. A manual journal has no
    currency column: it is in the organisation's base currency, so a journal in more
    than one currency is refused.
    """
    currencies = {x.currency for x in lines}
    if len(currencies) > 1:
        raise UnrepresentableValueError(
            "a Xero manual journal has no currency: export one currency at a time"
        )
    rate = _text(tax_rate)
    rows: list[list[str]] = []
    seen: set[int] = set()
    for p in _prepare(lines, accounts, lambda _c: 2):
        x = p.src
        first = x.entry not in seen
        seen.add(x.entry)
        centre = _text(x.cost_centre)
        rows.append(
            [
                _narration(x) if first else "",
                x.day.strftime(DATE_FORMAT) if first else "",
                _description(x),
                p.account,
                rate,
                p.amount,
                XERO_COST_CENTRE_TRACKING if centre else "",
                centre,
                "",
                "",
            ]
        )
    return _write(XERO_HEADER, rows)


def to_quickbooks(
    lines: Sequence[JournalLine], accounts: Mapping[str, str], *, number_prefix: str = ""
) -> bytes:
    """QuickBooks Online journal entry import (Settings, Import data, Journal entries).

    Source: QuickBooks Online help, "Import journal entries",
    https://quickbooks.intuit.com/learn-support/en-us/help-article/import-export-data-files/import-journal-entries-quickbooks-online/L4tQBwbs7_US_en_US
    (read 2026-10-19). The page names the required fields (journal number, journal date,
    account name, description, debits, credits), the Name column (a vendor for Accounts
    Payable lines), class and location tracking, and says sub accounts are written
    ``Parent:Sub``. It does not print the sample file's header row or its date format:
    the header below (JournalNo, JournalDate, Currency, Memo, AccountName, Debits,
    Credits, Description, Name, Location, Class) is the sample file's as we know it,
    verify it against the sample file of your company. QuickBooks maps the columns on
    import, so a renamed header is matched by hand.

    Lines of one entry share JournalNo (``number_prefix`` and the entry number) and
    JournalDate (``dd/mm/yyyy``, a company set to day first). AccountName is the mapped
    account name. Debits and Credits have at most 2 decimal places, no thousands
    separator, the other side blank. Name is the vendor (it must exist in QuickBooks),
    Class the cost centre. Location is left blank.
    """
    rows = [
        [
            _text(f"{number_prefix}{p.src.entry}"),
            p.src.day.strftime(DATE_FORMAT),
            p.src.currency,
            _narration(p.src),
            p.account,
            p.debit,
            p.credit,
            _description(p.src),
            _text(p.src.vendor),
            "",
            _text(p.src.cost_centre),
        ]
        for p in _prepare(lines, accounts, lambda _c: 2)
    ]
    return _write(QUICKBOOKS_HEADER, rows)


def _currency_places(currency: str) -> int:
    return -int(minor_unit(currency).as_tuple().exponent)


def to_zoho_books(
    lines: Sequence[JournalLine], accounts: Mapping[str, str], *, number_prefix: str = ""
) -> bytes:
    """Zoho Books manual journal import (Accountant, Manual Journals, Import Journals).

    Source: Zoho Books help, "Manual Journals",
    https://www.zoho.com/us/books/help/accountant/manual-journal.html (read 2026-10-19),
    and the Zoho Books API, Journals, https://www.zoho.com/books/api/v3/journals/ (read
    2026-10-19: journal type ``Cash`` or ``Both``, debit or credit per line). The help
    page says to download the sample file from the import screen and map the fields; it
    does not print the column names. Layout from memory, verify before relying on it:
    Journal Date, Journal Number, Reference Number, Notes, Journal Type, Currency,
    Account, Description, Contact Name, Debit, Credit.

    Lines of one entry share the journal number (``number_prefix`` and the entry
    number), date (``dd/mm/yyyy``), reference, notes, type (``Both``) and currency.
    Account is the mapped account name, Contact Name the vendor. Debit and Credit have
    the currency's decimal places (3 for KWD, BHD and OMR, else 2), no thousands
    separator, the other side blank. The tax columns are left out: Pairly posts the VAT
    on its own line, a tax on the expense line would count it twice. Cost centres
    (reporting tags in Zoho Books) are not carried.
    """
    rows = [
        [
            p.src.day.strftime(DATE_FORMAT),
            _text(f"{number_prefix}{p.src.entry}"),
            _text(p.src.reference),
            _narration(p.src),
            ZOHO_JOURNAL_TYPE,
            p.src.currency,
            p.account,
            _description(p.src),
            _text(p.src.vendor),
            p.debit,
            p.credit,
        ]
        for p in _prepare(lines, accounts, _currency_places)
    ]
    return _write(ZOHO_BOOKS_HEADER, rows)


def render(
    lines: Sequence[JournalLine],
    fmt: PayJournalFormat,
    *,
    accounts: Mapping[str, str] | None = None,
    tax_rate: str | None = None,
    number_prefix: str = "",
) -> bytes:
    """The journal as a file in ``fmt``; raises ``ExportRefusedError`` instead of a
    partial or unbalanced file."""
    mapped = accounts or {}
    match fmt:
        case PayJournalFormat.GENERIC:
            check_balanced(lines)
            return to_csv(lines)
        case PayJournalFormat.XERO:
            return to_xero(lines, mapped, tax_rate=tax_rate)
        case PayJournalFormat.QUICKBOOKS:
            return to_quickbooks(lines, mapped, number_prefix=number_prefix)
        case PayJournalFormat.ZOHO_BOOKS:
            return to_zoho_books(lines, mapped, number_prefix=number_prefix)
