# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Purchase order matching on the synthetic matching cases (``synth/pay``, brief 4.5.2).

``PoBook`` holds the synthetic POs and goods receipts as Pay would have them on file,
and what earlier invoices of each PO took. Invoices are matched in the order they
arrive (invoice date, then id), so the second invoice of a PO sees what the first
took. Attack invoices (duplicates of PO invoices and the like) are matched too but take
nothing: a person rejects them, and a rejected invoice takes nothing from its PO.

Units of the metrics: each matching case (its invoices together), plus each regular
invoice that is in no case (a vendor without POs: it must not be flagged).

* ``exception_recall``: cases that expect an exception where the expected exception
  code was raised.
* ``exception_precision``: units flagged with any exception that expect that exception.
* ``match_rate``: cases that expect a match where every invoice matched.
* ``exact_code_rate``: exception cases that raised exactly the expected codes (a price
  variance also raising a total variance, say, counts as not exact).

The read values are the synthetic ground truth (a perfect reader).
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pairly_pay.checks.matching import Allocation, MatchOutcome, match_invoice
from pairly_pay.checks.settings import CheckSettings
from pairly_pay.checks.types import History, InvoiceFacts, Line, PoFacts, PoLineFacts, VendorFacts
from synth.pay.dataset import Dataset, DocSpec
from synth.pay.models import Vendor


def vendor_facts(v: Vendor) -> VendorFacts:
    return VendorFacts(
        id=v.id,
        name=v.legal_name,
        country=v.country,
        trn=v.trn,
        category=v.category,
        email_domains=tuple(d.lower() for d in v.email_domains),
        iban_on_file=v.bank.iban,
    )


def truth_facts(spec: DocSpec, vendors: dict[str, VendorFacts]) -> InvoiceFacts:
    """What a perfect reader takes from the invoice (enough for matching)."""
    t = spec.record.truth
    date = dt.date.fromisoformat(t.invoice_date)
    return InvoiceFacts(
        id=spec.record.id,
        received_at=dt.datetime.combine(date, dt.time(9), dt.UTC),
        vendor=vendors.get(t.vendor_id or ""),
        invoice_number=t.invoice_number,
        invoice_date=date,
        currency=t.currency,
        po_number=t.po_number,
        subtotal=Decimal(t.subtotal),
        total=Decimal(t.total),
        lines=tuple(
            Line(
                index=i,
                description=x.description,
                quantity=Decimal(x.quantity),
                unit_price=Decimal(x.unit_price),
                line_total=Decimal(x.line_total),
            )
            for i, x in enumerate(t.lines)
        ),
    )


