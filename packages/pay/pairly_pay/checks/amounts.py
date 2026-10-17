# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Amount anomalies (brief 4.4.5).

* ``amount_outlier``: the total against this vendor's earlier totals in the same
  currency, with the engine's robust statistics (``pairly_core.risk.stats.amount_outlier``:
  median and MAD modified z-score) above ``outlier_threshold`` and at least
  ``outlier_min_ratio`` times the median: medium; at twice that ratio: high. A lower
  amount than usual is not a risk. Fewer than ``outlier_min_history`` earlier invoices:
  not compared.
* ``round_amount`` (low): the subtotal or the total is a multiple of ``round_unit`` and
  at least ``round_minimum``.
* ``split_under_threshold`` (high): this invoice is under the approval threshold by at
  most ``split_margin_pct`` percent, and with the vendor's other invoices of the window
  (``split_window_days`` around its date) that are also under the threshold, the sum
  reaches it. The related invoices are named; the service sends them to a person too.
  Only invoices in the threshold's currency are compared.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from pairly_core.risk.stats import amount_outlier
from pairly_core.risk.types import Severity
from pairly_pay.checks.settings import CheckSettings, amount_display
from pairly_pay.checks.types import (
    CheckResult,
    Finding,
    History,
    InvoiceFacts,
    PastInvoice,
    error,
    finding,
    result,
)

CHECK: Final = "amounts"
VERSION: Final = "1"


def _money(value: Decimal, currency: str | None) -> str:
    return amount_display(value, currency)


def _outlier(facts: InvoiceFacts, history: History, settings: CheckSettings) -> Finding | None:
    if facts.total is None:
        return None
    past = [
        p.total
        for p in history.vendor_invoices
        if p.total is not None and p.id != facts.id and (p.currency or "") == (facts.currency or "")
    ]
    found = amount_outlier(
        facts.total,
        past,
        threshold=settings.outlier_threshold,
        min_history=settings.outlier_min_history,
    )
    if not found.enough_history or not found.is_outlier or found.median is None:
        return None
    z = found.z_score if found.z_score is not None else Decimal(0)
    # Only more than usual is a risk, and only clearly more: the robust z-score finds
    # the unusual, the ratio to the median keeps a vendor with steady amounts from
    # being flagged for an ordinary larger order.
    if z <= 0 or found.median <= 0:
        return None
    ratio = facts.total / found.median
    if ratio < settings.outlier_min_ratio:
        return None
    extreme = ratio >= settings.outlier_min_ratio * 2
    return finding(
        CHECK,
        "amount_anomaly",
        "amount_outlier",
        Severity.HIGH if extreme else Severity.MEDIUM,
        evidence={
            "median": str(found.median),
            "z_score": str(z.quantize(Decimal("0.01"))) if z.is_finite() else str(z),
            "history_size": found.history_size,
            "ratio": str(ratio.quantize(Decimal("0.01"))),
        },
        amount=_money(facts.total, facts.currency),
        currency=facts.currency,
        median=_money(found.median, facts.currency),
        count=found.history_size,
    )


def _round(facts: InvoiceFacts, settings: CheckSettings) -> Finding | None:
    for value in (facts.subtotal, facts.total):
        if (
            value is not None
            and value >= settings.round_minimum
            and value % settings.round_unit == 0
        ):
            return finding(
                CHECK,
                "round_amount",
                "round_amount",
                Severity.LOW,
                evidence={"amount": str(value)},
                amount=_money(value, facts.currency),
            )
    return None


def _split(facts: InvoiceFacts, history: History, settings: CheckSettings) -> Finding | None:
    threshold = settings.approval_threshold
    if (
        facts.total is None
        or facts.invoice_date is None
        or (facts.currency or "") != settings.approval_currency
        or threshold <= 0
    ):
        return None
    floor = threshold * (Decimal(100) - settings.split_margin_pct) / Decimal(100)
    if not floor <= facts.total < threshold:
        return None
    window = settings.split_window_days
    group: list[PastInvoice] = [
        p
        for p in history.vendor_invoices
        if p.id != facts.id
        and p.total is not None
        and p.invoice_date is not None
        and (p.currency or "") == settings.approval_currency
        and floor <= p.total < threshold
        and abs((p.invoice_date - facts.invoice_date).days) <= window
    ]
    total = facts.total + sum((p.total for p in group if p.total is not None), Decimal(0))
    if not group or total < threshold:
        return None
    return finding(
        CHECK,
        "split_under_threshold",
        "split_under_threshold",
        Severity.HIGH,
        evidence={"sum": str(total), "threshold": str(threshold), "invoices": len(group) + 1},
        related_ids=tuple(p.id for p in group),
        count=len(group) + 1,
        days=window,
        threshold=_money(threshold, settings.approval_currency),
        currency=settings.approval_currency,
        sum=_money(total, settings.approval_currency),
    )


def check_amounts(facts: InvoiceFacts, history: History, settings: CheckSettings) -> CheckResult:
    if not facts.read:
        return error(CHECK, VERSION, "nothing_read")
    if facts.total is None:
        return error(CHECK, VERSION, "total_missing")
    findings = [
        f
        for f in (
            _outlier(facts, history, settings),
            _round(facts, settings),
            _split(facts, history, settings),
        )
        if f is not None
    ]
    return result(CHECK, VERSION, findings)
