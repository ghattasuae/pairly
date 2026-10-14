# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The Pay demo seed's choices (``scripts/demo_pay.py``), without a database.

* The history has every vendor that has regular invoices before the cut-off, at most
  ``HISTORY_PER_VENDOR`` each, and the near-duplicate demo invoice's original; never an
  invoice the seed uploads, nor a demo invoice.
* The invoices driven to a status and the received one are recorded (``synth.pay``
  recordings), so they read offline; none belongs to a demo or ticket vendor.
* Every Pay status the demo needs is covered.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

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
