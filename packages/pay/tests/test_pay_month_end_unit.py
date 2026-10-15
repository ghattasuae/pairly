# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Month-end aging, accrual suggestions and the CSV, without a database."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from pairly_pay.month_end import (
    AccrualLine,
    Aging,
    InvalidPeriodError,
    MonthEnd,
    OpenInvoice,
    accruals,
    age,
    bucket_of,
    parse_period,
    previous_period,
    to_csv,
)

END = date(2026, 9, 30)


def _inv(
    due: date | None, amount: str | None = "100.00", currency: str = "AED", **kw: object
) -> OpenInvoice:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "reference": f"INV-{uuid.uuid4().hex[:6].upper()}",
        "invoice_number": "A-1",
        "vendor_name": "Synthetic Supplies LLC",
        "status": "approved",
        "currency": currency,
        "amount": Decimal(amount) if amount is not None else None,
        "invoice_date": None,
        "due_date": due,
    }
    values.update(kw)
    return OpenInvoice(**values)  # type: ignore[arg-type]


def test_periods() -> None:
    p = parse_period("2026-02")
    assert (p.start, p.end, p.label) == (date(2026, 2, 1), date(2026, 2, 28), "2026-02")
    assert previous_period(date(2026, 1, 15)).label == "2025-12"
    running = parse_period("2026-10")
    now = datetime(2026, 10, 15, 9, tzinfo=UTC)
    assert running.as_of(now) == now
    assert parse_period("2026-09").as_of(now).date() == END
    for bad in ("2026-13", "2026-1", "26-01", "1999-01"):
        with pytest.raises(InvalidPeriodError):
            parse_period(bad)


@pytest.mark.parametrize(
    ("due", "bucket"),
    [
        (date(2026, 10, 5), "current"),
        (END, "current"),
        (date(2026, 9, 29), "1_30"),
        (date(2026, 8, 31), "1_30"),
        (date(2026, 8, 30), "31_60"),
        (date(2026, 7, 2), "61_90"),
        (date(2026, 7, 1), "90_plus"),
        (date(2026, 6, 30), "90_plus"),
    ],
)
def test_buckets_by_days_past_due_at_period_end(due: date, bucket: str) -> None:
    assert bucket_of(_inv(due), END) == bucket


def test_aging_per_currency_with_undated_and_unpriced_apart() -> None:
    no_due = _inv(None, invoice_date=date(2026, 9, 1))
    undated = _inv(None)
    unpriced = _inv(END, amount=None)
    result = age(
        [
            _inv(date(2026, 10, 10), "10.10"),
            _inv(date(2026, 9, 1), "20.205", currency="KWD"),
            no_due,
            undated,
            unpriced,
        ],
        END,
    )
    assert isinstance(result, Aging)
    aed, kwd = result.currencies
    assert aed.currency == "AED"
    assert aed.total == Decimal("210.10")
    assert aed.buckets["current"].amount == Decimal("10.10")
    assert aed.buckets["1_30"].invoices == [no_due]  # by invoice date: 29 days
    assert aed.buckets["undated"].invoices == [undated]
    assert kwd.buckets["1_30"].amount == Decimal("20.205")
    assert result.unpriced == [unpriced]


def _line(received: str, invoiced: str, price: str = "12.50", currency: str = "AED") -> AccrualLine:
    return AccrualLine(
        po_id=uuid.uuid4(),
        po_number="PO-2026-0079",
        vendor_name="Synthetic Supplies LLC",
        currency=currency,
        line_no=1,
        description="Printer paper A4",
        received=Decimal(received),
        invoiced=Decimal(invoiced),
        unit_price=Decimal(price),
    )


def test_accruals_value_received_not_invoiced_at_po_price() -> None:
    partly = _line("10", "4")
    nothing = _line("5", "5")
    over = _line("2", "3")
    fraction = _line("1.5", "0", price="0.333", currency="BHD")
    result = accruals([partly, nothing, over, fraction])
    aed, bhd = result
    assert aed.lines == [partly]
    assert partly.quantity == Decimal(6)
    assert aed.total == Decimal("75.00")
    assert bhd.total == Decimal("0.500")  # 0.4995 rounded half up at 3 places


def test_the_csv_lists_every_record_and_neutralises_formulas() -> None:
    period = parse_period("2026-09")
    risky = _inv(date(2026, 9, 20), vendor_name="=HYPERLINK(1)")
    report = MonthEnd(
        period=period,
        as_of=datetime(2026, 9, 30, 23, 59, tzinfo=UTC),
        aging=age([risky], END),
        accruals=accruals([_line("10", "4")]),
        exceptions=[],
    )
    rows = list(csv.DictReader(io.StringIO(to_csv(report).decode("utf-8"))))
    assert [r["section"] for r in rows] == ["open_payable", "accrual"]
    assert rows[0]["vendor"] == "'=HYPERLINK(1)"
    assert rows[0]["bucket"] == "1_30"
    assert rows[0]["amount"] == "100.00"
    assert rows[1]["amount"] == "75.00"
    assert rows[1]["detail"] == "Printer paper A4 (6 x 12.5)"
