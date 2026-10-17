# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Statement readers and the matching rules, without a database (synthetic data)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from pairly_pay.models import StatementMatchKind
from pairly_pay.reconciliation import ItemFact, LineFact, match_lines, ref_key
from pairly_pay.statements import InvalidStatementError, parse_camt053, parse_csv

CAMT = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"
IBAN = "AE070331234567890123456"
DAY = dt.date(2026, 10, 21)
RUN = uuid.uuid4()


def _item(
    amount: str, keys: tuple[str, ...] = (), bidx: str | None = None, **kw: object
) -> ItemFact:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "run_id": RUN,
        "run_reference": "RUN-7K2QX9",
        "invoice_id": uuid.uuid4(),
        "amount": Decimal(amount),
        "currency": "AED",
        "keys": tuple(ref_key(k) for k in keys),
        "iban_bidx": bidx,
        "run_paid_count": 1,
    }
    values.update(kw)
    return ItemFact(**values)  # type: ignore[arg-type]


def _line(
    amount: str, reference: str | None = None, bidx: str | None = None, **kw: object
) -> LineFact:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "booking_date": DAY,
        "amount": Decimal(amount),
        "currency": "AED",
        "reference": reference,
        "iban_bidx": bidx,
    }
    values.update(kw)
    return LineFact(**values)  # type: ignore[arg-type]


# ------------------------------------------------------------------ matching


def test_reference_and_amount_match_one_item() -> None:
    item = _item("1050.00", keys=("E2E-0001-ABCD", "INV-2026-079"))
    line = _line("-1050.00", "pmt e2e 0001 abcd")
    result = match_lines([line], [item])
    (match,) = result.matches
    assert match.item_id == item.id
    assert match.invoice_id == item.invoice_id
    assert match.kind is StatementMatchKind.ITEM
    assert match.reasons == ("amount", "reference")


def test_iban_and_amount_match_and_the_amount_alone_never_does() -> None:
    item = _item("200.00", bidx="a" * 64)
    by_iban = _line("-200.00", None, bidx="a" * 64)
    alone = _line("-200.00", "salary", bidx="b" * 64)
    result = match_lines([alone, by_iban], [item])
    (match,) = result.matches
    assert match.line_id == by_iban.id
    assert match.reasons == ("amount", "iban")
    assert result.unmatched == {alone.id: "no_candidate"}


def test_the_amount_must_agree_to_the_minor_unit_and_currency() -> None:
    item = _item("200.00", keys=("INV-55555",))
    assert match_lines([_line("-200.01", "INV-55555")], [item]).matches == []
    usd = _line("-200.00", "INV-55555", currency="USD")
    assert match_lines([usd], [item]).matches == []
    assert match_lines([_line("-200.0", "INV55555")], [item]).matches  # same value


def test_two_equally_good_candidates_leave_the_line_to_a_person() -> None:
    one = _item("75.00", bidx="c" * 64)
    two = _item("75.00", bidx="c" * 64)
    line = _line("-75.00", bidx="c" * 64)
    result = match_lines([line], [one, two])
    assert result.matches == []
    assert result.unmatched == {line.id: "ambiguous"}
    # More agreement wins: the reference breaks the tie.
    three = _item("75.00", keys=("E2E-THREE",), bidx="c" * 64)
    best = match_lines([_line("-75.00", "E2E-THREE", bidx="c" * 64)], [one, three])
    assert best.matches[0].item_id == three.id


def test_each_item_explains_one_line_and_short_keys_never_match() -> None:
    item = _item("10.00", keys=("E2E-777",))
    first = _line("-10.00", "E2E-777")
    second = _line("-10.00", "E2E-777", booking_date=DAY + dt.timedelta(days=1))
    result = match_lines([second, first], [item])
    assert [m.line_id for m in result.matches] == [first.id]  # the earlier booking
    assert result.unmatched == {second.id: "no_candidate"}
    short = _item("10.00", keys=("A1",))
    assert match_lines([_line("-10.00", "A1 payment")], [short]).matches == []


def test_credits_are_not_matched() -> None:
    item = _item("10.00", keys=("E2E-CREDIT",))
    result = match_lines([_line("10.00", "E2E-CREDIT")], [item])
    assert result.matches == []
    assert result.unmatched == {}


def test_a_batch_debit_naming_the_run_matches_the_whole_run() -> None:
    a = _item("100.00", run_paid_count=2)
    b = _item("250.50", run_paid_count=2)
    line = _line("-350.50", "Payment run RUN-7K2QX9")
    (match,) = match_lines([line], [a, b]).matches
    assert match.kind is StatementMatchKind.RUN
    assert match.run_id == RUN
    assert match.item_id is None
    # Not while one of its paid items is explained elsewhere (or missing).
    assert match_lines([line], [a]).matches == []
    wrong_total = _line("-350.00", "RUN-7K2QX9")
    assert match_lines([wrong_total], [a, b]).matches == []


# ------------------------------------------------------------------ CSV


