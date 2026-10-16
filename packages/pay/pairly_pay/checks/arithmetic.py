# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Arithmetic and VAT (brief 4.4.1).

Rules (codes ``arithmetic_error`` and ``vat_error``):

* ``line_total_mismatch``    quantity x unit price is not the line total;
* ``subtotal_mismatch``      the subtotal is not the sum of the line totals;
* ``line_vat_mismatch``      a line's VAT is not its rate times its total;
* ``vat_sum_mismatch``       the VAT total is not the sum of the line VAT;
* ``total_mismatch``         the total is not subtotal plus VAT;
* ``vat_rate_mismatch``      the VAT total is not what the lines' rates give (vat_error);
* ``rate_not_allowed``       a line (or the invoice) uses a rate that is not configured;
* ``zero_rated_charged``     VAT on a line whose description is a zero-rated supply;
* ``zero_rate_unexplained``  a UAE VAT-registered vendor charges 0% with no zero-rated,
                             exempt or reverse-charge reason on the invoice.

All exact ``Decimal`` arithmetic, rounded half up to the currency's minor unit, with the
tolerance of ``pairly_pay.checks.settings`` (one minor unit per rounded figure). A rule
whose inputs were not read is skipped; when the total itself was not read, or nothing
was read, the check cannot run (``error``, fail closed).
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from pairly_core.risk.types import Severity
from pairly_pay.checks.settings import (
    REVERSE_CHARGE_WORDS,
    CheckSettings,
    minor_unit,
)
from pairly_pay.checks.types import (
    CheckResult,
    Finding,
    InvoiceFacts,
    Line,
    error,
    finding,
    result,
)

CHECK: Final = "arithmetic"
VERSION: Final = "1"
_HUNDRED: Final = Decimal(100)
_SPACE: Final = re.compile(r"\s+")


def _q(value: Decimal, unit: Decimal) -> Decimal:
    return value.quantize(unit, rounding=ROUND_HALF_UP)


def _s(value: Decimal) -> str:
    return f"{value:,.2f}" if value == value.quantize(Decimal("0.01")) else f"{value:,}"


def _fold(text: str | None) -> str:
    return _SPACE.sub(" ", (text or "").casefold()).strip()


def _mentions(text: str, words: tuple[str, ...]) -> str | None:
    for word in words:
        folded = word.casefold()
        if re.search(rf"(?<!\w){re.escape(folded)}(?!\w)", text):
            return word
    return None


def _bad(rule: str, code: str, **values: Any) -> Finding:
    return finding(CHECK, code, rule, Severity.HIGH, evidence={}, **values)


def _with_evidence(f: Finding, **evidence: str | int | None) -> Finding:
    return Finding(
        check=f.check,
        code=f.code,
        rule=f.rule,
        severity=f.severity,
        message_en=f.message_en,
        message_ar=f.message_ar,
        evidence=evidence,
    )


def _lines(facts: InvoiceFacts, unit: Decimal, tol: Decimal) -> list[Finding]:
    out: list[Finding] = []
    for line in facts.lines:
        n = line.index + 1
        q, p, total = line.quantity, line.unit_price, line.line_total
        if q is not None and p is not None and total is not None:
            expected = _q(q * p, unit)
            if abs(expected - total) > tol:
                out.append(
                    _with_evidence(
                        _bad(
                            "line_total_mismatch",
                            "arithmetic_error",
                            line=n,
                            quantity=f"{q.normalize():f}",
                            unit_price=_s(p),
                            expected=_s(expected),
                            printed=_s(total),
                        ),
                        line=n,
                        expected=str(expected),
                        printed=str(total),
                    )
                )
        rate, vat = line.vat_rate, line.vat_amount
        if rate is not None and vat is not None and total is not None:
            expected = _q(total * rate / _HUNDRED, unit)
            if abs(expected - vat) > tol:
                out.append(
                    _with_evidence(
                        _bad(
                            "line_vat_mismatch",
                            "arithmetic_error",
                            line=n,
                            rate=f"{rate.normalize():f}",
                            base=_s(total),
                            expected=_s(expected),
                            printed=_s(vat),
                        ),
                        line=n,
                        expected=str(expected),
                        printed=str(vat),
                    )
                )
    return out


