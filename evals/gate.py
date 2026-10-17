# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The eval merge gate: ``uv run python -m evals.gate`` (BRIEF rule 6).

Reads ``evals/thresholds.yaml`` and the result files it names (``evals/results/``) and
prints one line per metric:

* ``PASS``: measured and within its threshold;
* ``FAIL``: measured and outside it, or missing, or not a number, or measured with no
  threshold yet (fail closed: a number nobody agreed on never passes silently);
* ``PENDING``: ``{"status": "pending_live_run"}``, it needs a real model; listed, not
  gating.
* ``INFO``: an ``informational: true`` metric (no bound); its value is listed, never
  gating. It must still be present in its result file (missing fails closed).

A metric marked ``live: true`` is read from a committed live baseline file
(``*_live.json``, written by ``python -m evals.live``): a real model answered. Its line
says ``LIVE`` with the run date, and the summary counts the live values. It fails closed
when its suite (the first part of its path) is not a complete live run.

Exit code 1 when any metric fails, else 0. ``--json`` prints the verdicts as JSON.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final, Literal

import yaml

ROOT: Final = Path(__file__).resolve().parent
THRESHOLDS: Final = ROOT / "thresholds.yaml"
RESULTS: Final = ROOT / "results"
PENDING: Final = "pending_live_run"
AFTER_BASELINE: Final = "after_first_live_baseline"
LIVE_SUFFIX: Final = "_live.json"
_KEYS: Final = frozenset(
    {"id", "file", "path", "min", "max", "equals", "threshold", "informational", "live", "why"}
)

Status = Literal["PASS", "FAIL", "PENDING", "INFO"]


class ThresholdsError(ValueError):
    """The thresholds file is malformed (fails the gate)."""


@dataclass(frozen=True, slots=True)
class Verdict:
    id: str
    status: Status
    value: Any
    threshold: str
    detail: str
    live: bool = False
    run_date: str = ""


