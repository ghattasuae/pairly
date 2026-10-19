# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The Pay demo seed's choices (``scripts/demo_pay.py``), without a database.

* The history has every vendor that has regular invoices before the cut-off, at most
  ``HISTORY_PER_VENDOR`` each, and the near-duplicate demo invoice's original; never an
  invoice the seed uploads, nor a demo invoice.
* The invoices driven to a status and the received one are recorded (``synth.pay``
  recordings), so they read offline; none belongs to a demo or ticket vendor.
* Every Pay status the demo needs is covered.
* The history's bank statement is a valid statement CSV that the real matcher explains
  every history payment with, except the few deliberately left out (paid long before,
  so they are listed as unexplained), and whose extra debits stay unmatched; the same
  history always gives the same file.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
import uuid
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

from pairly_pay.reconciliation import ItemFact, LineFact, match_lines, ref_key
from pairly_pay.statements import parse_csv
from synth.pay.dataset import build
from synth.pay.recordings import subset_specs

SCRIPT = Path(__file__).resolve().parents[1] / "demo_pay.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("demo_pay", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_pay"] = module
    spec.loader.exec_module(module)
    return module


demo_pay = _load()


def test_history_has_the_originals_and_never_a_seeded_invoice() -> None:
    ds = build()
    history = demo_pay.history_specs(ds)
    ids = {s.record.id for s in history}
    originals = {
        s.record.attack.original_id
        for s in ds.docs
        if s.record.demo and s.record.attack and s.record.attack.original_id
    }
    assert originals
    assert originals <= ids
    seeded = {t.doc_id for t in demo_pay.TARGETS} | {demo_pay.RECEIVED}
    demo = {s.record.id for s in ds.docs if s.record.demo}
    assert not ids & (seeded | demo)
    per_vendor = Counter(s.record.truth.vendor_id for s in history)
    assert max(per_vendor.values()) <= demo_pay.HISTORY_PER_VENDOR + len(originals)
    assert len(per_vendor) >= 30
    assert all(s.record.format in demo_pay.HISTORY_FORMATS for s in history)


def test_seeded_invoices_are_recorded_and_cover_every_status() -> None:
    ds = build()
    recorded = {s.record.id for s in subset_specs(ds)}
    seeded = [t.doc_id for t in demo_pay.TARGETS] + [demo_pay.RECEIVED]
    assert set(seeded) <= recorded
    assert len(set(seeded)) == len(seeded)
    avoid = {"VEN-015", "VEN-021", "VEN-022", demo_pay.TICKET_WAITING, demo_pay.TICKET_COOLING}
    assert not {ds.doc(d).record.truth.vendor_id for d in seeded} & avoid
    statuses = {t.status for t in demo_pay.TARGETS} | {"scheduled", "paid", "received"}
    assert statuses >= {
        "received",
        "needs_review",
        "ready",
        "awaiting_approval",
        "approved",
        "scheduled",
        "paid",
        "rejected",
    }
    # The payment runs are in one currency (AED).
    for doc_id in (*demo_pay.PAID, *demo_pay.SCHEDULED):
        assert ds.doc(doc_id).record.truth.currency == "AED"


def test_next_steps_name_the_drop_command() -> None:
    seed = demo_pay.PaySeed(address="pay+demo.x@inbound.test", emails=[Path("/x/1-clean.eml")])
    text = "\n".join(demo_pay.next_steps(seed, "http://localhost:3100"))
    assert "make inbound-drop FILE=/x/1-clean.eml" in text
    assert "pay+demo.x@inbound.test" in text
    assert "—" not in text


def test_the_clean_demo_invoice_has_its_po_and_is_under_the_rule_limit() -> None:
    ds = build()
    clean = next(s for s in ds.docs if s.record.demo == "clean")
    assert clean.record.id == demo_pay.CLEAN_DOC
    numbers = demo_pay.po_numbers(ds)
    assert len(set(numbers)) == len(numbers)
    on_file = {p.po_number: p for p in ds.purchase_orders}
    assert set(numbers) <= set(on_file)
    clean_po = on_file[clean.record.truth.po_number or ""]
    assert clean_po.match_type == "3-way"
    assert any(r.po_number == clean_po.po_number for r in ds.receipts)
    assert clean.record.truth.currency == "AED"
    assert Decimal(clean.record.truth.total) < Decimal(demo_pay.RULE_LIMIT)
    # The invoice that waits for the approver stays over the limit (a person decides).
    waiting = [t.doc_id for t in demo_pay.TARGETS if t.status == "awaiting_approval"]
    assert all(
        Decimal(ds.doc(d).record.truth.total) >= Decimal(demo_pay.RULE_LIMIT) for d in waiting
    )
    seeded = {t.doc_id for t in demo_pay.TARGETS}
    assert set(demo_pay.PO_DOCS) - {demo_pay.CLEAN_DOC} <= seeded


