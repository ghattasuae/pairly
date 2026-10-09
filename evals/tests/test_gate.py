# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The eval gate (``python -m evals.gate``): what passes, fails and is only listed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from evals.gate import THRESHOLDS, ThresholdsError, gate, load_thresholds, main
from evals.hire.report import BEGIN, END, write_report


def _write(tmp: Path, metrics: list[dict[str, Any]], results: dict[str, Any]) -> Path:
    (tmp / "thresholds.yaml").write_text(
        yaml.safe_dump({"version": 1, "metrics": metrics}), encoding="utf-8"
    )
    for name, data in results.items():
        (tmp / name).write_text(json.dumps(data), encoding="utf-8")
    return tmp / "thresholds.yaml"


def _statuses(tmp: Path, metrics: list[dict[str, Any]], results: dict[str, Any]) -> dict[str, str]:
    path = _write(tmp, metrics, results)
    return {v.id: v.status for v in gate(path, tmp)}


def test_measured_metrics_pass_or_fail_against_their_threshold(tmp_path: Path) -> None:
    results = {"r.json": {"a": {"rate": 1.0}, "b": 0.97, "c": 3, "d": True, "e": 1}}
    statuses = _statuses(
        tmp_path,
        [
            {"id": "a", "file": "r.json", "path": "a.rate", "min": 1.0},
            {"id": "b", "file": "r.json", "path": "b", "min": 0.98},
            {"id": "c", "file": "r.json", "path": "c", "max": 0},
            {"id": "d", "file": "r.json", "path": "d", "equals": True},
            # ``1`` is not ``True``: types must match too.
            {"id": "e", "file": "r.json", "path": "e", "equals": True},
        ],
        results,
    )
    assert statuses == {"a": "PASS", "b": "FAIL", "c": "FAIL", "d": "PASS", "e": "FAIL"}


def test_pending_is_listed_not_gating_and_missing_fails_closed(tmp_path: Path) -> None:
    pending = {"status": "pending_live_run", "reason": "needs a model"}
    statuses = _statuses(
        tmp_path,
        [
            {"id": "p", "file": "r.json", "path": "p", "min": 0.9},
            {"id": "q", "file": "r.json", "path": "q", "threshold": "after_first_live_baseline"},
            {
                "id": "measured_no_threshold",
                "file": "r.json",
                "path": "m",
                "threshold": "after_first_live_baseline",
            },
            {"id": "missing_path", "file": "r.json", "path": "nope", "min": 0},
            {"id": "missing_file", "file": "none.json", "path": "x", "min": 0},
            {"id": "not_a_number", "file": "r.json", "path": "s", "min": 0},
            {"id": "null", "file": "r.json", "path": "n", "min": 0},
        ],
        {"r.json": {"p": pending, "q": pending, "m": 0.5, "s": "high", "n": None}},
    )
    assert statuses == {
        "p": "PENDING",
        "q": "PENDING",
        "measured_no_threshold": "FAIL",
        "missing_path": "FAIL",
        "missing_file": "FAIL",
        "not_a_number": "FAIL",
        "null": "FAIL",
    }


def test_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ok = _write(
        tmp_path,
        [
            {"id": "a", "file": "r.json", "path": "a", "min": 1},
            {"id": "p", "file": "r.json", "path": "p", "threshold": "after_first_live_baseline"},
        ],
        {"r.json": {"a": 1, "p": {"status": "pending_live_run"}}},
    )
    assert main(["--thresholds", str(ok), "--results", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "1 passed, 0 failed, 1 pending" in out
    (tmp_path / "r.json").write_text(json.dumps({"a": 0, "p": 1}), encoding="utf-8")
    assert main(["--thresholds", str(ok), "--results", str(tmp_path)]) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"version": 2, "metrics": [{"id": "a", "file": "r", "path": "a", "min": 1}]},
        {"version": 1, "metrics": []},
        {"version": 1, "metrics": [{"id": "a", "file": "r", "path": "a"}]},
        {"version": 1, "metrics": [{"id": "a", "file": "r", "path": "a", "min": "1"}]},
        {"version": 1, "metrics": [{"id": "a", "file": "r", "path": "a", "min": 1, "x": 1}]},
        {
            "version": 1,
            "metrics": [
                {"id": "a", "file": "r", "path": "a", "min": 1},
                {"id": "a", "file": "r", "path": "b", "min": 1},
            ],
        },
        {
            "version": 1,
            "metrics": [
                {
                    "id": "a",
                    "file": "r",
                    "path": "a",
                    "min": 1,
                    "threshold": "after_first_live_baseline",
                }
            ],
        },
    ],
)
def test_malformed_thresholds_fail(tmp_path: Path, bad: dict[str, Any]) -> None:
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(ThresholdsError):
        load_thresholds(path)
    assert main(["--thresholds", str(path), "--results", str(tmp_path)]) == 1


def test_committed_thresholds_are_valid() -> None:
    metrics = load_thresholds(THRESHOLDS)
    ids = {m["id"] for m in metrics}
    assert {"hire.evidence_validity", "hire.blind.fairness_cvs_form_only"} <= ids
    evidence = next(m for m in metrics if m["id"] == "hire.evidence_validity")
    assert evidence["min"] == 1.0  # BRIEF 7.2: target 100%


def test_report_section_is_replaced_in_place(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# Report\n\nOther runner's text.\n", encoding="utf-8")
    result: dict[str, Any] = {
        "dataset": {"count": 3, "seed": 1, "screened": 3, "errors": []},
        "evidence_validity": {"rate": 1.0, "quotes": 10},
        "rubric_agreement": {"status": "pending_live_run"},
    }
    write_report(report, result)
    write_report(report, result)
    text = report.read_text(encoding="utf-8")
    assert text.count(BEGIN) == 1
    assert text.count(END) == 1
    assert "Other runner's text." in text
    assert "| Rubric scoring agreement with labels | pending live run |" in text
    assert "| Evidence quotes checked | 10 |" in text
