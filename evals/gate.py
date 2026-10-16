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
_KEYS: Final = frozenset(
    {"id", "file", "path", "min", "max", "equals", "threshold", "informational", "why"}
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


def _describe(item: Mapping[str, Any]) -> str:
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


def judge(item: Mapping[str, Any], results_dir: Path) -> Verdict:
    metric_id = str(item["id"])
    threshold = _describe(item)
    path = results_dir / str(item["file"])
    if not path.exists():
        return Verdict(metric_id, "FAIL", None, threshold, f"missing result file {item['file']}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return Verdict(metric_id, "FAIL", None, threshold, f"{item['file']} is not JSON")
    found, value = lookup(data, str(item["path"]))
    if not found:
        return Verdict(metric_id, "FAIL", None, threshold, f"missing {item['path']}")
    if isinstance(value, Mapping) and value.get("status") == PENDING:
        return Verdict(metric_id, "PENDING", PENDING, threshold, str(value.get("reason", "")))
    if item.get("informational") is True:
        return Verdict(metric_id, "INFO", value, threshold, "")
    if item.get("threshold") == AFTER_BASELINE:
        return Verdict(
            metric_id, "FAIL", value, threshold, "measured, but no threshold is set yet (ASK)"
        )
    if "equals" in item:
        ok = value == item["equals"] and type(value) is type(item["equals"])
        return Verdict(metric_id, "PASS" if ok else "FAIL", value, threshold, "")
    if isinstance(value, bool) or not isinstance(value, int | float) or math.isnan(value):
        return Verdict(metric_id, "FAIL", value, threshold, "not a number")
    if "min" in item and value < item["min"]:
        return Verdict(metric_id, "FAIL", value, threshold, "below threshold")
    if "max" in item and value > item["max"]:
        return Verdict(metric_id, "FAIL", value, threshold, "above threshold")
    return Verdict(metric_id, "PASS", value, threshold, "")


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
            print(f"{v.status:<7} {v.id:<{width}} {v.threshold}{value}{note}")
        print(
            f"\n{len(verdicts) - len(failed) - len(pending) - len(info)} passed, "
            f"{len(failed)} failed, {len(pending)} pending a live run (listed, not gating), "
            f"{len(info)} informational (not gating)"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
