# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``uv run python -m synth.pay``: the synthetic Pay dataset.

    python -m synth.pay generate --out <dir> [--count N] [--seed S]
        Render every document into <dir> (invoices/, attacks/, emails/) and write
        <dir>/index.json: every invoice with its file and ground truth, the vendors,
        purchase orders, receipts, matching cases and emails.
    python -m synth.pay manifests [--count N] [--seed S]
        Rewrite the committed manifests (synth/pay/manifests/).
    python -m synth.pay --check [--count N] [--seed S]
        Rebuild the manifests in memory and fail when a committed one differs from what
        the generators produce now. Nothing is rendered.

    python -m synth.pay recordings [--check] [--report]
        Write the recorded (scripted) extraction responses for the eval subset and the
        demo trio to packages/pay/pairly_pay/data/recordings.json; --check compares
        instead, --report prints the scripted accuracy (synth/pay/recordings.py).

The committed manifests are for the defaults (320 regular invoices, seed 2026).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel

from synth.pay.attacks import APPROVAL_THRESHOLD
from synth.pay.banking import BANKS
from synth.pay.dataset import DEFAULT_COUNT, DEFAULT_SEED, TOLERANCES, Dataset, build
from synth.pay.emails import render_email

MANIFEST_DIR: Final = Path(__file__).resolve().parent / "manifests"
ROOT: Final = Path(__file__).resolve().parents[2]
GENERATOR_VERSION: Final = "1"


