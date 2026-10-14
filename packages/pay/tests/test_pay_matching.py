# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""2-way and 3-way purchase order matching (brief 4.5.2), pure.

* Within tolerance (price 2%, quantity 5%) matches; just outside is an exception, and
  every tolerance is configurable. Paying less than the PO is not a variance.
* Partial deliveries invoiced as received match; a second invoice of the PO takes what
  is left and no more.
* Missing receipt, no PO (required or not), unknown PO, another vendor's PO, another
  currency, a closed PO, a line not on the PO, charges above the PO total.
* Lines pair by description, in order, or by nearest price (Arabic invoices).
* Every exception sends the invoice to a person, with a sentence in English and Arabic.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

from pairly_core.risk.types import Severity
from pairly_pay.checks.matching import MatchOutcome, match_invoice, pair_lines
from pairly_pay.checks.run import run_pure_checks
from pairly_pay.checks.settings import CheckSettings, parse_settings
from pairly_pay.checks.types import (
    CheckStatus,
    History,
    InvoiceFacts,
    Line,
    PoFacts,
    PoLineFacts,
    VendorFacts,
)

D = Decimal
SETTINGS = CheckSettings()
VENDOR = VendorFacts("ven-1", "Synthetic Supplies LLC", "AE", None, "office", ("s.example",), None)
OTHER = replace(VENDOR, id="ven-2")


