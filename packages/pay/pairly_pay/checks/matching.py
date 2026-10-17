# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""2-way and 3-way matching of an invoice with its purchase order (brief 4.5.2).

The invoice cites a PO number; ``History.purchase_order`` is the PO with that number
when one is on file, with what was received so far (every goods receipt) and what
earlier invoices citing it took (``invoiced_before``: rejected invoices left out, later
invoices never counted, so running the checks again gives the same answer).

* **No PO cited**: ``no_po`` (rule ``po_required``, a person looks) when the
  organisation requires a PO (``require_po``) or the vendor has POs on file; else an
  information note (``po_not_cited``). Either way the invoice is not matched.
* **Unknown PO** (a number with no PO on file): ``no_po`` (``po_unknown``) once the
  organisation keeps POs in Pairly (any PO on file, or ``require_po``); before that, an
  information note (``po_not_on_file``): nothing to match with, and a person approves.
* **The PO itself**: another vendor's PO (``po_vendor``, high), a vendor that could not
  be confirmed (``po_vendor_unverified``), another currency (``po_currency``), a closed
  or cancelled PO (``po_closed``): ``po_mismatch``.
* **Lines**: each invoice line is paired with a PO line (same description; else in
  order when the counts agree; else the nearest unit price within 50%). A line with no
  PO line (``line_not_on_po``) or that could not be read is ``unmatched_line``.
* **Price** (``price_variance``): the invoice unit price more than ``match_price_pct``
  percent above the PO's. Paying less than the PO is not a variance.
* **Quantity** (``quantity_variance``): more than ``match_quantity_pct`` percent above
  what is left to invoice of the line: received minus earlier invoices (3-way), or
  ordered minus earlier invoices (2-way). A partial delivery invoiced as received
  matches and leaves the rest of the PO open; a second invoice takes what is left.
* **Receipt** (``missing_receipt``): a 3-way PO with no goods receipt at all
  (``receipt_missing``), or an invoiced line with nothing received (``line_not_received``).
* **Total** (``total_variance``): the subtotal more than the larger of
  ``match_total_pct`` percent and ``match_total_amount`` above the PO value of the
  invoiced lines (their quantities at the PO prices), which catches charges that are
  not on the PO.

Every exception is medium severity and sends the invoice to a person (the exceptions
queue); another vendor's PO is high. A matched invoice gets ``pass`` with a note
naming the PO, and its line allocation is stored on the invoice (``po_match``) so later
invoices of the same PO see what is left. ``Decimal`` throughout; tolerances compare
exactly (``price * 100 > po_price * (100 + pct)``).

Evidence holds the PO id and number, line numbers and figures, never descriptions.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal

from pairly_core.risk.types import Severity
from pairly_pay.checks.settings import CheckSettings, amount_display
from pairly_pay.checks.types import (
    CheckResult,
    CheckStatus,
    Finding,
    History,
    InvoiceFacts,
    Line,
    PoFacts,
    PoLineFacts,
    finding,
)

CHECK: Final = "matching"
VERSION: Final = "1"
EXCEPTION_CODES: Final = frozenset(
    {
        "no_po",
        "po_mismatch",
        "unmatched_line",
        "price_variance",
        "quantity_variance",
        "missing_receipt",
        "total_variance",
    }
)
# How far a unit price may be from a PO line's for the two to be paired at all.
PAIR_WINDOW: Final = Decimal("0.5")
THREE_WAY: Final = ("3-way (PO and goods received)", "ثلاثية (أمر الشراء والبضائع المستلمة)")
TWO_WAY: Final = ("2-way (PO)", "ثنائية (أمر الشراء)")
PO_STATES: Final = {"closed": ("closed", "مغلق"), "cancelled": ("cancelled", "ملغى")}
WHY_SETTINGS: Final = (
    "the organisation requires a PO on every invoice",
    "تشترط المؤسسة أمر شراء لكل فاتورة",
)
WHY_VENDOR: Final = (
    "this vendor has purchase orders on file",
    "لهذا المورد أوامر شراء مسجلة",
)
_WORDS: Final = re.compile(r"[\W_]+", re.UNICODE)
MatchStatus = Literal["matched", "exception", "not_cited"]


