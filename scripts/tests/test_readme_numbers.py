# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The "Real numbers" section of README.md is generated: it matches the generator's
output exactly, and every value in it is read from the eval report or results."""

from __future__ import annotations

import json

from scripts import readme_numbers


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


def test_no_em_dash_in_the_section() -> None:
    assert "—" not in readme_numbers.section()