def test_a_valid_csv_reads_signed_amounts_at_the_currency_minor_unit() -> None:
    content = (
        "﻿counterparty_iban,date,amount,currency,reference\n"
        f"{IBAN},2026-10-21,-1050.00,AED,E2E-1\n"
        ",2026-10-22,12.345,KWD,\n"
        "\n"
    )
    parsed = parse_csv(content)
    first, second = parsed.lines
    assert first.amount == Decimal("-1050.00")
    assert first.counterparty_iban == IBAN
    assert first.booking_date == DAY
    assert second.amount == Decimal("12.345")
    assert second.reference is None
    assert second.counterparty_iban is None


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("date,amount,currency,reference\n2026-10-21,-1,AED,x\n", (1, "counterparty_iban")),
        ("date,amount,currency,reference,counterparty_iban,extra\n", (1, "extra")),
        ("date,amount,currency,reference,counterparty_iban\n", (0, None)),
        (
            "date,amount,currency,reference,counterparty_iban\n2026-10-21,-1.234,AED,x,\n",
            (2, "amount"),
        ),
        (
            'date,amount,currency,reference,counterparty_iban\n2026-10-21,"1,000",AED,x,\n',
            (2, "amount"),
        ),
        (
            "date,amount,currency,reference,counterparty_iban\n21/10/2026,-1,AED,x,\n",
            (2, "date"),
        ),
        (
            "date,amount,currency,reference,counterparty_iban\n2026-10-21,-1,AED,"
            + "r" * 141
            + ",\n",
            (2, "reference"),
        ),
    ],
)
def test_csv_problems_are_reported_with_their_row(
    content: str, expected: tuple[int, str | None]
) -> None:
    with pytest.raises(InvalidStatementError) as caught:
        parse_csv(content)
    assert expected in {(e.row, e.field) for e in caught.value.errors}


# ------------------------------------------------------------------ camt.053


def _camt(entries: str, namespace: str = CAMT) -> str:
    return (
        f'<Document xmlns="{namespace}"><BkToCstmrStmt><Stmt><Id>S-1</Id>'
        f"<Acct><Id><IBAN>{IBAN}</IBAN></Id></Acct>{entries}</Stmt></BkToCstmrStmt></Document>"
    )


def test_camt053_reads_booked_entries_and_itemised_batches() -> None:
    entries = (
        '<Ntry><Amt Ccy="AED">300.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>'
        "<BookgDt><DtTm>2026-10-21T09:00:00</DtTm></BookgDt><AcctSvcrRef>BNK-1</AcctSvcrRef>"
        "<NtryDtls>"
        "<TxDtls><Refs><EndToEndId>E2E-A</EndToEndId></Refs>"
        '<AmtDtls><TxAmt><Amt Ccy="AED">100.00</Amt></TxAmt></AmtDtls>'
        f"<RltdPties><CdtrAcct><Id><IBAN>{IBAN}</IBAN></Id></CdtrAcct></RltdPties></TxDtls>"
        "<TxDtls><Refs><EndToEndId>NOTPROVIDED</EndToEndId></Refs>"
        '<AmtDtls><TxAmt><Amt Ccy="AED">200.00</Amt></TxAmt></AmtDtls>'
        "<RmtInf><Ustrd>INV-2</Ustrd></RmtInf></TxDtls>"
        "</NtryDtls></Ntry>"
        '<Ntry><Amt Ccy="AED">5.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts>BOOK</Sts>'
        "<BookgDt><Dt>2026-10-22</Dt></BookgDt><AcctSvcrRef>BNK-2</AcctSvcrRef></Ntry>"
        '<Ntry><Amt Ccy="AED">9.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts>PDNG</Sts>'
        "<BookgDt><Dt>2026-10-22</Dt></BookgDt></Ntry>"
    )
    parsed = parse_camt053(_camt(entries))
    assert parsed.bank_statement_id == "S-1"
    assert parsed.account_masked == "AE** **** 3456"
    assert [(x.amount, x.reference) for x in parsed.lines] == [
        (Decimal("-100.00"), "E2E-A"),
        (Decimal("-200.00"), "INV-2"),
        (Decimal("5.00"), "BNK-2"),
    ]
    assert parsed.lines[0].counterparty_iban == IBAN
    assert parsed.lines[0].booking_date == DAY


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("<Document", "malformed_xml"),
        (_camt("", namespace="urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"), "not_camt053"),
        (_camt(""), "no_rows"),
        (
            '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]>'
            f'<Document xmlns="{CAMT}"><BkToCstmrStmt>&a;</BkToCstmrStmt></Document>',
            "malformed_xml",
        ),
        (
            _camt(
                '<Ntry><Amt Ccy="AED">-5</Amt><CdtDbtInd>DBIT</CdtDbtInd>'
                "<BookgDt><Dt>2026-10-22</Dt></BookgDt></Ntry>"
            ),
            "invalid_amount",
        ),
        (
            _camt(
                '<Ntry><Amt Ccy="AED">5</Amt><CdtDbtInd>XX</CdtDbtInd>'
                "<BookgDt><Dt>2026-10-22</Dt></BookgDt></Ntry>"
            ),
            "invalid_indicator",
        ),
    ],
)
def test_camt053_problems_refuse_the_file(content: str, code: str) -> None:
    with pytest.raises(InvalidStatementError) as caught:
        parse_camt053(content)
    assert code in {e.code for e in caught.value.errors}
