# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The generic journal: balanced entries at each currency's precision (no database)."""

from __future__ import annotations

import csv
import io
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal

from pairly_pay.journal import (
    AP_CONTROL,
    BANK,
    CREDIT_JOURNAL,
    DISCOUNT_RECEIVED,
    SUSPENSE,
    VAT_INPUT,
    CodedLine,
    InvoiceFact,
    JournalLine,
    PaymentFact,
    build_journal,
    tax_code,
    to_csv,
)

DAY = date(2026, 9, 15)
D = Decimal


def _invoice(total: str, vat: str, lines: list[CodedLine], currency: str = "AED") -> InvoiceFact:
    return InvoiceFact(
        id=uuid.uuid4(),
        reference="INV-ABC123",
        document_id=uuid.uuid4(),
        vendor_name="Synthetic Supplies LLC",
        currency=currency,
        day=DAY,
        total=D(total),
        vat=D(vat),
        subtotal=None,
        lines=tuple(lines),
    )


def _coded(
    gl: str | None,
    net: str | None,
    rate: str | None = "5",
    vat: str | None = None,
    cc: str | None = "CC-IT",
) -> CodedLine:
    return CodedLine(
        gl, cc, D(net) if net else None, D(rate) if rate else None, D(vat) if vat else None
    )


def _balanced(lines: list[JournalLine]) -> None:
    debit: dict[int, Decimal] = defaultdict(Decimal)
    credit: dict[int, Decimal] = defaultdict(Decimal)
    for x in lines:
        debit[x.entry] += x.debit
        credit[x.entry] += x.credit
    assert debit == credit


def test_a_coded_invoice_debits_each_line_and_vat_and_credits_payables() -> None:
    inv = _invoice(
        "1050.00",
        "50.00",
        [_coded("6100", "600.00", vat="30.00"), _coded("6200", "400.00", vat="20.00", cc="CC-OPS")],
    )
    lines = build_journal([inv], [])
    _balanced(lines)
    assert [(x.account, x.cost_centre, x.debit, x.credit, x.tax_code) for x in lines] == [
        ("6100", "CC-IT", D("600.00"), D(0), "VAT5"),
        ("6200", "CC-OPS", D("400.00"), D(0), "VAT5"),
        (VAT_INPUT, None, D("50.00"), D(0), "VAT"),
        (AP_CONTROL, None, D(0), D("1050.00"), None),
    ]
    assert {x.reference for x in lines} == {"INV-ABC123"}
    assert {x.document_id for x in lines} == {inv.document_id}


def test_lines_that_do_not_add_up_fall_back_to_one_balanced_expense_line() -> None:
    uncoded = _invoice("1050.00", "50.00", [_coded("6100", "600.00"), _coded(None, "400.00")])
    one_code = _invoice("1050.00", "50.00", [_coded("6100", "600.00"), _coded("6100", "300.00")])
    lines = build_journal([uncoded, one_code], [])
    _balanced(lines)
    first = [x for x in lines if x.entry == 1]
    second = [x for x in lines if x.entry == 2]
    assert first[0].account == SUSPENSE
    assert first[0].debit == D("1000.00")
    assert second[0].account == "6100"  # every coded line shares it
    assert second[0].cost_centre == "CC-IT"
    assert second[0].debit == D("1000.00")


def test_three_place_currencies_keep_their_precision_and_balance() -> None:
    inv = _invoice("105.5555", "5.0255", [_coded("6100", "100.53", vat="5.026")], currency="KWD")
    lines = build_journal([inv], [])
    _balanced(lines)
    assert [x.debit + x.credit for x in lines] == [D("100.530"), D("5.026"), D("105.556")]
    out = list(csv.DictReader(io.StringIO(to_csv(lines).decode("utf-8"))))
    assert [r["debit"] for r in out] == ["100.530", "5.026", "0.000"]
    assert out[-1]["credit"] == "105.556"


