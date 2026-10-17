# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Live Pay suite ``pay-extraction``: invoice extraction with a real model.

The eval subset of ``synth.pay.recordings`` (per format and layout, the first invoices
by id, then the demo trio): each invoice is rendered, parsed with the engine's parser,
scanned and guarded exactly as the ``pay_invoice`` graph does, then read by
``pairly_core.extraction.engine.extract`` with the ``PayInvoice`` schema and the live
router. What passes the engine's checks (no flag) is compared with the ground truth,
with the same rules as the scripted accuracy (``synth.pay.recordings.accuracy``).
"""

from __future__ import annotations

import time
from typing import Any

from evals.live.harness import (
    ORG,
    CapBudget,
    LiveCallStore,
    LiveRouter,
    Meter,
    run_docs,
    run_status,
)
from pairly_core.extraction.engine import ExtractionResult, extract
from pairly_pay.invoice_schema import PayInvoice
from synth.pay.dataset import DocSpec, build
from synth.pay.recordings import Item, _expected, demo_specs, materialise, subset_specs


def _pct(a: int, b: int) -> float | None:
    return round(100 * a / b, 1) if b else None


def score(items: list[tuple[Item, ExtractionResult[PayInvoice]]]) -> dict[str, Any]:
    header_total = header_ok = line_total = line_ok = totals_ok = vat_ok = ready = 0
    lines_found = lines_expected = 0
    by_field: dict[str, list[int]] = {}
    needs_human: dict[str, list[str]] = {}
    for item, result in items:
        ready += int(not result.needs_human)
        if result.needs_human:
            needs_human[item.id] = list(result.reasons)
        got = {f.path: f.value for f in result.fields if not f.flags}
        header, lines = _expected(item.spec.record.truth)
        for name, want in header.items():
            ok = got.get(name) == want
            header_total += 1
            header_ok += ok
            stats = by_field.setdefault(name, [0, 0])
            stats[0] += ok
            stats[1] += 1
        totals_ok += got.get("total") == header["total"]
        vat_ok += got.get("vat_amount") == header["vat_amount"]
        lines_expected += len(lines)
        lines_found += sum(
            1 for i in range(len(lines)) if any(p.startswith(f"line_items[{i}].") for p in got)
        )
        for index, line in enumerate(lines):
            for name, want in line.items():
                ok = got.get(f"line_items[{index}].{name}") == want
                line_total += 1
                line_ok += ok
                stats = by_field.setdefault(f"line_items[].{name}", [0, 0])
                stats[0] += ok
                stats[1] += 1
    n = len(items)
    return {
        "what": (
            "Percent of ground-truth values the live model returned and the engine accepted "
            "(no flag), as synth.pay.recordings.accuracy scores the scripted answers."
        ),
        "invoices": n,
        "header_fields": header_total,
        "header_field_accuracy_pct": _pct(header_ok, header_total),
        "line_item_fields": line_total,
        "line_item_field_accuracy_pct": _pct(line_ok, line_total),
        "total_accuracy_pct": _pct(totals_ok, n),
        "vat_accuracy_pct": _pct(vat_ok, n),
        "extraction_ready": ready,
        "extraction_ready_pct": _pct(ready, n),
        "line_items_expected": lines_expected,
        "line_items_with_an_accepted_value": lines_found,
        "by_field_pct": {
            k: {"n": v[1], "pct": _pct(v[0], v[1])} for k, v in sorted(by_field.items())
        },
        "needs_human": needs_human,
    }


async def suite_pay_extraction(
    router: LiveRouter, store: LiveCallStore, budget: CapBudget, meter: Meter, limit: int | None
) -> dict[str, Any]:
    ds = build()
    specs: list[DocSpec] = subset_specs(ds)
    if limit is not None:
        specs = specs[:limit]
    by_format: dict[str, int] = {}
    for spec in specs:
        by_format[spec.record.format] = by_format.get(spec.record.format, 0) + 1
    items: dict[str, Item] = {}

    async def one(spec: Any) -> ExtractionResult[PayInvoice]:
        item = materialise(spec, ds)
        items[item.id] = item
        return await extract(item.doc, item.guarded, PayInvoice, router=router, org_id=ORG)

    started = time.monotonic()
    outputs, errors, cut, stopped = await run_docs(specs, meter, budget, one)
    measured = [(items[k], v) for k, v in outputs.items()]
    # The subset takes the demo trio last (two of them are attack invoices): the eval
    # subset proper is everything before it.
    demo = {s.record.id for s in demo_specs(ds)}
    return {
        "status": run_status(
            stopped_by_cap=stopped, measured=len(measured), planned=len(specs), errors=len(errors)
        ),
        "planned_documents": len(specs),
        "planned_by_format": by_format,
        "measured_documents": len(measured),
        "stopped_by_cost_cap": stopped,
        "cut_by_cap": cut,
        "errors": errors,
        "documents": sorted(outputs),
        "accuracy": score(measured),
        "accuracy_eval_subset_without_demo_trio": score(
            [(i, r) for i, r in measured if i.id not in demo]
        ),
        "usage": meter.summary(),
        "seconds": round(time.monotonic() - started, 1),
    }
