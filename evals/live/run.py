# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Command line of the live evals (``python -m evals.live``)."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

from evals.live.harness import (
    RECORDINGS_DIR,
    RESULTS_DIR,
    ROOT,
    CapBudget,
    LiveCallStore,
    LiveRouter,
    LiveSetupError,
    Meter,
    build_live_router,
    compress_recordings,
    live_settings,
    merge_result,
)

Suite = Callable[
    [LiveRouter, LiveCallStore, CapBudget, Meter, int | None], Awaitable[dict[str, Any]]
]
DEFAULT_MAX_COST_USD: Final = 5.0


def _suites() -> dict[str, tuple[Suite, str]]:
    from evals.live.hire import suite_extraction, suite_fairness, suite_injection
    from evals.live.pay import suite_pay_extraction

    return {
        "hire-extraction": (suite_extraction, "hire"),
        "hire-fairness": (suite_fairness, "hire"),
        "hire-injection": (suite_injection, "hire"),
        "pay-extraction": (suite_pay_extraction, "pay"),
    }


def _commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


async def _run(suite: Suite, product: str, args: argparse.Namespace) -> dict[str, Any]:
    from evals.live.hire import PROMPT_ROOTS

    settings = live_settings()
    recordings: Path = args.recordings or RECORDINGS_DIR / f"{product}_live.jsonl.xz"
    router, store, budget = build_live_router(
        settings,
        recordings=recordings,
        max_cost_usd=args.max_cost_usd,
        prompt_roots=PROMPT_ROOTS if product == "hire" else (),
    )
    print(
        f"[live] {args.suite}: cap {args.max_cost_usd:.2f} USD, "
        f"{store.loaded} recordings loaded ({store.refused} refused)",
        flush=True,
    )
    meter = Meter(store, budget, every=args.every)
    try:
        result = await suite(router, store, budget, meter, args.limit)
    finally:
        compress_recordings(recordings)
    result["run"] = {
        "suite": args.suite,
        "date": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": _commit(),
        "mode": settings.model_mode.value,
        "max_cost_usd": args.max_cost_usd,
        "spent_usd": round(budget.spent / 1e6, 6),
        "limit": args.limit,
        "recordings": str(recordings.relative_to(ROOT))
        if recordings.is_relative_to(ROOT)
        else recordings.name,
        "recordings_loaded": store.loaded,
        "catalog": "packages/core/pairly_core/llm/data/models.yaml",
        "prices": "packages/core/pairly_core/llm/data/prices.yaml",
    }
    return result


def main(argv: list[str] | None = None) -> int:
    suites = _suites()
    parser = argparse.ArgumentParser(prog="python -m evals.live")
    parser.add_argument("suite", choices=[*suites, "report", "compress"])
    parser.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD)
    parser.add_argument("--limit", type=int, default=None, help="first N documents only")
    parser.add_argument("--every", type=int, default=5, help="print the spend every N documents")
    parser.add_argument("--recordings", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.suite == "compress":
        for product in sorted({p for _, p in suites.values()}):
            path = args.recordings or RECORDINGS_DIR / f"{product}_live.jsonl.xz"
            done = compress_recordings(path)
            print(f"{product}: {done.relative_to(ROOT) if done else 'nothing to compress'}")
        return 0
    if args.suite == "report":
        from evals.live.report import write_live_report

        write_live_report()
        print("wrote evals/report.md (Live baseline)")
        return 0
    if args.max_cost_usd <= 0:
        parser.error("--max-cost-usd must be positive")
    suite, product = suites[args.suite]
    try:
        result = asyncio.run(_run(suite, product, args))
    except LiveSetupError as exc:
        print(f"[live] cannot run: {exc}")
        return 2
    out: Path = args.out or RESULTS_DIR / f"{product}_live.json"
    key = args.suite.split("-", 1)[1]
    merge_result(out, key, result)
    usage = result["usage"]
    print(
        f"[live] {args.suite}: {result['status']}, {result['measured_documents']} of "
        f"{result['planned_documents']} documents, spent {result['run']['spent_usd']:.4f} USD, "
        f"models {usage['models_returned_by_api'] or usage['models']}, tokens "
        f"{usage['input_tokens']} in / {usage['output_tokens']} out, wrote {out}",
        flush=True,
    )
    return 0