def load_thresholds(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ThresholdsError("thresholds: expected version 1")
    metrics = data.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise ThresholdsError("thresholds: no metrics")
    seen: set[str] = set()
    for item in metrics:
        if not isinstance(item, dict) or not {"id", "file", "path"} <= set(item):
            raise ThresholdsError(f"thresholds: incomplete entry {item!r}")
        unknown = set(item) - _KEYS
        if unknown:
            raise ThresholdsError(f"thresholds: {item['id']}: unknown keys {sorted(unknown)}")
        if item["id"] in seen:
            raise ThresholdsError(f"thresholds: duplicate id {item['id']}")
        seen.add(item["id"])
        if "live" in item and (
            item["live"] is not True or not str(item["file"]).endswith(LIVE_SUFFIX)
        ):
            raise ThresholdsError(
                f"thresholds: {item['id']}: live: true reads a *{LIVE_SUFFIX} file"
            )
        if str(item["file"]).endswith(LIVE_SUFFIX) and item.get("live") is not True:
            raise ThresholdsError(
                f"thresholds: {item['id']}: a *{LIVE_SUFFIX} metric says live: true"
            )
        has_bound = any(k in item for k in ("min", "max", "equals"))
        if "informational" in item:
            if item["informational"] is not True or has_bound or "threshold" in item:
                raise ThresholdsError(
                    f"thresholds: {item['id']}: informational: true takes no threshold"
                )
            if not item.get("why"):
                raise ThresholdsError(f"thresholds: {item['id']}: say why it does not gate")
            continue
        if has_bound == (item.get("threshold") == AFTER_BASELINE):
            raise ThresholdsError(
                f"thresholds: {item['id']}: give min, max or equals, or threshold: {AFTER_BASELINE}"
            )
        for key in ("min", "max"):
            if key in item and (
                isinstance(item[key], bool) or not isinstance(item[key], int | float)
            ):
                raise ThresholdsError(f"thresholds: {item['id']}: {key} must be a number")
    return metrics


def lookup(data: Any, path: str) -> tuple[bool, Any]:
    for part in path.split("."):
        if not isinstance(data, Mapping) or part not in data:
            return False, None
        data = data[part]
    return True, data


def describe(item: Mapping[str, Any]) -> str:
    if item.get("informational") is True:
        return "informational, not gating"
    if "equals" in item:
        return f"== {item['equals']!r}"
    parts = []
    if "min" in item:
        parts.append(f">= {item['min']}")
    if "max" in item:
        parts.append(f"<= {item['max']}")
    return ", ".join(parts) or "set after the first live baseline"


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _live_run(data: Any, path: str) -> tuple[str, str]:
    """(run date, problem) of the live suite holding ``path``; problem is "" when sound."""
    suite = path.split(".", 1)[0]
    found, block = lookup(data, suite)
    if not found or not isinstance(block, Mapping):
        return "", f"no live suite {suite!r}"
    run = block.get("run")
    date = str(run.get("date", "")) if isinstance(run, Mapping) else ""
    if not isinstance(run, Mapping) or run.get("mode") != "live":
        return date, f"suite {suite!r} is not a live run"
    if block.get("status") != "complete":
        return date, f"suite {suite!r} is {block.get('status', 'not run')!s}, not complete"
    return date, ""


def judge(item: Mapping[str, Any], results_dir: Path) -> Verdict:
    metric_id = str(item["id"])
    threshold = describe(item)
    live = item.get("live") is True

    def verdict(status: Status, value: Any, detail: str = "", date: str = "") -> Verdict:
        return Verdict(metric_id, status, value, threshold, detail, live, date)

    path = results_dir / str(item["file"])
    if not path.exists():
        return verdict("FAIL", None, f"missing result file {item['file']}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return verdict("FAIL", None, f"{item['file']} is not JSON")
    found, value = lookup(data, str(item["path"]))
    if not found:
        return verdict("FAIL", None, f"missing {item['path']}")
    date = ""
    if live:
        date, problem = _live_run(data, str(item["path"]))
        if problem:
            return verdict("FAIL", value, problem, date)
    if isinstance(value, Mapping) and value.get("status") == PENDING:
        return verdict("PENDING", PENDING, str(value.get("reason", "")), date)
    if item.get("informational") is True:
        return verdict("INFO", value, "", date)
    if item.get("threshold") == AFTER_BASELINE:
        return verdict("FAIL", value, "measured, but no threshold is set yet (ASK)", date)
    if "equals" in item:
        expected = item["equals"]
        if _is_number(expected) and _is_number(value):
            ok = not math.isnan(value) and value == expected
        else:  # ``1`` is not ``True``: outside numbers, types must match too.
            ok = value == expected and type(value) is type(expected)
        return verdict("PASS" if ok else "FAIL", value, "", date)
    if not _is_number(value) or math.isnan(value):
        return verdict("FAIL", value, "not a number", date)
    if "min" in item and value < item["min"]:
        return verdict("FAIL", value, "below threshold", date)
    if "max" in item and value > item["max"]:
        return verdict("FAIL", value, "above threshold", date)
    return verdict("PASS", value, "", date)


def gate(thresholds: Path = THRESHOLDS, results_dir: Path = RESULTS) -> list[Verdict]:
    return [judge(item, results_dir) for item in load_thresholds(thresholds)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.gate")
    parser.add_argument("--thresholds", type=Path, default=THRESHOLDS)
    parser.add_argument("--results", type=Path, default=RESULTS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        verdicts = gate(args.thresholds, args.results)
    except (OSError, ThresholdsError, yaml.YAMLError) as exc:
        print(f"eval gate: {exc}", file=sys.stderr)
        return 1
    failed = [v for v in verdicts if v.status == "FAIL"]
    pending = [v for v in verdicts if v.status == "PENDING"]
    info = [v for v in verdicts if v.status == "INFO"]
    if args.json:
        print(json.dumps([asdict(v) for v in verdicts], indent=1, default=str))
    else:
        width = max(len(v.id) for v in verdicts)
        for v in verdicts:
            value = "" if v.status == "PENDING" else f" value={v.value!r}"
            note = f" ({v.detail})" if v.detail else ""
            source = f" [LIVE model, run {v.run_date or 'unknown'}]" if v.live else ""
            print(f"{v.status:<7} {v.id:<{width}} {v.threshold}{value}{source}{note}")
        live = [v for v in verdicts if v.live and v.status != "INFO"]
        live_passed = sum(1 for v in live if v.status == "PASS")
        print(
            f"\n{len(verdicts) - len(failed) - len(pending) - len(info)} passed, "
            f"{len(failed)} failed, {len(pending)} pending a live run (listed, not gating), "
            f"{len(info)} informational (not gating)"
        )
        print(
            f"Live values: {len(live)} gated metrics are live model results read from the "
            f"committed *{LIVE_SUFFIX} files ({live_passed} passed); every other value is "
            "deterministic or from scripted answers."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