def _history_payments(ds: Any) -> tuple[list[Any], list[ItemFact]]:
    """The history payments as a fresh seed numbers them (``_paid_history``: per
    currency run, in history order), and the matcher's view of each."""
    payments: list[Any] = []
    facts: list[ItemFact] = []
    per_run: Counter[str] = Counter()
    run_ids: dict[str, uuid.UUID] = {}
    for n, spec in enumerate(demo_pay.history_specs(ds), start=1):
        truth = spec.record.truth
        per_run[truth.currency] += 1
        end_to_end = f"HIST-{truth.currency}-{per_run[truth.currency]:05d}"
        _received, _approved, paid = demo_pay.history_times(truth.invoice_date, truth.due_date)
        amount = Decimal(truth.total)
        payments.append(
            demo_pay.HistoryPayment(
                end_to_end, truth.invoice_number, amount, truth.currency, paid.date()
            )
        )
        run_id = run_ids.setdefault(truth.currency, uuid.uuid4())
        facts.append(
            ItemFact(
                id=uuid.uuid4(),
                run_id=run_id,
                run_reference=f"HIST-{truth.currency}",
                invoice_id=uuid.uuid4(),
                amount=amount,
                currency=truth.currency,
                keys=tuple(
                    k
                    for k in (
                        ref_key(end_to_end),
                        ref_key(f"HIST-{n:04d}"),
                        ref_key(truth.invoice_number),
                    )
                    if k
                ),
                iban_bidx=None,
                run_paid_count=0,
            )
        )
    for i, fact in enumerate(facts):
        count = sum(1 for f in facts if f.run_id == fact.run_id)
        facts[i] = dataclasses.replace(fact, run_paid_count=count)
    return payments, facts


def test_the_history_statement_explains_all_but_the_ones_left_out() -> None:
    ds = build()
    payments, facts = _history_payments(ds)
    statement = demo_pay.history_statement(payments)
    assert statement.csv == demo_pay.history_statement(list(reversed(payments))).csv
    assert set(statement.left_out) == demo_pay.STATEMENT_LEFT_OUT
    assert 2 <= len(statement.left_out) <= 3
    assert statement.lines == len(payments) - len(statement.left_out) + len(
        demo_pay.STATEMENT_EXTRA
    )

    parsed = parse_csv(statement.csv)
    lines = [
        LineFact(uuid.uuid4(), x.booking_date, x.amount, x.currency, x.reference, None)
        for x in parsed.lines
    ]
    result = match_lines(lines, facts)
    by_line = {line.id: line for line in lines}
    extra_debits = {r for _d, a, _c, r in demo_pay.STATEMENT_EXTRA if a.startswith("-")}
    assert {by_line[i].reference for i in result.unmatched} == extra_debits
    matched_items = {m.item_id for m in result.matches}
    end_to_end = {f.id: p.end_to_end_id for f, p in zip(facts, payments, strict=True)}
    unexplained = {end_to_end[f.id] for f in facts if f.id not in matched_items}
    assert unexplained == demo_pay.STATEMENT_LEFT_OUT

    # Each line is booked a few days after its payment, and the ones left out were paid
    # long enough before the cut-off to be listed as unexplained.
    paid_on = {p.end_to_end_id: p.paid_on for p in payments}
    for match in result.matches:
        line = by_line[match.line_id]
        assert match.item_id is not None
        assert 1 <= (line.booking_date - paid_on[end_to_end[match.item_id]]).days <= 4
    cutoff = demo_pay.dt.date.fromisoformat(demo_pay.HISTORY_BEFORE)
    assert all((cutoff - paid_on[e]).days > 30 for e in demo_pay.STATEMENT_LEFT_OUT)