def _tax_codes(facts: InvoiceFacts, settings: CheckSettings) -> list[Finding]:
    """Rates per line: configured, zero-rated supplies, unexplained zero rates."""
    out: list[Finding] = []
    allowed = settings.allowed_rates
    text = _fold(facts.document.text if facts.document else "")
    vendor_country = facts.vendor.country if facts.vendor else None
    domestic = (
        vendor_country == settings.home_country
        if vendor_country
        else bool(facts.vendor_trn and len(facts.vendor_trn) == 15 and facts.vendor_trn.isdigit())
    )
    reverse_charge = not domestic or _mentions(text, REVERSE_CHARGE_WORDS) is not None
    if facts.vat_rate is not None and facts.vat_rate not in allowed:
        out.append(
            _with_evidence(
                finding(
                    CHECK,
                    "vat_error",
                    "header_rate_not_allowed",
                    Severity.HIGH,
                    rate=f"{facts.vat_rate.normalize():f}",
                ),
                rate=str(facts.vat_rate),
            )
        )
    unexplained_zero = 0
    rated = 0
    for line in facts.lines:
        if line.vat_rate is None:
            continue
        rated += 1
        n = line.index + 1
        description = _fold(line.description)
        zero_supply = _mentions(description, settings.zero_rated_words + settings.exempt_words)
        if line.vat_rate not in allowed:
            out.append(
                _with_evidence(
                    finding(
                        CHECK,
                        "vat_error",
                        "rate_not_allowed",
                        Severity.HIGH,
                        line=n,
                        rate=f"{line.vat_rate.normalize():f}",
                    ),
                    line=n,
                    rate=str(line.vat_rate),
                )
            )
        elif line.vat_rate > 0 and zero_supply is not None and domestic:
            out.append(
                _with_evidence(
                    finding(
                        CHECK,
                        "vat_error",
                        "zero_rated_charged",
                        Severity.HIGH,
                        line=n,
                        rate=f"{line.vat_rate.normalize():f}",
                    ),
                    line=n,
                    rate=str(line.vat_rate),
                )
            )
        elif line.vat_rate == 0 and zero_supply is None and not reverse_charge:
            unexplained_zero += 1
    if rated and unexplained_zero == rated and domestic:
        out.append(
            _with_evidence(
                finding(CHECK, "vat_error", "zero_rate_unexplained", Severity.HIGH),
                lines=unexplained_zero,
            )
        )
    return out


def check_arithmetic(facts: InvoiceFacts, settings: CheckSettings) -> CheckResult:
    if not facts.read:
        return error(CHECK, VERSION, "nothing_read")
    if facts.total is None:
        return error(CHECK, VERSION, "total_missing")
    unit = minor_unit(facts.currency)
    tol = unit * settings.rounding_units
    findings = _lines(facts, unit, tol)
    lines: tuple[Line, ...] = facts.lines
    totals = [x.line_total for x in lines]
    if facts.subtotal is not None and lines and all(t is not None for t in totals):
        expected = sum((t for t in totals if t is not None), Decimal(0))
        if abs(expected - facts.subtotal) > tol:
            findings.append(
                _with_evidence(
                    _bad(
                        "subtotal_mismatch",
                        "arithmetic_error",
                        printed=_s(facts.subtotal),
                        expected=_s(expected),
                    ),
                    expected=str(expected),
                    printed=str(facts.subtotal),
                )
            )
    vats = [x.vat_amount for x in lines]
    if facts.vat_amount is not None and lines and all(v is not None for v in vats):
        expected = sum((v for v in vats if v is not None), Decimal(0))
        if abs(expected - facts.vat_amount) > tol:
            findings.append(
                _with_evidence(
                    _bad(
                        "vat_sum_mismatch",
                        "arithmetic_error",
                        printed=_s(facts.vat_amount),
                        expected=_s(expected),
                    ),
                    expected=str(expected),
                    printed=str(facts.vat_amount),
                )
            )
    rates_known = lines and all(x.vat_rate is not None and x.line_total is not None for x in lines)
    if facts.vat_amount is not None and rates_known:
        expected = sum(
            (
                _q((x.line_total or Decimal(0)) * (x.vat_rate or Decimal(0)) / _HUNDRED, unit)
                for x in lines
            ),
            Decimal(0),
        )
        if abs(expected - facts.vat_amount) > tol * len(lines):
            findings.append(
                _with_evidence(
                    finding(
                        CHECK,
                        "vat_error",
                        "vat_rate_mismatch",
                        Severity.HIGH,
                        printed=_s(facts.vat_amount),
                        expected=_s(expected),
                    ),
                    expected=str(expected),
                    printed=str(facts.vat_amount),
                )
            )
    if facts.subtotal is not None and facts.vat_amount is not None:
        expected = facts.subtotal + facts.vat_amount
        if abs(expected - facts.total) > tol:
            findings.append(
                _with_evidence(
                    _bad(
                        "total_mismatch",
                        "arithmetic_error",
                        printed=_s(facts.total),
                        expected=_s(expected),
                    ),
                    expected=str(expected),
                    printed=str(facts.total),
                )
            )
    findings += _tax_codes(facts, settings)
    return result(CHECK, VERSION, findings)