def po(**changes: object) -> PoFacts:
    base = PoFacts(
        id="po-1",
        number="PO-2026-0079",
        vendor_id="ven-1",
        currency="AED",
        status="open",
        match_type="3-way",
        lines=(
            PoLineFacts(1, "Paper A4", D(100), D("12.50")),
            PoLineFacts(2, "Toner cartridge", D(4), D(310)),
        ),
        received={1: D(100), 2: D(4)},
        receipt_count=1,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def invoice(*lines: tuple[str, str, str], **changes: object) -> InvoiceFacts:
    rows = tuple(
        Line(i, desc, D(q), D(p), line_total=D(q) * D(p)) for i, (desc, q, p) in enumerate(lines)
    )
    base = InvoiceFacts(
        id="inv-1",
        received_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
        vendor=VENDOR,
        currency="AED",
        po_number="PO-2026-0079",
        subtotal=sum((r.line_total or D(0) for r in rows), D(0)),
        lines=rows,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


EXACT = (("Paper A4", "100", "12.50"), ("Toner cartridge", "4", "310"))


def run(
    facts: InvoiceFacts, order: PoFacts | None = None, *, vendor_uses_pos: bool = False
) -> MatchOutcome:
    history = History(purchase_order=order, vendor_uses_pos=vendor_uses_pos, org_uses_pos=True)
    return match_invoice(facts, history, SETTINGS)


def codes(outcome: MatchOutcome) -> set[str]:
    return {f.code for f in outcome.result.findings if f.needs_review}


def rules(outcome: MatchOutcome) -> set[str]:
    return {f.rule for f in outcome.result.findings}


def test_exact_three_way_match_records_the_allocation() -> None:
    outcome = run(invoice(*EXACT), po())
    assert outcome.matched
    assert outcome.result.status is CheckStatus.PASS
    assert outcome.po_id == "po-1"
    assert [(a.invoice_line, a.po_line_no, a.quantity) for a in outcome.allocations] == [
        (0, 1, D(100)),
        (1, 2, D(4)),
    ]
    (note,) = outcome.result.findings
    assert note.rule == "po_matched"
    assert note.severity is Severity.INFO
    assert "PO-2026-0079" in note.message_en
    assert "PO-2026-0079" in note.message_ar
    assert outcome.as_json()["lines"][0] == {"invoice_line": 0, "po_line_no": 1, "quantity": "100"}


def test_price_tolerance_is_two_percent_and_configurable() -> None:
    inside = run(invoice(("Paper A4", "100", "12.75"), EXACT[1]), po())  # +2.0%
    assert inside.matched
    outside = run(invoice(("Paper A4", "100", "12.76"), EXACT[1]), po())  # +2.08%
    assert codes(outside) == {"price_variance"}
    (found,) = [f for f in outside.result.findings if f.code == "price_variance"]
    assert found.forces_review
    assert found.evidence["po_line"] == 1
    assert "2.08%" in found.message_en
    assert "2.08" in found.message_ar
    cheaper = run(invoice(("Paper A4", "100", "11.00"), EXACT[1]), po())
    assert cheaper.matched
    looser = replace(SETTINGS, match_price_pct=D(3))
    assert match_invoice(
        invoice(("Paper A4", "100", "12.76"), EXACT[1]), History(purchase_order=po()), looser
    ).matched
    assert parse_settings({"match_price_pct": "3", "require_po": True}).require_po is True
    assert parse_settings({"match_price_pct": "-1"}).match_price_pct == D(2)


def test_quantity_against_received_and_partial_deliveries() -> None:
    partial = po(received={1: D(60), 2: D(4)})
    assert run(invoice(("Paper A4", "60", "12.50"), EXACT[1]), partial).matched
    assert run(invoice(("Paper A4", "63", "12.50"), EXACT[1]), partial).matched  # +5%
    over = run(invoice(("Paper A4", "100", "12.50"), EXACT[1]), partial)
    assert codes(over) == {"quantity_variance"}
    assert "qty_over_received" in rules(over)
    # A second invoice takes what is left: 40 more after the first took 60.
    second = po(received={1: D(100), 2: D(4)}, invoiced_before={1: D(60), 2: D(4)})
    assert run(invoice(("Paper A4", "40", "12.50")), second).matched
    assert codes(run(invoice(("Paper A4", "45", "12.50")), second)) == {"quantity_variance"}
    # Nothing left of the toner line.
    assert codes(run(invoice(EXACT[1]), second)) == {"quantity_variance"}


def test_two_way_checks_against_ordered() -> None:
    services = po(match_type="2-way", received={}, receipt_count=0)
    assert run(invoice(*EXACT), services).matched
    assert run(invoice(("Paper A4", "104", "12.50"), EXACT[1]), services).matched
    over = run(invoice(("Paper A4", "106", "12.50"), EXACT[1]), services)
    assert "qty_over_ordered" in rules(over)


def test_missing_receipt() -> None:
    none = run(invoice(*EXACT), po(received={}, receipt_count=0))
    assert codes(none) == {"missing_receipt"}
    assert "receipt_missing" in rules(none)
    line = run(invoice(*EXACT), po(received={1: D(100)}))
    assert codes(line) == {"missing_receipt"}
    assert "line_not_received" in rules(line)


def test_no_po_unknown_po_and_when_a_po_is_required() -> None:
    plain = run(invoice(*EXACT, po_number=None))
    assert plain.status == "not_cited"
    assert plain.result.status is CheckStatus.FAIL
    assert not plain.result.needs_review  # information only: a person approves anyway
    required = run(invoice(*EXACT, po_number=None), vendor_uses_pos=True)
    assert codes(required) == {"no_po"}
    assert "has purchase orders on file" in required.result.findings[0].message_en
    by_settings = match_invoice(
        invoice(*EXACT, po_number=" "), History(), replace(SETTINGS, require_po=True)
    )
    assert codes(by_settings) == {"no_po"}
    unknown = run(invoice(*EXACT, po_number="PO-2026-9123"))
    assert codes(unknown) == {"no_po"}
    assert rules(unknown) == {"po_unknown"}
    assert unknown.po_id is None
    # An organisation that keeps no POs in Pairly yet: a note, not an exception.
    before = match_invoice(invoice(*EXACT), History(), SETTINGS)
    assert before.status == "not_cited"
    assert rules(before) == {"po_not_on_file"}
    assert not before.result.needs_review


def test_the_po_must_be_this_vendors_open_po_in_the_same_currency() -> None:
    other = run(invoice(*EXACT, vendor=OTHER), po())
    (found,) = [f for f in other.result.findings if f.rule == "po_vendor"]
    assert found.severity is Severity.HIGH
    assert "po_vendor_unverified" in rules(run(invoice(*EXACT, vendor=None), po()))
    assert "po_currency" in rules(run(invoice(*EXACT, currency="USD"), po()))
    closed = run(invoice(*EXACT), po(status="cancelled"))
    assert "po_closed" in rules(closed)
    assert "ملغى" in next(f for f in closed.result.findings if f.rule == "po_closed").message_ar


def test_extra_lines_and_charges_above_the_po() -> None:
    extra = run(invoice(*EXACT, ("Delivery fee", "1", "250")), po())
    assert {"unmatched_line", "total_variance"} <= codes(extra)
    assert "line_not_on_po" in rules(extra)
    padded = run(replace(invoice(*EXACT), subtotal=D(2490) + D(60)), po())
    assert codes(padded) == {"total_variance"}
    rounding = run(replace(invoice(*EXACT), subtotal=D("2490.01")), po())
    assert rounding.matched
    assert "lines_missing" in rules(run(invoice(), po()))
    unreadable = invoice(*EXACT)
    unreadable = replace(unreadable, lines=(replace(unreadable.lines[0], quantity=None),))
    assert "line_unreadable" in rules(run(unreadable, po()))


def test_lines_pair_by_description_order_or_nearest_price() -> None:
    order = po()
    by_order = pair_lines(invoice(("ورق", "100", "12.50"), ("حبر", "4", "310")).lines, order.lines)
    assert [p.line_no if p else None for _, p in by_order] == [1, 2]
    reordered = pair_lines(invoice(EXACT[1], EXACT[0]).lines, order.lines)
    assert [p.line_no if p else None for _, p in reordered] == [2, 1]
    nearest = pair_lines(invoice(("حبر", "2", "305")).lines, order.lines)
    assert [p.line_no if p else None for _, p in nearest] == [2]
    far = pair_lines(invoice(("شيء", "2", "5000")).lines, order.lines)
    assert [p for _, p in far] == [None]


def test_matching_is_one_of_the_pure_checks_and_fails_closed() -> None:
    results = run_pure_checks(
        invoice(*EXACT), History(purchase_order=po()), SETTINGS, document_required=False
    )
    assert results["matching"].status is CheckStatus.PASS
    broken = run_pure_checks(
        invoice(*EXACT),
        History(purchase_order=po(lines=None)),  # type: ignore[arg-type]
        SETTINGS,
        document_required=False,
    )
    assert broken["matching"].status is CheckStatus.ERROR
    assert broken["matching"].needs_review
