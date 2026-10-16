# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pay analytics (brief 4.9) on a hand-built dataset: every number is what the rows say,
links to the rows it counts, and "no data" is None, never 0."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pairly_pay.analytics import (
    Facts,
    InvoiceFact,
    Metric,
    StepFact,
    TicketFact,
    compute,
    reason_category,
)

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def _check(name: str, code: str, severity: str, needs_review: bool = True) -> dict[str, Any]:
    return {
        "check": name,
        "status": "fail",
        "findings": [
            {"code": code, "rule": code, "severity": severity, "needs_review": needs_review}
        ],
    }


TOUCHLESS = InvoiceFact(
    id=_id(1),
    reference="AP-0001",
    status="paid",
    created_at=T0,
    invoice_date=date(2026, 8, 31),
    due_date=date(2026, 9, 30),
    currency="AED",
    total=Decimal("1000.00"),
    approved_at=T0 + timedelta(hours=2),
    approved_by_rule=True,
    paid_at=T0 + timedelta(hours=26),
    cost_micros=0,
    model_calls=3,
    recorded_calls=3,
)
FRAUD = InvoiceFact(
    id=_id(2),
    reference="AP-0002",
    status="rejected",
    created_at=T0,
    currency="AED",
    total=Decimal("48000.00"),
    review_reasons=("check:bank_mismatch", "risk:critical"),
    checks=(_check("bank", "bank_mismatch", "critical"),),
    model_calls=2,
    recorded_calls=2,
)
DUPLICATE = InvoiceFact(
    id=_id(3),
    reference="AP-0003",
    status="needs_review",
    created_at=T0,
    currency="AED",
    total=Decimal("500.00"),
    review_reasons=("check:duplicate_invoice_number",),
    checks=(_check("duplicates", "duplicate_invoice_number", "high"),),
)
LATE = InvoiceFact(
    id=_id(4),
    reference="AP-0004",
    status="paid",
    created_at=T0,
    invoice_date=date(2026, 8, 1),
    due_date=date(2026, 9, 2),
    currency="AED",
    total=Decimal("200.00"),
    review_reasons=("extraction:low_confidence:total",),
    reviewed=True,
    approved_at=T0 + timedelta(hours=6),
    paid_at=datetime(2026, 9, 10, 9, 0, tzinfo=UTC),
)
FACTS = Facts(
    invoices=(TOUCHLESS, FRAUD, DUPLICATE, LATE),
    tickets=(
        TicketFact(_id(10), "BCT-1", "rejected", "invoice", T0),
        TicketFact(_id(11), "BCT-2", "awaiting_verification", "email", T0),
        TicketFact(_id(12), "BCT-3", "completed", "staff", T0),
    ),
    steps=(
        StepFact(_id(20), "Invoice AP-0004", str(_id(4)), "approved", T0, T0 + timedelta(hours=6)),
        StepFact(_id(21), "Invoice AP-0001", str(_id(1)), "approved", NOW, T0),
        StepFact(_id(22), "Invoice AP-0003", str(_id(3)), "active", T0, None),
    ),
)


def _ids(m: Metric) -> set[str]:
    return {r.id for r in m.records}


def test_touchless_and_cycle_time() -> None:
    a = compute(FACTS, now=NOW)
    assert a.invoices == 4
    assert a.touchless_rate.value == 0.5  # one of the two approved invoices
    assert a.touchless_rate.n == 2
    assert _ids(a.touchless_rate) == {str(_id(1))}
    receipt_to_approval, approval_to_payment, receipt_to_payment = a.cycle_time
    assert receipt_to_approval.key == "cycle.receipt_to_approval"
    assert receipt_to_approval.value == 4.0  # median of 2 h and 6 h
    assert receipt_to_approval.detail == {"median": 4.0, "mean": 4.0}
    assert approval_to_payment.n == 2
    assert receipt_to_payment.records_total == 2


def test_cost_is_zero_in_recorded_mode_and_says_so() -> None:
    cost = compute(FACTS, now=NOW).cost_per_invoice
    assert cost.value == 0.0
    assert cost.n == 2
    assert cost.detail["recorded_mode"] is True
    assert cost.detail["model_calls"] == 5


def test_exceptions_by_category() -> None:
    rates = {m.key: m for m in compute(FACTS, now=NOW).exception_rate}
    assert rates["exceptions.all"].value == 0.75
    assert rates["exceptions.check.bank"].value == 0.25
    assert rates["exceptions.check.duplicates"].value == 0.25
    assert rates["exceptions.risk"].value == 0.25
    assert rates["exceptions.reading"].value == 0.25
    assert _ids(rates["exceptions.check.bank"]) == {str(_id(2))}
    assert reason_category("vendor_not_found", DUPLICATE) == "vendor"
    assert reason_category("approval_blocked:x", DUPLICATE) == "approval"
    assert reason_category("check:unknown", DUPLICATE) == "check.other"
    assert reason_category("mystery", DUPLICATE) == "other"


def test_duplicates_bank_changes_and_fraud_flags() -> None:
    a = compute(FACTS, now=NOW)
    count, value = a.duplicates_prevented
    assert count.value == 1.0
    assert _ids(count) == {str(_id(3))}
    assert value.key == "duplicates_prevented_value.AED"
    assert value.detail["amount"] == "500.00"
    blocked = a.bank_changes_blocked
    assert blocked.value == 1.0
    assert [r.kind for r in blocked.records] == ["bank_change"]
    assert blocked.detail["open"] == 1
    assert blocked.detail["by_source"] == {"invoice": 1}
    flags = {m.key: m for m in a.fraud_flags}
    assert flags["fraud_flags"].value == 1.0
    assert flags["fraud_flags.rejected"].value == 1.0
    assert flags["fraud_flags.paid"].value == 0.0


def test_sla_on_time_and_dpo() -> None:
    a = compute(FACTS, now=NOW)
    # Decided 6 h after a due time at receipt, and still open past its due time.
    assert a.approval_sla_breaches.value == 2.0
    assert _ids(a.approval_sla_breaches) == {str(_id(20)), str(_id(22))}
    assert a.on_time_payment_rate.value == 0.5
    assert _ids(a.on_time_payment_rate) == {str(_id(1))}
    assert a.on_time_payment_rate.detail["late"] == 1
    # 2 days (31 Aug to 2 Sep) and 40 days (1 Aug to 10 Sep).
    assert a.days_payable_outstanding.value == 21.0
    assert a.days_payable_outstanding.n == 2


def test_no_data_is_none_never_zero() -> None:
    a = compute(Facts(), now=NOW)
    assert a.invoices == 0
    singles = [
        a.touchless_rate,
        a.cost_per_invoice,
        a.bank_changes_blocked,
        a.approval_sla_breaches,
        a.on_time_payment_rate,
        a.days_payable_outstanding,
    ]
    lists = [*a.cycle_time, *a.exception_rate, *a.duplicates_prevented, *a.fraud_flags]
    for metric in [*singles, *lists]:
        assert metric.value is None, metric.key
        assert metric.records == []
