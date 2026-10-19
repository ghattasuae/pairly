# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The "Real numbers" section of README.md is generated: it matches the generator's
output exactly, and every value in it is read from the eval report or results."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "readme_numbers", ROOT / "scripts" / "readme_numbers.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["readme_numbers"] = module
    spec.loader.exec_module(module)
    return module


readme_numbers = _load()


def test_readme_numbers_section_matches_the_generator() -> None:
    readme = readme_numbers.README.read_text(encoding="utf-8")
    assert readme_numbers.rendered(readme) == readme, "run: uv run python -m scripts.readme_numbers"
    assert readme_numbers.section() in readme


def test_values_come_from_the_sources() -> None:
    table, facts = readme_numbers.rows()
    report = readme_numbers.REPORT.read_text(encoding="utf-8")
    pay = json.loads((readme_numbers.RESULTS / "pay.json").read_text(encoding="utf-8"))
    for row in table:
        if row.how == readme_numbers.LIVE:
            assert f"| {row.value} | {row.sample} |" in report, row
    by_metric = {row.metric: row for row in table}
    assert by_metric["Bank-mismatch recall"].value == str(pay["bank_mismatch"]["recall"])
    for value in facts.values():
        assert value in report


def test_the_live_gate_sentence_counts_the_thresholds_file() -> None:
    from evals.gate import THRESHOLDS, load_thresholds

    live = [
        m
        for m in load_thresholds(THRESHOLDS)
        if m.get("live") is True and not m.get("informational")
    ]
    assert live
    assert f"{len(live)} of the live numbers gate merges" in readme_numbers.section()


def test_no_em_dash_in_the_section() -> None:
    assert "—" not in readme_numbers.section()


def test_the_repository_stats_are_generated_and_name_their_date() -> None:
    readme = readme_numbers.README.read_text(encoding="utf-8")
    start = readme.index(readme_numbers.STATS_BEGIN)
    block = readme[start : readme.index(readme_numbers.STATS_END)]
    # The block names the date it was counted on, never a commit hash: a squash merge
    # drops the branch commit a hash would name.
    day = re.search(r"Counted on (\d{4}-\d{2}-\d{2}) from the files git tracks", block)
    assert day is not None
    date.fromisoformat(day.group(1))
    assert not re.search(r"at commit `[0-9a-f]{7,40}`", block)
    # Every row the generator makes today is in the block (the values move with each
    # commit; rerun the script to refresh them).
    for what, _ in readme_numbers.stats():
        assert f"| {what} | " in block


def test_readme_links_point_at_files_that_exist() -> None:
    readme = readme_numbers.README.read_text(encoding="utf-8")
    for link in re.findall(r"\]\(([^)#]+)\)", readme):
        if not link.startswith("http"):
            assert (ROOT / link).exists(), link
