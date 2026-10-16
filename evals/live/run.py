# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Command line of the live evals (``python -m evals.live``).

``compress`` folds each product's plain recordings file into the committed xz one and
drops the entries no suite asks for any more (``harness.prune_recordings``): every suite
of the product is replayed offline (no model call, recordings only), with response
reuse on and off, to learn the request hashes it asks for now. If any suite cannot be
replayed, nothing is dropped for that product.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import subprocess
from collections.abc import Awaitable, Callable, Iterable, Mapping
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
    OfflineProvider,
    build_live_router,
    compress_recordings,
    live_settings,
    merge_result,
    prune_recordings,
)
from pairly_core.llm.prompt_registry import Root
from pairly_core.llm.settings import LLMSettings, ModelMode

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


async def current_request_hashes(
    product: str,
    recordings: Path,
    suites: Mapping[str, tuple[Suite, str]] | None = None,
    prompt_roots: Iterable[Root] | None = None,
) -> set[str]:
    """Every request hash the product's suites ask for now, learnt by replaying them
    offline from ``recordings``: the provider is never available, so a request without
    a recording is skipped, never sent. Raises when a suite fails."""
    if prompt_roots is None:
        from evals.live.hire import PROMPT_ROOTS

        prompt_roots = PROMPT_ROOTS if product == "hire" else ()
    roots = list(prompt_roots)
    seen: set[str] = set()
    for _, (suite, owner) in sorted((suites or _suites()).items()):
        if owner != product:
            continue
        for reuse in (True, False):
            settings = LLMSettings(
                model_mode=ModelMode.LIVE, llm_max_retries=0, llm_reuse_responses=reuse
            )
            router, store, budget = build_live_router(
                settings,
                recordings=recordings,
                max_cost_usd=1e9,
                prompt_roots=roots,
                provider=OfflineProvider(),
            )
            await suite(router, store, budget, Meter(store, budget, every=0), None)
            seen |= router.seen
    return seen


def compress(
    products: list[str],
    recordings: Path | None,
    suites: Mapping[str, tuple[Suite, str]] | None = None,
    prompt_roots: Iterable[Root] | None = None,
) -> int:
    """``python -m evals.live compress``: prune and compress each product's recordings."""
    rc = 0
    for product in products:
        path = recordings or RECORDINGS_DIR / f"{product}_live.jsonl.xz"
        try:
            current = asyncio.run(current_request_hashes(product, path, suites, prompt_roots))
        except Exception as exc:  # kept whole: never drop on a partial picture
            print(f"{product}: not pruned, a suite could not be replayed ({type(exc).__name__})")
            done = compress_recordings(path)
            print(f"{product}: {_shown(done) if done else 'nothing to compress'}")
            rc = 1
            continue
        result = prune_recordings(path, current)
        where = _shown(result.path) if result.path else "unchanged"
        print(
            f"{product}: {where}, kept {result.kept}, dropped {result.dropped} "
            "stale entries (no current request asks for them)"
        )
    return rc


def _shown(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else path.name


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
        return compress(sorted({p for _, p in suites.values()}), args.recordings)
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
