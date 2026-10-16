# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Open payables and the cash forecast on hand-built rows: applied credits are always
subtracted, amounts are Decimal at the currency's minor unit, and every number lists
the invoices behind it."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from pairly_pay.analytics import Facts, compute, open_payables
from pairly_pay.payables import (
    PayableFact,
    amount_due,
    expected_date,
    forecast,
    week_start,
)

TODAY = date(2026, 10, 14)  # a Wednesday
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def _p(
    n: int,
    status: str = "approved",
    *,
    total: str | None = "1000.00",
    approved: str | None = None,
    credit: str = "0",
    currency: str | None = "AED",
    due: date | None = None,
    run: date | None = None,
    invoice_date: date | None = None,
) -> PayableFact:
    return PayableFact(
        id=uuid.UUID(int=n),
        reference=f"INV-{n:06d}",
        status=status,
        currency=currency,
        total=Decimal(total) if total is not None else None,
        approved_amount=Decimal(approved) if approved is not None else None,
        credit_total=Decimal(credit),
        invoice_date=invoice_date,
        due_date=due,
        run_date=run,
    )


def test_amount_due_subtracts_applied_credits_at_the_minor_unit() -> None:
    assert amount_due(_p(1, approved="1000.00", credit="250.50")) == Decimal("749.50")
    # The approved amount wins over the total; before approval the total is used.
    assert amount_due(_p(2, total="900", approved="800", credit="0")) == Decimal("800.00")
    assert amount_due(_p(3, "needs_review", total="120.005")) == Decimal("120.01")
    # Three-place currencies keep three places.
    assert amount_due(_p(4, total="10.0005", currency="KWD")) == Decimal("10.001")
    # Never below zero, never a guess without an amount or a currency.
    assert amount_due(_p(5, total="100", credit="150")) == Decimal("0.00")
    assert amount_due(_p(6, total=None)) is None
    assert amount_due(_p(7, currency=None)) is None
    assert isinstance(amount_due(_p(1)), Decimal)


def test_expected_date_prefers_the_run_then_the_due_date() -> None:
    later = TODAY + timedelta(days=20)
    run = TODAY + timedelta(days=3)
    assert expected_date(_p(1, "scheduled", due=later, run=run), TODAY) == run
    # The run date only counts for a scheduled invoice.
    assert expected_date(_p(2, "approved", due=later, run=run), TODAY) == later
    assert expected_date(_p(3, invoice_date=later), TODAY) == later
    # Overdue: as soon as possible, today.
    assert expected_date(_p(4, due=TODAY - timedelta(days=9)), TODAY) == TODAY
    assert expected_date(_p(5), TODAY) is None


def test_open_payables_per_currency_net_of_credits() -> None:
    rows = [
        _p(1, "approved", approved="1000.00", credit="100.00"),
        _p(2, "scheduled", approved="500.00", credit="0"),
        _p(3, "needs_review", total="999.00"),  # not a commitment yet
        _p(4, "approved", approved="40.000", currency="KWD", credit="10.000"),
    ]
    metrics = {m.key: m for m in open_payables(rows)}
    assert set(metrics) == {"open_payables.AED", "open_payables.KWD"}
    aed = metrics["open_payables.AED"]
    assert aed.value == 1400.0
    assert aed.detail["amount"] == "1400.00"
    assert aed.detail["gross"] == "1500.00"
    assert aed.detail["credits_applied"] == "100.00"
    assert (aed.detail["approved"], aed.detail["scheduled"]) == (1, 1)
    assert [r.label for r in aed.records] == ["INV-000001", "INV-000002"]
    assert metrics["open_payables.KWD"].detail["amount"] == "30.000"
    # Through compute: empty without payables, never a made-up zero.
    assert compute(Facts(), now=NOW).open_payables == []
    assert compute(Facts(payables=tuple(rows)), now=NOW).open_payables[0].key == (
        "open_payables.AED"
    )


def test_forecast_buckets_twelve_weeks_per_currency_with_review_apart() -> None:
    monday = week_start(TODAY)
    rows = [
        # Overdue: the first week.
        _p(1, "approved", approved="100.00", due=TODAY - timedelta(days=30)),
        # Scheduled in a run next week, whatever its due date.
        _p(2, "scheduled", approved="200.00", run=monday + timedelta(days=8), due=TODAY),
        # A credit note applied to it reduces the week's amount.
        _p(3, "approved", approved="300.00", credit="50.00", due=monday + timedelta(days=15)),
        # Still in review: its own line.
        _p(4, "needs_review", total="400.00", due=monday + timedelta(days=15)),
        _p(5, "awaiting_approval", total="10.000", currency="BHD", due=TODAY),
        # After the twelfth week, without a date, without an amount.
        _p(6, "approved", approved="600.00", due=monday + timedelta(weeks=12)),
        _p(7, "approved", approved="700.00"),
        _p(8, "approved", total=None, due=TODAY),
        # Paid and rejected invoices are not in the forecast.
        _p(9, "paid", approved="900.00", due=TODAY),
    ]
    out = forecast(rows, today=TODAY)
    assert out.weeks == 12
    assert [c.currency for c in out.currencies] == ["AED", "BHD"]
    aed = out.currencies[0]
    assert len(aed.weeks) == 12
    assert aed.weeks[0].start == monday
    assert aed.weeks[0].end == monday + timedelta(days=6)
    assert aed.weeks[11].start == monday + timedelta(weeks=11)
    assert aed.weeks[0].committed.amount == Decimal("100.00")
    assert aed.weeks[1].committed.amount == Decimal("200.00")
    week3 = aed.weeks[2]
    assert week3.committed.amount == Decimal("250.00")
    assert week3.committed.gross == Decimal("300.00")
    assert week3.committed.credits == Decimal("50.00")
    assert [f.reference for f in week3.committed.invoices] == ["INV-000003"]
    assert week3.in_review.amount == Decimal("400.00")
    assert aed.committed_total == Decimal("550.00")
    assert aed.in_review_total == Decimal("400.00")
    assert aed.later_committed.amount == Decimal("600.00")
    assert [f.reference for f in aed.undated_committed.invoices] == ["INV-000007"]
    assert [f.reference for f in out.unpriced] == ["INV-000008"]
    bhd = out.currencies[1]
    assert bhd.weeks[0].in_review.amount == Decimal("10.000")
    assert bhd.committed_total == Decimal(0)
    for currency in out.currencies:
        for week in currency.weeks:
            assert isinstance(week.committed.amount, Decimal)
            assert week.committed.amount == sum(
                (amount_due(f) or Decimal(0) for f in week.committed.invoices), Decimal(0)
            )


def test_forecast_is_deterministic_and_rejects_no_weeks() -> None:
    rows = [
        _p(n, "approved", approved=f"{n}.00", due=TODAY + timedelta(days=n)) for n in range(1, 40)
    ]
    first = forecast(rows, today=TODAY)
    reordered = forecast(list(reversed(rows)), today=TODAY)
    assert [w.committed.amount for w in first.currencies[0].weeks] == [
        w.committed.amount for w in reordered.currencies[0].weeks
    ]
    assert sum((w.committed.amount for w in first.currencies[0].weeks), Decimal(0)) + (
        first.currencies[0].later_committed.amount
    ) == sum((Decimal(n) for n in range(1, 40)), Decimal(0))
    with pytest.raises(ValueError, match="weeks"):
        forecast(rows, today=TODAY, weeks=0)