def test_payments_settle_payables_with_bank_and_discount() -> None:
    pay = PaymentFact(
        id=uuid.uuid4(),
        reference="INV-ABC123",
        document_id=uuid.uuid4(),
        vendor_name="=cmd",
        currency="AED",
        day=DAY,
        amount=D("1029.00"),
        discount=D("21.00"),
    )
    plain = PaymentFact(pay.id, "INV-2", pay.document_id, None, "BHD", DAY, D("10.1234"), D(0))
    lines = build_journal([], [pay, plain])
    _balanced(lines)
    first = [x for x in lines if x.reference == "INV-ABC123"]
    assert [(x.account, x.debit, x.credit) for x in first] == [
        (AP_CONTROL, D("1050.00"), D(0)),
        (BANK, D(0), D("1029.00")),
        (DISCOUNT_RECEIVED, D(0), D("21.00")),
    ]
    second = [x for x in lines if x.reference == "INV-2"]
    assert [x.debit + x.credit for x in second] == [D("10.123"), D("10.123")]
    rows = list(csv.DictReader(io.StringIO(to_csv(lines).decode("utf-8"))))
    assert {r["vendor"] for r in rows if r["reference"] == "INV-ABC123"} == {"'=cmd"}
    assert list(rows[0]) == [
        "entry", "date", "journal", "account_code", "cost_centre", "debit", "credit",
        "currency", "tax_code", "vat_amount", "vendor", "reference", "document_id",
    ]  # fmt: skip


def test_a_credit_note_reverses_its_invoice_entry_and_balances() -> None:
    credit = _invoice(
        "210.00",
        "10.00",
        [_coded("6100", "120.00", vat="6.00"), _coded("6200", "80.00", vat="4.00", cc="CC-OPS")],
    )
    lines = build_journal([], [], [credit])
    _balanced(lines)
    assert {x.journal for x in lines} == {CREDIT_JOURNAL}
    got = [(x.account, x.cost_centre, x.debit, x.credit, x.tax_code, x.vat_amount) for x in lines]
    assert got == [
        ("6100", "CC-IT", D(0), D("120.00"), "VAT5", D("-6.00")),
        ("6200", "CC-OPS", D(0), D("80.00"), "VAT5", D("-4.00")),
        (VAT_INPUT, None, D(0), D("10.00"), "VAT", D("-10.00")),
        (AP_CONTROL, None, D("210.00"), D(0), None, None),
    ]  # fmt: skip
    rows = list(csv.DictReader(io.StringIO(to_csv(lines).decode("utf-8"))))
    assert [r["vat_amount"] for r in rows] == ["-6.00", "-4.00", "-10.00", ""]


def test_an_uncoded_credit_note_falls_back_to_suspense_reversed() -> None:
    credit = _invoice("105.00", "5.00", [_coded(None, "100.00")])
    lines = build_journal([], [], [credit])
    _balanced(lines)
    assert [(x.account, x.debit, x.credit) for x in lines] == [
        (SUSPENSE, D(0), D("100.00")),
        (VAT_INPUT, D(0), D("5.00")),
        (AP_CONTROL, D("105.00"), D(0)),
    ]


def test_invoice_credit_and_payment_clear_the_payable() -> None:
    inv = _invoice("1050.00", "50.00", [_coded("6100", "1000.00", vat="50.00")])
    credit = _invoice("210.00", "10.00", [_coded("6100", "200.00", vat="10.00")])
    pay = PaymentFact(
        uuid.uuid4(), "INV-ABC123", inv.document_id, None, "AED", DAY, D("840.00"), D(0)
    )
    lines = build_journal([inv], [pay], [credit])
    _balanced(lines)
    # Entries in order: invoice, credit note, payment.
    assert [x.journal for x in lines if x.account == AP_CONTROL] == [
        "AP_INVOICE",
        CREDIT_JOURNAL,
        "AP_PAYMENT",
    ]
    payable = [x.credit - x.debit for x in lines if x.account == AP_CONTROL]
    assert sum(payable, D(0)) == 0
    expense = sum((x.debit - x.credit for x in lines if x.account == "6100"), D(0))
    assert expense == D("800.00")
    vat = sum((x.debit - x.credit for x in lines if x.account == VAT_INPUT), D(0))
    assert vat == D("40.00")


def test_tax_codes() -> None:
    assert tax_code(D("5.00")) == "VAT5"
    assert tax_code(D(0)) == "VAT0"
    assert tax_code(D("12.5")) == "VAT12.5"
    assert tax_code(None) is None
