# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic terms: Arabic-Indic digits ruff mistakes for Latin look-alikes)
"""Early-payment discounts (brief 4.6.4): terms read from invoices and vendors, the
saving in Decimal at the currency's minor unit, the pay-by date, deterministic."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from pairly_pay.discounts import (
    DiscountTerms,
    early_payment,
    invoice_offer,
    offer_json,
    parse_terms,
)
from pairly_pay.models import PayInvoice

INVOICE_DATE = date(2026, 10, 1)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2/10 net 30", DiscountTerms(Decimal(2), 10, 30)),
        ("2/10, n/30", DiscountTerms(Decimal(2), 10, 30)),
        ("1.5/15 N 45", DiscountTerms(Decimal("1.5"), 15, 45)),
        ("Terms: 2% 10 days, net 30", DiscountTerms(Decimal(2), 10, 30)),
        ("2% discount if paid within 10 days", DiscountTerms(Decimal(2), 10, None)),
        ("2%/10", DiscountTerms(Decimal(2), 10, None)),
        ("خصم ٢٪ خلال ١٠ أيام، صافي ٣٠ يوماً", DiscountTerms(Decimal(2), 10, 30)),
        ("خصم 2.5% خلال 7 أيام", DiscountTerms(Decimal("2.5"), 7, None)),
        ("Net 30 / صافي 30 يوماً", None),
        ("Due on receipt", None),
        ("12/10/2026", None),  # a date, not a discount
        ("2/10", None),  # could be a date: needs a net period or a percent sign
        ("3/10 net 5", None),  # the net period must be longer
        ("60/10 net 30", None),  # not a plausible discount
        ("", None),
        (None, None),
    ],
)
def test_parse_terms(text: str | None, expected: DiscountTerms | None) -> None:
    assert parse_terms(text) == expected


def test_label() -> None:
    assert DiscountTerms(Decimal("2.0"), 10, 30).label == "2/10 net 30"
    assert DiscountTerms(Decimal("1.5"), 15, None).label == "1.5/15"


def test_saving_and_pay_by_in_minor_units() -> None:
    offer = early_payment(
        invoice_terms="2/10 net 30",
        vendor_terms=None,
        invoice_date=INVOICE_DATE,
        amount=Decimal("1234.5600"),
        currency="AED",
    )
    assert offer is not None
    assert offer.source == "invoice"
    assert offer.saving == Decimal("24.69")  # 24.6912, half up
    assert offer.discounted == Decimal("1209.87")
    assert offer.pay_by == date(2026, 10, 11)
    assert offer.net_due == date(2026, 10, 31)
    assert offer.annual_rate == Decimal("0.3724")
    assert offer.available(date(2026, 10, 11))
    assert not offer.available(date(2026, 10, 12))
    assert offer.days_left(date(2026, 10, 5)) == 6
    # Three-place currencies keep three places.
    kwd = early_payment(
        invoice_terms=None,
        vendor_terms="1/10 net 30",
        invoice_date=INVOICE_DATE,
        amount=Decimal("100.555"),
        currency="KWD",
    )
    assert kwd is not None
    assert kwd.source == "vendor"
    assert kwd.saving == Decimal("1.006")
    assert isinstance(kwd.saving, Decimal)


def test_invoice_terms_win_and_nothing_is_guessed() -> None:
    both = early_payment(
        invoice_terms="1/10 net 30",
        vendor_terms="3/10 net 30",
        invoice_date=INVOICE_DATE,
        amount=Decimal(1000),
        currency="AED",
    )
    assert both is not None
    assert (both.source, both.saving) == ("invoice", Decimal("10.00"))
    vendor = early_payment(
        invoice_terms="Net 30",
        vendor_terms="3/10 net 30",
        invoice_date=INVOICE_DATE,
        amount=Decimal(1000),
        currency="AED",
    )
    assert vendor is not None
    assert vendor.source == "vendor"
    common = {"invoice_terms": "2/10 net 30", "vendor_terms": None}
    assert early_payment(**common, invoice_date=None, amount=Decimal(1), currency="AED") is None
    assert early_payment(**common, invoice_date=INVOICE_DATE, amount=None, currency="AED") is None
    assert (
        early_payment(**common, invoice_date=INVOICE_DATE, amount=Decimal(0), currency="AED")
        is None
    )
    assert (
        early_payment(**common, invoice_date=INVOICE_DATE, amount=Decimal(1), currency=None) is None
    )
    # A saving that rounds to nothing is no offer.
    assert (
        early_payment(**common, invoice_date=INVOICE_DATE, amount=Decimal("0.20"), currency="AED")
        is None
    )


def _invoice(**kw: object) -> PayInvoice:
    base: dict[str, object] = {
        "id": uuid.UUID(int=1),
        "document_type": "invoice",
        "payment_terms": "2/10 net 30",
        "invoice_date": INVOICE_DATE,
        "total": Decimal("1000.00"),
        "approved_amount": Decimal("1000.00"),
        "credit_total": Decimal("200.00"),
        "currency": "AED",
        "approved_currency": "AED",
    }
    base.update(kw)
    return PayInvoice(**base)


def test_invoice_offer_after_credits_and_never_on_a_credit_note() -> None:
    offer = invoice_offer(_invoice(), None)
    assert offer is not None
    # 2 percent of what is due after the applied credit.
    assert offer.amount == Decimal("800.00")
    assert offer.saving == Decimal("16.00")
    assert invoice_offer(_invoice(document_type="credit_note"), "2/10 net 30") is None
    as_json = offer_json(offer, date(2026, 10, 12))
    assert as_json["saving"] == "16.00"
    assert as_json["pay_early"] == "784.00"
    assert as_json["available"] is False
    assert as_json["days_left"] == -1
    # Deterministic: the same inputs, the same offer.
    assert invoice_offer(_invoice(), None) == offer
