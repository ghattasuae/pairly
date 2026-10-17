# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The eval gate (``python -m evals.gate``): what passes, fails and is only listed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from evals.gate import RESULTS, THRESHOLDS, ThresholdsError, gate, judge, load_thresholds, main
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


def test_informational_metrics_are_listed_not_gating(tmp_path: Path) -> None:
    statuses = _statuses(
        tmp_path,
        [
            {"id": "i", "file": "r.json", "path": "i", "informational": True, "why": "context"},
            {"id": "gone", "file": "r.json", "path": "nope", "informational": True, "why": "x"},
        ],
        {"r.json": {"i": 0.5}},
    )
    # A low value never fails an informational metric; a missing one still fails closed.
    assert statuses == {"i": "INFO", "gone": "FAIL"}
    for bad in (
        {"id": "b", "file": "r.json", "path": "i", "informational": True, "min": 1.0, "why": "x"},
        {"id": "b", "file": "r.json", "path": "i", "informational": True},
        {"id": "b", "file": "r.json", "path": "i", "informational": False, "why": "x"},
    ):
        with pytest.raises(ThresholdsError):
            load_thresholds(_write(tmp_path, [bad], {}))


def test_blind_gate_is_the_parsed_profile_path() -> None:
    by_id = {m["id"]: m for m in load_thresholds(THRESHOLDS)}
    assert by_id["hire.blind.regular_cvs_with_profile"]["min"] == 1.0
    assert by_id["hire.blind.regular_cvs_form_only"].get("informational") is True
    assert by_id["hire.blind.fairness_cvs_form_only"]["min"] == 1.0


# The live thresholds the founder approved on 2026-10-17, with a value just outside each.
APPROVED_LIVE = {
    "hire.extraction.model_accuracy": ("min", 0.95, 0.9499),
    "hire.rubric_agreement": ("min", 0.80, 0.7999),
    "hire.fairness.model_score_delta": ("equals", 0, 0.01),
    "hire.fairness.model_delta_above_noise": ("equals", 0, 1),
    "hire.injection.classifier_recall": ("min", 0.8, 0.6),
    "hire.injection.classifier_false_positive_rate": ("max", 0.02, 0.0201),
    "pay.extraction.header_accuracy": ("min", 95, 94.9),
    "pay.extraction.line_item_accuracy": ("min", 95, 94.9),
    "pay.extraction.total_accuracy": ("min", 95, 94.9),
    "pay.extraction.vat_accuracy": ("min", 85, 84.9),
}


def _committed_with(tmp: Path, metric: dict[str, Any], value: Any) -> Path:
    """The committed result file of ``metric``, copied to ``tmp`` with ``value`` at its path."""
    data = json.loads((RESULTS / metric["file"]).read_text(encoding="utf-8"))
    node = data
    *parents, last = str(metric["path"]).split(".")
    for part in parents:
        node = node[part]
    node[last] = value
    (tmp / metric["file"]).write_text(json.dumps(data), encoding="utf-8")
    return tmp


@pytest.mark.parametrize("metric_id", sorted(APPROVED_LIVE))
def test_approved_live_thresholds_gate_the_committed_live_files(
    tmp_path: Path, metric_id: str
) -> None:
    by_id = {m["id"]: m for m in load_thresholds(THRESHOLDS)}
    metric = by_id[metric_id]
    bound, threshold, outside = APPROVED_LIVE[metric_id]
    assert metric[bound] == threshold
    assert metric["live"] is True
    assert metric["file"].endswith("_live.json")
    # The committed live run passes, and is labelled live with its run date.
    passed = judge(metric, RESULTS)
    assert passed.status == "PASS"
    assert passed.live
    assert passed.run_date.startswith("2026-")
    # A value just outside the threshold fails the gate; the threshold itself passes.
    assert judge(metric, _committed_with(tmp_path, metric, outside)).status == "FAIL"
    assert judge(metric, _committed_with(tmp_path, metric, threshold)).status == "PASS"


def test_rubric_exact_agreement_is_reported_not_gated() -> None:
    by_id = {m["id"]: m for m in load_thresholds(THRESHOLDS)}
    exact = by_id["hire.rubric_agreement.exact"]
    assert exact.get("informational") is True
    assert judge(exact, RESULTS).status == "INFO"


def test_live_metric_fails_closed_unless_its_suite_is_a_complete_live_run(
    tmp_path: Path,
) -> None:
    metric = {"id": "l", "file": "x_live.json", "path": "s.v", "live": True, "min": 0.5}
    run = {"mode": "live", "date": "2026-10-16T00:00:00Z"}
    cases = {
        "ok": {"s": {"status": "complete", "run": run, "v": 0.9}},
        "recorded": {"s": {"status": "complete", "run": {"mode": "recorded"}, "v": 0.9}},
        "partial": {"s": {"status": "partial", "run": run, "v": 0.9}},
        "no_run": {"s": {"status": "complete", "v": 0.9}},
    }
    statuses = {}
    for name, data in cases.items():
        (tmp_path / "x_live.json").write_text(json.dumps(data), encoding="utf-8")
        statuses[name] = judge(metric, tmp_path).status
    assert statuses == {"ok": "PASS", "recorded": "FAIL", "partial": "FAIL", "no_run": "FAIL"}


def test_live_flag_must_match_the_file(tmp_path: Path) -> None:
    for bad in (
        {"id": "a", "file": "r.json", "path": "a", "live": True, "min": 1},
        {"id": "a", "file": "r_live.json", "path": "a", "min": 1},
        {"id": "a", "file": "r_live.json", "path": "a", "live": "yes", "min": 1},
    ):
        with pytest.raises(ThresholdsError):
            load_thresholds(_write(tmp_path, [bad], {}))


def test_numeric_equals_compares_values_not_types(tmp_path: Path) -> None:
    statuses = _statuses(
        tmp_path,
        [
            {"id": "float_zero", "file": "r.json", "path": "f", "equals": 0},
            {"id": "int_zero", "file": "r.json", "path": "i", "equals": 0},
            {"id": "nonzero", "file": "r.json", "path": "n", "equals": 0},
            {"id": "false_is_not_zero", "file": "r.json", "path": "b", "equals": 0},
        ],
        {"r.json": {"f": 0.0, "i": 0, "n": 0.5, "b": False}},
    )
    assert statuses == {
        "float_zero": "PASS",
        "int_zero": "PASS",
        "nonzero": "FAIL",
        "false_is_not_zero": "FAIL",
    }


def test_output_says_which_values_are_live(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if x.split()[1:2] == ["hire.rubric_agreement"])
    assert "[LIVE model, run 2026-" in line
    assert "Live values: 10 gated metrics are live model results" in out
    evidence = next(x for x in out.splitlines() if x.split()[1:2] == ["hire.evidence_validity"])
    assert "LIVE" not in evidence
