# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Invoice approval rules and approval by a rule (WP4.7), pure.

* The bundled rules YAML is valid and routes by risk, vendor risk, amount, category and
  department, first match wins, with a default chain.
* A rule never approves high, critical or medium risk, an amount at or over the limit,
  another currency, a risky vendor, an invoice not matched to its purchase order, or
  anything while the setting is off (the default).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from pairly_core.approvals.rules import parse_rules
from pairly_pay.approvals import (
    INVOICE_RULES_V1,
    risk_level,
    rule_refusals,
    vendor_risk_level,
)
from pairly_pay.models import PayInvoice
from pairly_pay.payment_runs import PAYMENT_RUN_RULES_V1
from pairly_pay.payment_settings import PaymentSettings, parse_payment_settings
from pairly_pay.plugin import BANK_CHANGE_RULES_V1

RULES = parse_rules(INVOICE_RULES_V1)


def _facts(**overrides: Any) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "amount": "1200.00",
        "currency": "AED",
        "department": None,
        "category": "software",
        "vendor_risk": "low",
        "risk_level": "low",
    }
    facts.update(overrides)
    return facts


@pytest.mark.parametrize(
    ("overrides", "chain"),
    [
        ({}, "standard"),
        ({"risk_level": "high"}, "high_risk"),
        ({"risk_level": "critical", "amount": "10.00"}, "high_risk"),
        ({"vendor_risk": "high"}, "risky_vendor"),
        ({"amount": "50000.01"}, "large_amount"),
        ({"amount": "50000.00"}, "standard"),
        ({"category": "it_hardware"}, "capital_spend"),
        ({"category": "construction"}, "capital_spend"),
        ({"department": "marketing"}, "marketing_budget"),
        ({"department": "marketing", "amount": "90000"}, "large_amount"),
        ({"department": "marketing", "risk_level": "high"}, "high_risk"),
    ],
)
def test_rules_route_by_risk_vendor_amount_category_department(
    overrides: dict[str, Any], chain: str
) -> None:
    assert RULES.select_chain(_facts(**overrides)).name == chain


def test_high_risk_chains_need_two_people_and_a_finance_controller() -> None:
    for name in ("high_risk", "risky_vendor", "large_amount", "capital_spend"):
        (chain,) = [c for c in RULES.chains if c.name == name]
        assert len(chain.steps) == 2
        assert chain.steps[-1].roles == ["finance_controller"]


def test_bundled_rules_parse() -> None:
    assert parse_rules(PAYMENT_RUN_RULES_V1).chains[0].steps[0].roles == ["finance_controller"]
    assert parse_rules(BANK_CHANGE_RULES_V1).approval_type == "pay.bank_change"


def _invoice(level: str | None = "low", requires_human: bool = False, **kw: Any) -> PayInvoice:
    risk: dict[str, Any] = {"assessment_id": "x", "requires_human": requires_human}
    if level is not None:
        risk["level"] = level
    values: dict[str, Any] = {
        "risk": risk,
        "total": Decimal("900.00"),
        "currency": "AED",
        "checks": [{"check": "matching", "status": "pass", "findings": []}],
        "po_id": uuid.UUID(int=7),
        "po_match": {"status": "matched"},
    }
    values.update(kw)
    return PayInvoice(**values)


ON = PaymentSettings(auto_approve_low_risk=True, auto_approve_limit=Decimal("1000"))


def test_rule_approves_only_low_risk_under_the_limit_when_on() -> None:
    assert rule_refusals(_invoice(), ON, "low") == []
    assert "rule_approval_off" in rule_refusals(_invoice(), PaymentSettings(), "low")
    for level in ("medium", "high", "critical"):
        assert f"risk_{level}" in rule_refusals(_invoice(level), ON, "low")
    assert "risk_critical" in rule_refusals(_invoice(None), ON, "low")  # unknown: critical
    assert "risk_requires_human" in rule_refusals(_invoice(requires_human=True), ON, "low")
    assert "over_limit" in rule_refusals(_invoice(total=Decimal("1000")), ON, "low")
    assert "over_limit" in rule_refusals(_invoice(total=None), ON, "low")
    assert "other_currency" in rule_refusals(_invoice(currency="USD"), ON, "low")
    assert "vendor_risk_medium" in rule_refusals(_invoice(), ON, "medium")
    # Only an invoice matched to its purchase order.
    assert "not_matched" in rule_refusals(_invoice(po_id=None), ON, "low")
    unmatched = [{"check": "matching", "status": "fail", "findings": []}]
    assert "not_matched" in rule_refusals(_invoice(checks=unmatched), ON, "low")
    assert "not_matched" in rule_refusals(_invoice(checks=[]), ON, "low")


def test_high_risk_is_never_approved_by_a_rule_whatever_the_limit() -> None:
    huge = PaymentSettings(auto_approve_low_risk=True, auto_approve_limit=Decimal("1e11"))
    for level in ("high", "critical"):
        assert rule_refusals(_invoice(level, total=Decimal("1")), huge, "low")


def test_risk_level_and_vendor_risk() -> None:
    assert risk_level(_invoice("weird")) == "critical"
    assert vendor_risk_level([]) == "low"
    assert vendor_risk_level(["new_vendor"]) == "medium"
    assert vendor_risk_level(["new_vendor", "payment_hold"]) == "high"
    assert vendor_risk_level(["phone_on_file_changed_recently"]) == "high"


def test_payment_settings_default_off_and_validate() -> None:
    assert parse_payment_settings(None).auto_approve_low_risk is False
    assert parse_payment_settings({"auto_approve_low_risk": "yes"}).auto_approve_low_risk is False
    parsed = parse_payment_settings(
        {
            "auto_approve_low_risk": True,
            "auto_approve_limit": "-5",
            "payer_iban": "AE07 0331 2345 6789 0123 456",
            "payer_bic": "not a bic",
        }
    )
    assert parsed.auto_approve_low_risk is True
    assert parsed.auto_approve_limit == Decimal(0)
    assert parsed.payer_iban == "AE070331234567890123456"
    assert parsed.payer_bic is None
