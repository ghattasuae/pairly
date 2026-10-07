# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Rule sets: YAML validation and fail-closed evaluation (synthetic spend policy)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from pairly_core.policy.rules import (
    Outcome,
    Severity,
    evaluate_rule_set,
    parse_rule_set,
    render_reason,
    rule_set_validator,
    worst_outcome,
)
from pairly_core.policy_versions import PolicyError

SPEND_RULES = """\
schema: pairly.rules/v1
name: pay.spend
facts:
  amount: decimal
  currency: string
  category: string
  invoice_date: date
  due_date: date
  vendor.is_new: bool
  po_number: {type: string, optional: true}
rules:
  - id: software_it_approval
    description:
      en: Software over 10,000 AED needs IT approval
      ar: البرمجيات التي تزيد على 10,000 درهم تحتاج إلى موافقة تقنية المعلومات
    when: "category == 'software' and currency == 'AED' and amount > 10000"
    outcome: needs_review
    severity: high
    reason:
      en: "Software purchase of {amount} {currency} needs IT approval"
      ar: "شراء برمجيات بقيمة {amount} {currency} يحتاج إلى موافقة تقنية المعلومات"
  - id: po_required
    description:
      en: Purchases over 50,000 AED need a purchase order
      ar: المشتريات التي تزيد على 50,000 درهم تحتاج إلى أمر شراء
    when: "amount > 50000 and not exists(po_number)"
    outcome: fail
    severity: critical
    reason: "No purchase order for a purchase of {amount} {currency}"
  - id: payment_terms
    description:
      en: Payment terms longer than 60 days need finance review
      ar: شروط الدفع التي تزيد على 60 يوما تحتاج إلى مراجعة مالية
    when: "due_date - invoice_date > days(60)"
    outcome: needs_review
    severity: low
    reason: "Payment terms are longer than 60 days (due {due_date})"
  - id: new_vendor_large
    description:
      en: A new vendor with a large first invoice is checked
      ar: يتم التحقق من المورد الجديد صاحب الفاتورة الأولى الكبيرة
    when: "vendor.is_new and amount >= 20000"
    outcome: needs_review
    otherwise: pass
    reason: "New vendor with an invoice of {amount}"
"""

BASE: dict[str, Any] = {
    "amount": "4200",
    "currency": "AED",
    "category": "office",
    "invoice_date": "2026-09-01",
    "due_date": "2026-09-30",
    "vendor": {"is_new": False},
}
AS_OF = date(2026, 10, 7)


def evaluate(facts: dict[str, Any]) -> dict[str, Any]:
    rule_set = parse_rule_set(SPEND_RULES)
    result = evaluate_rule_set(rule_set, facts, as_of=AS_OF)
    return {"overall": result.overall, "by_id": {r.rule_id: r for r in result.results}}


def test_all_pass() -> None:
    out = evaluate(BASE)
    assert out["overall"] is Outcome.PASS
    assert all(r.outcome is Outcome.PASS for r in out["by_id"].values())
    reason = out["by_id"]["software_it_approval"].reason
    assert reason.startswith("Condition not met: Software over 10,000 AED")
    assert out["by_id"]["software_it_approval"].triggered is False


def test_software_needs_it_approval_with_reason_and_facts() -> None:
    out = evaluate(BASE | {"category": "software", "amount": "12500.00"})
    result = out["by_id"]["software_it_approval"]
    assert result.outcome is Outcome.NEEDS_REVIEW
    assert result.severity is Severity.HIGH
    assert result.reason == "Software purchase of 12500.00 AED needs IT approval"
    assert result.reason_ar is not None
    assert "12500.00" in result.reason_ar
    assert result.facts == {"amount": "12500.00", "category": "software", "currency": "AED"}
    assert out["overall"] is Outcome.NEEDS_REVIEW


def test_fail_beats_review_and_optional_fact() -> None:
    out = evaluate(BASE | {"category": "software", "amount": "60000"})
    assert out["by_id"]["po_required"].outcome is Outcome.FAIL
    assert out["overall"] is Outcome.FAIL
    with_po = evaluate(BASE | {"amount": "60000", "po_number": "PO-77"})
    assert with_po["by_id"]["po_required"].outcome is Outcome.PASS


