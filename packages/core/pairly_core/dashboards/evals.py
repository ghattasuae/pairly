# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Eval results for the AI governance dashboard, read from the committed files.

The evals directory (``PAIRLY_EVALS_DIR``, default the repository's ``evals/``) holds
``thresholds.yaml`` (the merge gate) and ``results/*.json``: the recorded-mode results
and, when a live run happened, ``results/<name>_live.json``. Each metric of the gate is
judged here the way ``python -m evals.gate`` judges it (same rules, read only, nothing
recomputed): PASS, FAIL (out of bounds, missing, or measured with no threshold yet),
PENDING (needs a live run, not gating) or INFO (informational).

Nothing is invented: a missing directory, thresholds file or result file is reported as
such (a missing result file fails its metrics, as in the gate), and every value shown is
the one in the file. Each file is one point in time (the time in the file when it has
one, else the file's modification time, and the source says which).
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import yaml

ENV_DIR: Final = "PAIRLY_EVALS_DIR"
PENDING: Final = "pending_live_run"
AFTER_BASELINE: Final = "after_first_live_baseline"
LIVE_SUFFIX: Final = "_live"
_TIME_KEYS: Final = ("generated_at", "finished_at", "completed_at", "run_at", "started_at")

Status = Literal["PASS", "FAIL", "PENDING", "INFO"]


def default_evals_dir() -> Path | None:
    configured = os.environ.get(ENV_DIR, "").strip()
    if configured:
        return Path(configured)
    # packages/core/pairly_core/dashboards/evals.py -> the repository root.
    root = Path(__file__).resolve().parents[4] / "evals"
    return root if root.is_dir() else None


@dataclass(frozen=True, slots=True)
class MetricVerdict:
    id: str
    file: str
    status: Status
    value: Any
    threshold: str
    detail: str = ""


@dataclass(slots=True)
class ResultFile:
    name: str
    mode: Literal["recorded", "live"]
    present: bool
    at: datetime | None = None
    at_source: str | None = None
    verdicts: list[MetricVerdict] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {"PASS": 0, "FAIL": 0, "PENDING": 0, "INFO": 0}
        for v in self.verdicts:
            out[v.status] += 1
        return out


@dataclass(slots=True)
class EvalSummary:
    available: bool
    reason: str | None = None
    files: list[ResultFile] = field(default_factory=list)

    def gate(self, mode: str) -> str | None:
        """``pass`` or ``fail`` over the metrics of ``mode`` files, None when none."""
        verdicts = [v for f in self.files if f.mode == mode for v in f.verdicts]
        if not verdicts:
            return None
        return "fail" if any(v.status == "FAIL" for v in verdicts) else "pass"


def _lookup(data: Any, path: str) -> tuple[bool, Any]:
    for part in path.split("."):
        if not isinstance(data, Mapping) or part not in data:
            return False, None
        data = data[part]
    return True, data


def _describe(item: Mapping[str, Any]) -> str:
    if item.get("informational") is True:
        return "informational"
    if "equals" in item:
        return f"== {item['equals']!r}"
    parts = []
    if "min" in item:
        parts.append(f">= {item['min']}")
    if "max" in item:
        parts.append(f"<= {item['max']}")
    return ", ".join(parts) or AFTER_BASELINE


def judge(item: Mapping[str, Any], data: Any | None, file: str) -> MetricVerdict:
    """One metric against one parsed result file (None: the file is missing)."""
    metric_id, threshold = str(item["id"]), _describe(item)
    if data is None:
        return MetricVerdict(metric_id, file, "FAIL", None, threshold, "missing_file")
    found, value = _lookup(data, str(item["path"]))
    if not found:
        return MetricVerdict(metric_id, file, "FAIL", None, threshold, "missing_metric")
    if isinstance(value, Mapping) and value.get("status") == PENDING:
        return MetricVerdict(metric_id, file, "PENDING", None, threshold, PENDING)
    if item.get("informational") is True:
        return MetricVerdict(metric_id, file, "INFO", value, threshold)
    if item.get("threshold") == AFTER_BASELINE:
        return MetricVerdict(metric_id, file, "FAIL", value, threshold, "no_threshold_yet")
    if "equals" in item:
        ok = value == item["equals"] and type(value) is type(item["equals"])
        return MetricVerdict(metric_id, file, "PASS" if ok else "FAIL", value, threshold)
    if isinstance(value, bool) or not isinstance(value, int | float) or math.isnan(value):
        return MetricVerdict(metric_id, file, "FAIL", value, threshold, "not_a_number")
    if "min" in item and value < item["min"]:
        return MetricVerdict(metric_id, file, "FAIL", value, threshold, "below_threshold")
    if "max" in item and value > item["max"]:
        return MetricVerdict(metric_id, file, "FAIL", value, threshold, "above_threshold")
    return MetricVerdict(metric_id, file, "PASS", value, threshold)


def _read(path: Path) -> tuple[Any | None, str | None]:
    if not path.is_file():
        return None, "missing_file"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError):
        return None, "unreadable"


def _when(path: Path, data: Any) -> tuple[datetime | None, str | None]:
    if isinstance(data, Mapping):
        for key in _TIME_KEYS:
            raw = data.get(key)
            if isinstance(raw, str):
                try:
                    stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                return (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)), key
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC), "file_modified"
    except OSError:
        return None, None


def read_evals(directory: Path | None) -> EvalSummary:
    if directory is None or not directory.is_dir():
        return EvalSummary(available=False, reason="no_evals_directory")
    thresholds = directory / "thresholds.yaml"
    try:
        spec = yaml.safe_load(thresholds.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return EvalSummary(available=False, reason="no_thresholds")
    metrics = spec.get("metrics") if isinstance(spec, Mapping) else None
    if not isinstance(metrics, list):
        return EvalSummary(available=False, reason="no_thresholds")
    items = [m for m in metrics if isinstance(m, Mapping) and {"id", "file", "path"} <= set(m)]
    results = directory / "results"
    summary = EvalSummary(available=True)
    names = sorted({str(m["file"]) for m in items})
    for name in names:
        for mode in ("recorded", "live"):
            file_name = name if mode == "recorded" else name.replace(".json", f"{LIVE_SUFFIX}.json")
            path = results / file_name
            data, problem = _read(path)
            if mode == "live" and problem == "missing_file":
                continue  # no live run of this suite yet: nothing to show, nothing failed
            at, at_source = _when(path, data) if problem is None else (None, None)
            entry = ResultFile(
                name=file_name, mode=mode, present=problem is None, at=at, at_source=at_source
            )
            entry.verdicts = [judge(m, data, file_name) for m in items if str(m["file"]) == name]
            summary.files.append(entry)
    summary.files.sort(key=lambda f: (f.at or datetime.min.replace(tzinfo=UTC), f.name))
    return summary
