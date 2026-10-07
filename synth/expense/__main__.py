# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``uv run python -m synth.expense [--check]``: write the expense demo files.

* ``synth/expense/receipts/*.pdf``: the synthetic receipts.
* ``packages/expense/pairly_expense/data/recordings.json``: their recorded model
  responses (imported by ``pairly-admin seed expense-demo`` and the acceptance tests).

``--check`` writes nothing and fails when a committed file differs from what the
generator produces now (run it in CI or after changing a prompt, the schema or a parser).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from synth.expense.receipts import RECEIPTS, receipt_pdf
from synth.expense.recordings import build_bundle

ROOT = Path(__file__).resolve().parents[2]
RECEIPTS_DIR = ROOT / "synth" / "expense" / "receipts"
BUNDLE = ROOT / "packages" / "expense" / "pairly_expense" / "data" / "recordings.json"


def outputs() -> dict[Path, bytes]:
    files = {RECEIPTS_DIR / r.filename: receipt_pdf(r) for r in RECEIPTS}
    files[BUNDLE] = build_bundle().encode("utf-8")
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m synth.expense")
    parser.add_argument("--check", action="store_true", help="compare, write nothing")
    args = parser.parse_args(argv)
    stale: list[str] = []
    for path, data in outputs().items():
        current = path.read_bytes() if path.exists() else None
        if current == data:
            continue
        if args.check:
            stale.append(str(path.relative_to(ROOT)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"wrote {path.relative_to(ROOT)}")
    if stale:
        print("out of date (run: uv run python -m synth.expense):", file=sys.stderr)
        for name in stale:
            print(f"  {name}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