@dataclass(frozen=True, slots=True)
class Allocation:
    invoice_line: int
    po_line_no: int
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    result: CheckResult
    status: MatchStatus
    po_id: str | None = None
    allocations: tuple[Allocation, ...] = ()

    @property
    def matched(self) -> bool:
        return self.status == "matched"

    def as_json(self) -> dict[str, Any]:
        """What is stored on the invoice (``po_match``)."""
        return {
            "status": self.status,
            "po_id": self.po_id,
            "lines": [
                {
                    "invoice_line": a.invoice_line,
                    "po_line_no": a.po_line_no,
                    "quantity": format(a.quantity, "f"),
                }
                for a in self.allocations
            ],
        }


def _norm(text: str | None) -> str:
    return _WORDS.sub(" ", (text or "").lower()).strip()


def _num(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _pct(value: Decimal) -> str:
    return _num(value.quantize(Decimal("0.01")))


def pair_lines(
    lines: Sequence[Line], po_lines: Sequence[PoLineFacts]
) -> list[tuple[Line, PoLineFacts | None]]:
    """Each invoice line with its PO line (or None), one PO line per invoice line."""
    remaining = sorted(po_lines, key=lambda p: p.line_no)
    paired: dict[int, PoLineFacts] = {}
    for line in lines:
        key = _norm(line.description)
        found = next((p for p in remaining if key and _norm(p.description) == key), None)
        if found is not None:
            paired[line.index] = found
            remaining.remove(found)
    rest = [line for line in lines if line.index not in paired]
    if rest and len(rest) == len(remaining):
        for line, po_line in zip(rest, remaining, strict=True):
            paired[line.index] = po_line
        remaining = []
        rest = []
    for line in rest:
        price = line.unit_price
        if price is None or not remaining:
            continue
        best: tuple[Decimal, int, PoLineFacts] | None = None
        for po_line in remaining:
            if po_line.unit_price <= 0:
                continue
            gap = abs(price - po_line.unit_price) / po_line.unit_price
            rank = (gap, abs(po_line.line_no - 1 - line.index), po_line)
            if gap <= PAIR_WINDOW and (best is None or rank[:2] < best[:2]):
                best = rank
        if best is not None:
            paired[line.index] = best[2]
            remaining.remove(best[2])
    return [(line, paired.get(line.index)) for line in lines]


def _exception(
    code: str,
    rule: str,
    *,
    at: PoFacts | None = None,
    severity: Severity = Severity.MEDIUM,
    **values: Any,
) -> Finding:
    if at is not None:
        values = {"po_id": at.id, "po": at.number, **values}
    evidence = {k: v for k, v in values.items() if isinstance(v, str | int | bool) or v is None}
    return finding(CHECK, code, rule, severity, evidence=evidence, forces_review=True, **values)


def _done(findings: list[Finding], po: PoFacts, allocations: list[Allocation]) -> MatchOutcome:
    exceptions = [f for f in findings if f.code in EXCEPTION_CODES]
    if exceptions:
        return MatchOutcome(
            CheckResult(CHECK, VERSION, CheckStatus.FAIL, tuple(findings[:40])),
            "exception",
            po.id,
            tuple(allocations),
        )
    note = finding(
        CHECK,
        "po_matched",
        "po_matched",
        Severity.INFO,
        evidence={"po_id": po.id, "po_number": po.number, "match_type": po.match_type},
        po=po.number,
        kind=THREE_WAY if po.match_type == "3-way" else TWO_WAY,
        lines=len(allocations),
    )
    return MatchOutcome(
        CheckResult(CHECK, VERSION, CheckStatus.PASS, (note,)),
        "matched",
        po.id,
        tuple(allocations),
    )


def match_invoice(facts: InvoiceFacts, history: History, settings: CheckSettings) -> MatchOutcome:
    number = (facts.po_number or "").strip()
    po = history.purchase_order
    if not number:
        if settings.require_po or history.vendor_uses_pos:
            found = _exception(
                "no_po",
                "po_required",
                why="settings" if settings.require_po else "vendor",
                reason=WHY_SETTINGS if settings.require_po else WHY_VENDOR,
            )
            return MatchOutcome(
                CheckResult(CHECK, VERSION, CheckStatus.FAIL, (found,)), "exception"
            )
        note = finding(CHECK, "no_po", "po_not_cited", Severity.INFO)
        return MatchOutcome(CheckResult(CHECK, VERSION, CheckStatus.FAIL, (note,)), "not_cited")
    if po is None:
        if not (settings.require_po or history.org_uses_pos):
            note = finding(CHECK, "no_po", "po_not_on_file", Severity.INFO, po=number[:64])
            return MatchOutcome(CheckResult(CHECK, VERSION, CheckStatus.FAIL, (note,)), "not_cited")
        found = _exception("no_po", "po_unknown", po=number[:64])
        return MatchOutcome(CheckResult(CHECK, VERSION, CheckStatus.FAIL, (found,)), "exception")

    findings: list[Finding] = []
    if po.status != "open":
        state = PO_STATES.get(po.status, (po.status, po.status))
        findings.append(
            _exception("po_mismatch", "po_closed", status=po.status, state=state, at=po)
        )
    if facts.vendor is None:
        findings.append(_exception("po_mismatch", "po_vendor_unverified", at=po))
    elif facts.vendor.id != po.vendor_id:
        findings.append(_exception("po_mismatch", "po_vendor", severity=Severity.HIGH, at=po))
    same_currency = facts.currency == po.currency
    if not same_currency:
        findings.append(
            _exception(
                "po_mismatch",
                "po_currency",
                currency=facts.currency or "?",
                po_currency=po.currency,
                at=po,
            )
        )
    if not facts.lines:
        findings.append(_exception("unmatched_line", "lines_missing", at=po))
        return _done(findings, po, [])

    three_way = po.match_type == "3-way"
    if three_way and po.receipt_count == 0:
        findings.append(_exception("missing_receipt", "receipt_missing", at=po))
    price_tol = settings.match_price_pct
    qty_tol = settings.match_quantity_pct
    allocations: list[Allocation] = []
    expected = Decimal(0)
    lines_value = Decimal(0)
    for line, po_line in pair_lines(facts.lines, po.lines):
        n = line.index + 1
        if line.quantity is None or line.unit_price is None or line.quantity <= 0:
            findings.append(_exception("unmatched_line", "line_unreadable", line=n, at=po))
            continue
        lines_value += line.line_total if line.line_total is not None else (
            line.quantity * line.unit_price
        )  # fmt: skip
        if po_line is None:
            findings.append(_exception("unmatched_line", "line_not_on_po", line=n, at=po))
            continue
        expected += line.quantity * po_line.unit_price
        if line.unit_price * 100 > po_line.unit_price * (100 + price_tol):
            over = (
                (line.unit_price - po_line.unit_price) / po_line.unit_price * 100
                if po_line.unit_price > 0
                else Decimal(100)
            )
            findings.append(
                _exception(
                    "price_variance",
                    "price_over_po",
                    line=n,
                    po_line=po_line.line_no,
                    price=_num(line.unit_price),
                    po_price=_num(po_line.unit_price),
                    over=_pct(over),
                    tolerance=_num(price_tol),
                    at=po,
                )
            )
        before = po.invoiced_before.get(po_line.line_no, Decimal(0))
        if three_way and po.receipt_count > 0:
            received = po.received.get(po_line.line_no, Decimal(0))
            left = received - before
            if received <= 0:
                findings.append(
                    _exception(
                        "missing_receipt",
                        "line_not_received",
                        line=n,
                        po_line=po_line.line_no,
                        at=po,
                    )
                )
            elif line.quantity * 100 > left * (100 + qty_tol):
                findings.append(
                    _exception(
                        "quantity_variance",
                        "qty_over_received",
                        line=n,
                        po_line=po_line.line_no,
                        quantity=_num(line.quantity),
                        left=_num(max(left, Decimal(0))),
                        received=_num(received),
                        tolerance=_num(qty_tol),
                        at=po,
                    )
                )
        else:
            left = po_line.quantity - before
            if line.quantity * 100 > left * (100 + qty_tol):
                findings.append(
                    _exception(
                        "quantity_variance",
                        "qty_over_ordered",
                        line=n,
                        po_line=po_line.line_no,
                        quantity=_num(line.quantity),
                        left=_num(max(left, Decimal(0))),
                        ordered=_num(po_line.quantity),
                        tolerance=_num(qty_tol),
                        at=po,
                    )
                )
        allocations.append(Allocation(line.index, po_line.line_no, line.quantity))

    actual = facts.subtotal if facts.subtotal is not None else lines_value
    allowed = max(settings.match_total_amount, expected * settings.match_total_pct / 100)
    if same_currency and allocations and actual - expected > allowed:
        findings.append(
            _exception(
                "total_variance",
                "total_over_po",
                subtotal=amount_display(actual, po.currency),
                po_value=amount_display(expected, po.currency),
                at=po,
            )
        )
    return _done(findings, po, allocations)


def check_matching(facts: InvoiceFacts, history: History, settings: CheckSettings) -> CheckResult:
    return match_invoice(facts, history, settings).result
