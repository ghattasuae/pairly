# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Bank statement readers for reconciliation: a strict CSV and a simple camt.053.

**CSV**: a header with exactly the columns ``date, amount, currency, reference,
counterparty_iban`` (any order, no others), one booked line per row. ``date`` is ISO
(``2026-10-20``); ``amount`` is a plain signed decimal (a payment out is negative, at
most the currency's minor unit: 2 places, 3 for BHD, KWD, OMR and the other three-place
currencies; no exponent, no thousands separator, never zero); ``currency`` is three
capital letters; ``reference`` (at most 140 characters) and ``counterparty_iban`` (a
valid IBAN) may be empty. The PO importer's CSV rules apply too (UTF-8, at most 1 MB
and 5000 rows, no cell a spreadsheet would run as a formula), and every problem is
reported with its row; a file with any problem imports nothing.

**camt.053** (ISO 20022 bank to customer statement, any ``camt.053.001.xx``): read with
``defusedxml`` (no DTD, no entities, nothing fetched) under the same size cap. Each
booked entry (``Ntry`` with ``Sts`` BOOK, or no status) becomes a line: amount and
currency from ``Amt``, the sign from ``CdtDbtInd`` (DBIT is negative), the date from
``BookgDt`` (``Dt`` or ``DtTm``), the reference from the end-to-end id and the
unstructured remittance of its transaction details (and the bank's reference), and the
counterparty IBAN from the creditor's account for a debit, the debtor's for a credit.
An entry with several transaction details that each carry an amount (a batch the bank
itemised) becomes one line per transaction. Pending entries are skipped.

Nothing here touches the database.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final
from xml.etree.ElementTree import Element

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from pairly_core.errors import BadRequestError
from pairly_pay.checks.settings import THREE_PLACE_CURRENCIES
from pairly_pay.common import clean_text
from pairly_pay.iban import mask_iban, validate_iban
from pairly_pay.purchase_orders import MAX_CSV_BYTES, MAX_CSV_ROWS, MAX_ERRORS, RowError, read_csv

CSV_COLUMNS: Final = ("date", "amount", "currency", "reference", "counterparty_iban")
CSV_REQUIRED: Final = frozenset({"date", "amount", "currency"})
MAX_REFERENCE: Final = 140
MAX_AMOUNT: Final = Decimal("1000000000000")
CAMT_PREFIX: Final = "urn:iso:std:iso:20022:tech:xsd:camt.053."

_AMOUNT: Final = re.compile(r"^-?\d{1,13}(?:\.\d{1,3})?$")
_DATE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY: Final = re.compile(r"^[A-Z]{3}$")


class InvalidStatementError(BadRequestError):
    """The statement is not valid; ``errors`` lists every problem (row 1 is the CSV
    header; for camt.053 the row is the entry's position, 0 for the whole file)."""

    code = "invalid_statement"
    title = "Invalid bank statement"

    def __init__(self, errors: list[RowError]) -> None:
        listed = errors[:MAX_ERRORS]
        super().__init__(
            "the statement has problems: "
            + ", ".join(f"{e.row}:{e.field or '-'}:{e.code}" for e in listed[:10])
        )
        self.errors = listed


@dataclass(frozen=True, slots=True)
class StatementLineInput:
    line_no: int
    booking_date: dt.date
    amount: Decimal
    currency: str
    reference: str | None
    counterparty_iban: str | None


@dataclass(slots=True)
class StatementInput:
    format: str
    lines: list[StatementLineInput] = field(default_factory=list)
    bank_statement_id: str | None = None
    account_masked: str | None = None


def places(currency: str) -> int:
    return 3 if currency in THREE_PLACE_CURRENCIES else 2


def parse_amount(text: str, currency: str) -> Decimal | None:
    """A plain signed decimal at most at the currency's minor unit, never zero."""
    value = text.strip()
    if not _AMOUNT.fullmatch(value):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:  # pragma: no cover - the pattern already refused it
        return None
    if number == 0 or abs(number) > MAX_AMOUNT:
        return None
    exponent = number.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > places(currency):
        return None
    return number


def parse_date(text: str) -> dt.date | None:
    value = text.strip()
    if not _DATE.fullmatch(value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def checked_iban(text: str | None) -> tuple[str | None, bool]:
    """The normalised IBAN (``None`` when empty) and whether it is valid."""
    if not text or not text.strip():
        return None, True
    result = validate_iban(text)
    return (result.iban, True) if result.valid else (None, False)


def masked(iban: str | None) -> str | None:
    return mask_iban(iban) if iban else None


# ------------------------------------------------------------------ CSV


def parse_csv(content: str | bytes) -> StatementInput:
    parsed = read_csv(content, CSV_COLUMNS, CSV_REQUIRED)
    errors = list(parsed.errors)
    if not errors and parsed.rows:
        header = set(parsed.rows[0][1])
        errors += [
            RowError(1, name, "missing_column") for name in CSV_COLUMNS if name not in header
        ]
    out = StatementInput(format="csv")
    if errors and any(e.row <= 1 for e in errors):
        raise InvalidStatementError(errors)
    for number, row in parsed.rows:
        currency = row["currency"]
        if not _CURRENCY.fullmatch(currency):
            errors.append(RowError(number, "currency", "invalid_currency"))
            currency = "XXX"
        amount = parse_amount(row["amount"], currency)
        if amount is None:
            errors.append(RowError(number, "amount", "invalid_amount"))
        booked = parse_date(row["date"])
        if booked is None:
            errors.append(RowError(number, "date", "invalid_date"))
        reference = row.get("reference") or ""
        if len(reference) > MAX_REFERENCE:
            errors.append(RowError(number, "reference", "too_long"))
        iban, valid = checked_iban(row.get("counterparty_iban"))
        if not valid:
            errors.append(RowError(number, "counterparty_iban", "invalid_iban"))
        if amount is not None and booked is not None:
            out.lines.append(
                StatementLineInput(
                    line_no=number,
                    booking_date=booked,
                    amount=amount,
                    currency=currency,
                    reference=clean_text(reference, MAX_REFERENCE) or None,
                    counterparty_iban=iban,
                )
            )
    if errors:
        # One problem per cell: a required value that is missing is not also invalid.
        unique: dict[tuple[int, str | None], RowError] = {}
        for error in errors:
            unique.setdefault((error.row, error.field), error)
        raise InvalidStatementError(list(unique.values()))
    return out


# ------------------------------------------------------------------ camt.053


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: Element | None, *path: str) -> Element | None:
    """The first descendant along ``path`` of local names (namespace ignored)."""
    current = node
    for name in path:
        if current is None:
            return None
        current = next((c for c in current if _local(c.tag) == name), None)
    return current


def _children(node: Element | None, name: str) -> list[Element]:
    return [] if node is None else [c for c in node if _local(c.tag) == name]


def _text(node: Element | None, *path: str) -> str:
    found = _child(node, *path)
    return (found.text or "").strip() if found is not None else ""


def _amount_of(node: Element | None, row: int, errors: list[RowError]) -> tuple[Decimal, str]:
    currency = (node.get("Ccy") or "").strip() if node is not None else ""
    if not _CURRENCY.fullmatch(currency):
        errors.append(RowError(row, "currency", "invalid_currency"))
        return Decimal(0), "XXX"
    amount = parse_amount((node.text or "") if node is not None else "", currency)
    if amount is None or amount < 0:
        errors.append(RowError(row, "amount", "invalid_amount"))
        return Decimal(0), currency
    return amount, currency


def _reference(tx: Element | None, entry: Element) -> str | None:
    parts: list[str] = []
    end_to_end = _text(tx, "Refs", "EndToEndId")
    if end_to_end and end_to_end.upper() != "NOTPROVIDED":
        parts.append(end_to_end)
    remittance = _text(tx, "RmtInf", "Ustrd")
    if remittance:
        parts.append(remittance)
    bank_ref = _text(entry, "AcctSvcrRef")
    if bank_ref and not parts:
        parts.append(bank_ref)
    text = clean_text(" ".join(dict.fromkeys(parts)), MAX_REFERENCE)
    return text or None


def _counterparty(tx: Element | None, debit: bool) -> str:
    party = "CdtrAcct" if debit else "DbtrAcct"
    return _text(tx, "RltdPties", party, "Id", "IBAN")


def _tx_amount(tx: Element) -> Element | None:
    found = _child(tx, "AmtDtls", "TxAmt", "Amt")
    return found if found is not None else _child(tx, "Amt")


def parse_camt053(content: str | bytes) -> StatementInput:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if len(raw) > MAX_CSV_BYTES:
        raise InvalidStatementError([RowError(0, None, "too_large")])
    try:
        root = SafeET.fromstring(raw)
    except (DefusedXmlException, SafeET.ParseError, ValueError):
        raise InvalidStatementError([RowError(0, None, "malformed_xml")]) from None
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if _local(root.tag) != "Document" or not namespace.startswith(CAMT_PREFIX):
        raise InvalidStatementError([RowError(0, None, "not_camt053")])
    statements = _children(_child(root, "BkToCstmrStmt"), "Stmt")
    if len(statements) != 1:
        raise InvalidStatementError([RowError(0, None, "one_statement_expected")])
    stmt = statements[0]
    out = StatementInput(format="camt053")
    out.bank_statement_id = clean_text(_text(stmt, "Id"), 64) or None
    account, _valid = checked_iban(_text(stmt, "Acct", "Id", "IBAN"))
    out.account_masked = masked(account)
    errors: list[RowError] = []
    entries = _children(stmt, "Ntry")
    if not entries:
        errors.append(RowError(0, None, "no_rows"))
    number = 0
    for position, entry in enumerate(entries, start=1):
        status = _text(entry, "Sts", "Cd") or _text(entry, "Sts")
        if status and status.upper() != "BOOK":
            continue
        indicator = _text(entry, "CdtDbtInd").upper()
        if indicator not in {"CRDT", "DBIT"}:
            errors.append(RowError(position, "CdtDbtInd", "invalid_indicator"))
            continue
        debit = indicator == "DBIT"
        when = _text(entry, "BookgDt", "Dt") or _text(entry, "BookgDt", "DtTm")[:10]
        booked = parse_date(when)
        if booked is None:
            errors.append(RowError(position, "BookgDt", "invalid_date"))
            continue
        details = _children(_child(entry, "NtryDtls"), "TxDtls")
        itemised = len(details) > 1 and all(_tx_amount(t) is not None for t in details)
        parts: list[tuple[Element | None, Element | None]]
        if itemised:
            parts = [(t, _tx_amount(t)) for t in details]
        else:
            parts = [(details[0] if details else None, _child(entry, "Amt"))]
        for tx, amount_node in parts:
            amount, currency = _amount_of(amount_node, position, errors)
            iban, valid = checked_iban(_counterparty(tx, debit))
            if not valid:
                errors.append(RowError(position, "IBAN", "invalid_iban"))
            if amount == 0:
                continue
            number += 1
            out.lines.append(
                StatementLineInput(
                    line_no=number,
                    booking_date=booked,
                    amount=-amount if debit else amount,
                    currency=currency,
                    reference=_reference(tx, entry),
                    counterparty_iban=iban,
                )
            )
    if number > MAX_CSV_ROWS:
        errors.append(RowError(0, None, "too_many_rows"))
    if not errors and not out.lines:
        errors.append(RowError(0, None, "no_rows"))
    if errors:
        raise InvalidStatementError(errors)
    return out


def parse_statement(fmt: str, content: str | bytes) -> StatementInput:
    if fmt == "csv":
        return parse_csv(content)
    if fmt == "camt053":
        return parse_camt053(content)
    raise InvalidStatementError([RowError(0, None, "unknown_format")])
