# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``uv run python -m synth.attacks``: guardrail metrics on the synthetic corpus (JSON)."""

from __future__ import annotations

import json
from dataclasses import asdict

from synth.attacks.evaluate import run_corpus


def main() -> int:
    result = run_corpus()
    report = {
        "metrics": result.metrics(),
        "samples": [asdict(s) for s in result.samples],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