def test_date_math_rule() -> None:
    out = evaluate(BASE | {"due_date": "2026-12-15"})
    result = out["by_id"]["payment_terms"]
    assert result.outcome is Outcome.NEEDS_REVIEW
    assert result.reason == "Payment terms are longer than 60 days (due 2026-12-15)"


@pytest.mark.parametrize("missing", ["amount", "currency", "category", "invoice_date"])
def test_missing_fact_is_error_never_pass(missing: str) -> None:
    facts = {k: v for k, v in BASE.items() if k != missing}
    out = evaluate(facts)
    assert out["overall"] is Outcome.ERROR
    errors = [r for r in out["by_id"].values() if r.outcome is Outcome.ERROR]
    assert errors
    assert all(r.error_code == "missing_fact" for r in errors)
    assert all("A person must review this" in r.reason for r in errors)


def test_missing_fact_is_error_even_when_short_circuit_would_skip_it() -> None:
    # "vendor.is_new and amount >= 20000": vendor.is_new is false, so a plain evaluator
    # would never read amount. The rule still errors: required facts are checked first.
    facts = BASE | {"vendor": {"is_new": False}}
    facts = {k: v for k, v in facts.items() if k != "amount"}
    result = evaluate(facts)["by_id"]["new_vendor_large"]
    assert result.outcome is Outcome.ERROR


def test_nested_fact_missing_parent() -> None:
    facts = {k: v for k, v in BASE.items() if k != "vendor"}
    assert evaluate(facts)["by_id"]["new_vendor_large"].outcome is Outcome.ERROR


def test_malformed_fact_is_error() -> None:
    out = evaluate(BASE | {"amount": "lots"})
    assert out["by_id"]["software_it_approval"].error_code == "invalid_fact"
    assert out["overall"] is Outcome.ERROR


def test_facts_must_be_a_mapping() -> None:
    rule_set = parse_rule_set(SPEND_RULES)
    with pytest.raises(PolicyError):
        evaluate_rule_set(rule_set, ["amount"], as_of=AS_OF)  # type: ignore[arg-type]


def test_worst_outcome() -> None:
    assert worst_outcome([]) is Outcome.ERROR
    assert worst_outcome([Outcome.PASS, Outcome.NEEDS_REVIEW]) is Outcome.NEEDS_REVIEW


def test_render_reason_is_plain_substitution() -> None:
    assert render_reason("{amount.__class__}", {"amount": 1}) == "?"
    assert render_reason("{a} {b}", {"a": "x"}) == "x ?"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("schema: pairly.rules/v1", "schema: pairly.rules/v9", "schema"),
        ('amount > 10000"', "amount > 'x'\"", "cannot compare"),
        ('amount > 10000"', "__import__('os')\"", "unexpected character"),
        ("{amount} {currency} needs IT", "{amount.__class__} needs IT", "not a fact"),
        ("outcome: fail", "outcome: approve", "outcome"),
        ("severity: low", "severity: urgent", "severity"),
        ("id: po_required", "id: software_it_approval", "unique"),
        ("  amount: decimal", "  amount: float", "type must be"),
        ("  amount: decimal", "  Amount: decimal", "invalid fact name"),
        ("  vendor.is_new: bool", "  vendor: bool\n  vendor.is_new: bool", "nested"),
        ("    outcome: needs_review\n    otherwise: pass", "    outcome: pass", "must differ"),
        ("      ar: يتم التحقق", "      fr: x", "description"),
    ],
)
def test_invalid_rule_sets_rejected(old: str, new: str, message: str) -> None:
    assert old in SPEND_RULES
    with pytest.raises(PolicyError, match=message):
        parse_rule_set(SPEND_RULES.replace(old, new, 1))


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("a: [unclosed", "not valid YAML"),
        ("- a\n- b", "must be a mapping"),
        ("schema: pairly.rules/v1\nname: x\nfacts: {}\nrules: []", "invalid rule set"),
        ("!!python/object/apply:os.system ['id']", "not valid YAML"),
    ],
)
def test_bad_documents(content: str, message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        parse_rule_set(content)


def test_validator_checks_name() -> None:
    assert rule_set_validator("pay.spend")(SPEND_RULES).name == "pay.spend"
    with pytest.raises(PolicyError, match="name"):
        rule_set_validator("pay.other")(SPEND_RULES)