def _line(model: BaseModel) -> str:
    return json.dumps(
        model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _jsonl(models: list[Any]) -> str:
    return "\n".join(_line(m) for m in models) + "\n"


def stats(ds: Dataset) -> dict[str, Any]:
    records = [s.record for s in ds.docs]
    regular = [r for r in records if r.kind == "invoice"]
    attacks = [r for r in records if r.attack is not None]

    def count(key: Any, items: Any) -> dict[str, int]:
        return dict(sorted(Counter(key(r) for r in items).items()))

    types: Counter[str] = Counter()
    findings: Counter[str] = Counter()
    for r in attacks:
        if r.attack is not None:
            types.update(r.attack.types)
            findings.update(r.attack.expected_findings)
    return {
        "vendors": len(ds.vendors),
        "vendors_by_country": count(lambda v: v.country, ds.vendors),
        "unknown_vendors": len(ds.unknown_vendors),
        "invoices": len(regular),
        "attacks": len(attacks),
        "invoices_by_format": count(lambda r: r.format, regular),
        "invoices_by_language": count(lambda r: r.language, regular),
        "invoices_by_layout": count(lambda r: r.layout, regular),
        "invoices_by_currency": count(lambda r: r.truth.currency, regular),
        "invoices_by_channel": count(lambda r: r.channel, regular),
        "invoices_by_text_layer": count(lambda r: r.text_layer, regular),
        "invoices_with_po": sum(1 for r in regular if r.truth.po_number),
        "invoice_lines": sum(len(r.truth.lines) for r in regular),
        "zero_rated_lines": sum(1 for r in regular for x in r.truth.lines if x.vat_category == "Z"),
        "attacks_by_format": count(lambda r: r.format, attacks),
        "attack_types": dict(sorted(types.items())),
        "attack_findings": dict(sorted(findings.items())),
        "purchase_orders": len(ds.purchase_orders),
        "goods_receipts": len(ds.receipts),
        "match_cases": len(ds.cases),
        "match_scenarios": count(lambda c: c.scenario, ds.cases),
        "match_expected": count(lambda c: c.expected, ds.cases),
        "emails": len(ds.emails),
        "emails_by_kind": count(lambda e: e.kind, ds.emails),
        "demo": {r.demo: r.id for r in records if r.demo},
    }


def manifests(count: int, seed: int) -> dict[Path, str]:
    ds = build(count, seed)
    regular = [s.record for s in ds.docs if s.record.kind == "invoice"]
    attacks = [s.record for s in ds.docs if s.record.kind == "attack"]
    vendors = {
        "vendors": [v.model_dump(mode="json") for v in ds.vendors],
        "unknown_vendors": [v.model_dump(mode="json") for v in ds.unknown_vendors],
    }
    summary = {
        "generator_version": GENERATOR_VERSION,
        "count": count,
        "seed": seed,
        "synthetic": "every vendor, bank, IBAN, TRN, address, person and invoice is generated",
        "matching_tolerances": TOLERANCES,
        "approval_threshold_aed": str(APPROVAL_THRESHOLD),
        "banks": [
            {"key": b.key, "name": b.name, "country": b.country, "code": b.code, "bic": b.bic}
            for b in BANKS
        ],
        "stats": stats(ds),
    }
    return {
        MANIFEST_DIR / "vendors.json": json.dumps(vendors, ensure_ascii=False, indent=1) + "\n",
        MANIFEST_DIR / "invoices.jsonl": _jsonl(regular),
        MANIFEST_DIR / "attacks.jsonl": _jsonl(attacks),
        MANIFEST_DIR / "purchase_orders.jsonl": _jsonl(ds.purchase_orders),
        MANIFEST_DIR / "goods_receipts.jsonl": _jsonl(ds.receipts),
        MANIFEST_DIR / "matching.jsonl": _jsonl(ds.cases),
        MANIFEST_DIR / "emails.jsonl": _jsonl(ds.emails),
        MANIFEST_DIR / "summary.json": json.dumps(summary, ensure_ascii=False, indent=1) + "\n",
    }


def check(count: int, seed: int) -> int:
    stale = []
    for path, text in manifests(count, seed).items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != text:
            stale.append(str(path.relative_to(ROOT)))
    if stale:
        print("out of date (run: uv run python -m synth.pay manifests):", file=sys.stderr)
        for name in stale:
            print(f"  {name}", file=sys.stderr)
        return 1
    print("synth.pay manifests match the generators")
    return 0


def write_manifests(count: int, seed: int) -> int:
    for path, text in manifests(count, seed).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


def generate(out: Path, count: int, seed: int) -> int:
    started = time.monotonic()
    ds = build(count, seed)
    out.mkdir(parents=True, exist_ok=True)
    blobs: dict[str, bytes] = {}
    total = files = 0
    for spec in ds.docs:
        data = spec.render()
        blobs[spec.record.id] = data
        if spec.record.format == "email_body":
            continue  # the email is the file
        path = out / spec.record.file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        total += len(data)
        files += 1
    for email in ds.emails:
        data = render_email(email, ds, blobs)
        path = out / email.file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        total += len(data)
        files += 1
    summary = stats(ds)
    index = {
        "count": count,
        "seed": seed,
        "stats": summary,
        "vendors": [v.model_dump(mode="json") for v in ds.vendors],
        "unknown_vendors": [v.model_dump(mode="json") for v in ds.unknown_vendors],
        "documents": [s.record.model_dump(mode="json") for s in ds.docs],
        "purchase_orders": [p.model_dump(mode="json") for p in ds.purchase_orders],
        "goods_receipts": [r.model_dump(mode="json") for r in ds.receipts],
        "matching": [c.model_dump(mode="json") for c in ds.cases],
        "emails": [e.model_dump(mode="json") for e in ds.emails],
    }
    (out / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "out": str(out),
                "files": files,
                "bytes": total,
                "seconds": round(time.monotonic() - started, 1),
                "stats": summary,
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0


RECORDINGS: Final = ROOT / "packages" / "pay" / "pairly_pay" / "data" / "recordings.json"


def recordings(*, check: bool, report: bool) -> int:
    from synth.pay.recordings import accuracy, build_bundle, items

    recorded = list(items())
    if report:
        print(json.dumps(accuracy(recorded).as_json(), indent=1))
    text = build_bundle(recorded)
    current = RECORDINGS.read_text(encoding="utf-8") if RECORDINGS.exists() else None
    if current == text:
        return 0
    if check:
        print("out of date (run: uv run python -m synth.pay recordings)", file=sys.stderr)
        return 1
    RECORDINGS.parent.mkdir(parents=True, exist_ok=True)
    RECORDINGS.write_text(text, encoding="utf-8")
    print(f"wrote {RECORDINGS.relative_to(ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m synth.pay")
    parser.add_argument("--check", action="store_true", help="compare the committed manifests")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    sub = parser.add_subparsers(dest="command")
    gen = sub.add_parser("generate", help="render the dataset into a directory")
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--count", type=int, default=None)
    gen.add_argument("--seed", type=int, default=None)
    rec = sub.add_parser("recordings", help="write the scripted extraction recordings")
    rec.add_argument("--check", action="store_true", dest="rec_check")
    rec.add_argument("--report", action="store_true")
    man = sub.add_parser("manifests", help="rewrite the committed manifests")
    man.add_argument("--count", type=int, default=None)
    man.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)
    count = getattr(args, "count", None) or DEFAULT_COUNT
    seed = args.seed if getattr(args, "seed", None) is not None else DEFAULT_SEED
    if count < 120 or count > 3000:
        parser.error("--count must be 120..3000 (the attacks need enough regular invoices)")
    if args.check:
        return check(count, seed)
    if args.command == "generate":
        return generate(args.out, count, seed)
    if args.command == "recordings":
        return recordings(check=args.rec_check, report=args.report)
    if args.command == "manifests":
        return write_manifests(count, seed)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