@dataclass
class PoBook:
    """The synthetic POs and receipts on file, and what invoices took so far."""

    ds: Dataset
    taken: dict[str, dict[int, Decimal]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.pos = {p.po_number: p for p in self.ds.purchase_orders}
        self.receipts: dict[str, list[Any]] = defaultdict(list)
        for r in self.ds.receipts:
            self.receipts[r.po_number].append(r)
        self.vendors_with_pos = {p.vendor_id for p in self.ds.purchase_orders}

    def po_facts(self, number: str | None) -> PoFacts | None:
        po = self.pos.get((number or "").strip())
        if po is None:
            return None
        received: dict[int, Decimal] = defaultdict(Decimal)
        for receipt in self.receipts.get(po.po_number, []):
            for line in receipt.lines:
                received[line.po_line_no] += Decimal(line.quantity_received)
        return PoFacts(
            id=po.po_number,
            number=po.po_number,
            vendor_id=po.vendor_id,
            currency=po.currency,
            status="open",
            match_type=po.match_type,
            lines=tuple(
                PoLineFacts(x.line_no, x.description, Decimal(x.quantity), Decimal(x.unit_price))
                for x in po.lines
            ),
            received=dict(received),
            receipt_count=len(self.receipts.get(po.po_number, [])),
            invoiced_before=dict(self.taken.get(po.po_number, {})),
        )

    def history(self, facts: InvoiceFacts, base: History | None = None) -> History:
        base = base or History()
        vendor = facts.vendor.id if facts.vendor else None
        return History(
            vendor_invoices=base.vendor_invoices,
            same_file=base.same_file,
            same_file_documents=base.same_file_documents,
            vendor_domains=base.vendor_domains,
            seen_domains=base.seen_domains,
            purchase_order=self.po_facts(facts.po_number),
            vendor_uses_pos=vendor in self.vendors_with_pos,
            org_uses_pos=bool(self.pos),
        )

    def take(self, po_id: str | None, allocations: tuple[Allocation, ...]) -> None:
        if po_id is None:
            return
        took = self.taken.setdefault(po_id, {})
        for a in allocations:
            took[a.po_line_no] = took.get(a.po_line_no, Decimal(0)) + a.quantity


def exceptions_of(outcome: MatchOutcome) -> set[str]:
    return {f.code for f in outcome.result.findings if f.needs_review}


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def evaluate(ds: Dataset, settings: CheckSettings | None = None) -> dict[str, Any]:
    """Match every regular invoice in arrival order and measure against the cases."""
    settings = settings or CheckSettings()
    vendors = {v.id: vendor_facts(v) for v in ds.vendors}
    book = PoBook(ds)
    outcomes: dict[str, MatchOutcome] = {}
    errors: list[str] = []
    regular = sorted(ds.regular(), key=lambda s: (s.record.truth.invoice_date, s.record.id))
    for spec in regular:
        facts = truth_facts(spec, vendors)
        try:
            outcome = match_invoice(facts, book.history(facts), settings)
        except Exception as exc:  # recorded, never hidden
            errors.append(f"{spec.record.id}:{type(exc).__name__}")
            continue
        outcomes[spec.record.id] = outcome
        book.take(outcome.po_id, outcome.allocations)
    return measure(ds, outcomes, errors)


def measure(ds: Dataset, outcomes: dict[str, MatchOutcome], errors: list[str]) -> dict[str, Any]:
    in_case = {i for c in ds.cases for i in c.invoice_ids}
    positives = tp = flagged = matched_cases = match_cases = exact = 0
    extra: Counter[str] = Counter()
    missed: list[str] = []
    wrong: list[str] = []
    by_scenario: dict[str, Counter[str]] = defaultdict(Counter)
    for case in ds.cases:
        found: set[str] = set()
        all_matched = True
        for invoice_id in case.invoice_ids:
            outcome = outcomes.get(invoice_id)
            if outcome is None:
                all_matched = False
                found.add("error")
                continue
            found |= exceptions_of(outcome)
            all_matched &= outcome.matched
        expect_exception = case.expected == "exception"
        correct = expect_exception and set(case.expected_exceptions) <= found
        by_scenario[case.scenario]["cases"] += 1
        by_scenario[case.scenario]["as_expected"] += int(
            correct if expect_exception else (all_matched and not found)
        )
        if expect_exception:
            positives += 1
            tp += int(correct)
            exact += int(found == set(case.expected_exceptions))
            extra.update(found - set(case.expected_exceptions))
            if not correct:
                missed.append(f"{case.id}:{case.scenario}:{','.join(sorted(found)) or '-'}")
        else:
            match_cases += 1
            matched_cases += int(all_matched and not found)
            if found or not all_matched:
                wrong.append(f"{case.id}:{case.scenario}:{','.join(sorted(found)) or '-'}")
        if found:
            flagged += 1
    loose = [i for i in outcomes if i not in in_case]
    loose_flagged = sorted(i for i in loose if exceptions_of(outcomes[i]))
    flagged += len(loose_flagged)
    return {
        "cases": len(ds.cases),
        "invoices": len(outcomes),
        "exception_cases": positives,
        "match_cases": match_cases,
        "exception_recall": _ratio(tp, positives),
        "exception_precision": _ratio(tp, flagged),
        "match_rate": _ratio(matched_cases, match_cases),
        # Exception cases whose codes are exactly the expected ones (no extra codes).
        "exact_code_rate": _ratio(exact, positives),
        "extra_codes": dict(sorted(extra.items())),
        "flagged_units": flagged,
        "invoices_without_case": len(loose),
        "invoices_without_case_flagged": loose_flagged[:40],
        "missed": missed[:40],
        "wrongly_flagged_matches": wrong[:40],
        "by_scenario": {
            k: {"cases": v["cases"], "as_expected": v["as_expected"]}
            for k, v in sorted(by_scenario.items())
        },
        "errors": errors[:40],
        "error_count": len(errors),
        "tolerances": {
            "price_pct": str(CheckSettings().match_price_pct),
            "quantity_pct": str(CheckSettings().match_quantity_pct),
            "total_pct": str(CheckSettings().match_total_pct),
            "total_amount": str(CheckSettings().match_total_amount),
        },
    }
