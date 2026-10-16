# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Dashboard building blocks without a database: the eval file reader and the plugin
section registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pairly_core.dashboards.evals import judge, read_evals
from pairly_core.dashboards.model import (
    Board,
    DashboardSectionError,
    DashboardSectionSpec,
    Section,
    SectionContext,
    build_section_registry,
)


def _write(root: Path, thresholds: str, results: dict[str, Any]) -> Path:
    (root / "results").mkdir(parents=True)
    (root / "thresholds.yaml").write_text(thresholds, encoding="utf-8")
    for name, data in results.items():
        (root / "results" / name).write_text(json.dumps(data), encoding="utf-8")
    return root


def test_no_directory_or_no_thresholds_is_said_not_guessed(tmp_path: Path) -> None:
    assert read_evals(None).reason == "no_evals_directory"
    assert read_evals(tmp_path / "missing").reason == "no_evals_directory"
    (tmp_path / "e").mkdir()
    summary = read_evals(tmp_path / "e")
    assert (summary.available, summary.reason) == (False, "no_thresholds")


def test_verdicts_follow_the_gate(tmp_path: Path) -> None:
    root = _write(
        tmp_path / "evals",
        "version: 1\nmetrics:\n"
        "  - {id: r, file: a.json, path: x.rate, min: 0.9, why: w}\n"
        "  - {id: m, file: a.json, path: x.max, max: 0, why: w}\n"
        "  - {id: i, file: a.json, path: x.info, informational: true, why: w}\n"
        "  - {id: p, file: a.json, path: x.pending, threshold: after_first_live_baseline, why: w}\n"
        "  - {id: gone, file: gone.json, path: x, min: 1, why: w}\n",
        {
            "a.json": {
                "x": {"rate": 0.95, "max": 1, "info": 3, "pending": {"status": "pending_live_run"}}
            },
            "a_live.json": {"x": {"rate": 0.2, "max": 0, "info": 1, "pending": 0.7}},
        },
    )
    summary = read_evals(root)
    by_file = {f.name: f for f in summary.files}
    assert set(by_file) == {"a.json", "a_live.json", "gone.json"}
    recorded = {v.id: (v.status, v.detail) for v in by_file["a.json"].verdicts}
    assert recorded == {
        "r": ("PASS", ""),
        "m": ("FAIL", "above_threshold"),
        "i": ("INFO", ""),
        "p": ("PENDING", "pending_live_run"),
    }
    live = {v.id: (v.status, v.value) for v in by_file["a_live.json"].verdicts}
    assert live["r"] == ("FAIL", 0.2)
    assert live["p"] == ("FAIL", 0.7)  # measured with no threshold yet fails closed
    assert by_file["gone.json"].present is False
    assert [v.detail for v in by_file["gone.json"].verdicts] == ["missing_file"]
    assert summary.gate("recorded") == "fail"
    assert summary.gate("live") == "fail"


def test_equals_needs_the_same_type() -> None:
    item = {"id": "e", "file": "a.json", "path": "ok", "equals": True}
    assert judge(item, {"ok": True}, "a.json").status == "PASS"
    assert judge(item, {"ok": 1}, "a.json").status == "FAIL"
    assert judge(item, {}, "a.json").detail == "missing_metric"
    assert judge({"id": "n", "file": "a", "path": "v", "min": 1}, {"v": "x"}, "a").detail == (
        "not_a_number"
    )


async def _build(ctx: SectionContext) -> Section:
    return Section(key="x", count=1)


def test_plugin_sections_are_checked() -> None:
    class Good:
        id = "pay"
        dashboard_sections = (DashboardSectionSpec(Board.ADMIN, "x", "pay.invoice.read", _build),)

    class Foreign:
        id = "pay"
        dashboard_sections = (DashboardSectionSpec(Board.ADMIN, "x", "hire.job.read", _build),)

    class NotASpec:
        id = "pay"
        dashboard_sections = ("x",)

    assert len(build_section_registry([Good()]).for_board(Board.ADMIN)) == 1
    with pytest.raises(DashboardSectionError, match="its own permission"):
        build_section_registry([Foreign()])
    with pytest.raises(DashboardSectionError, match="must be DashboardSectionSpec"):
        build_section_registry([NotASpec()])
    with pytest.raises(DashboardSectionError, match="duplicate"):
        build_section_registry([Good(), Good()])
    with pytest.raises(DashboardSectionError, match="platform operations"):
        DashboardSectionSpec(Board.PLATFORM_OPS, "x", "pay.invoice.read", _build)
    with pytest.raises(DashboardSectionError, match="invalid"):
        DashboardSectionSpec(Board.ADMIN, "Bad Key", "pay.invoice.read", _build)
