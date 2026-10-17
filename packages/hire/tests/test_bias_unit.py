# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The bias and adverse-impact report: four-fifths math, small samples, suppression,
the exact statistics, and the audit pack's contents."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import UTC, datetime

import pytest

from pairly_hire.analytics.bias import (
    BiasSettings,
    audit_pack,
    build,
    step_result,
)
from pairly_hire.analytics.stats import cohen_kappa, fisher_exact

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
ORDER = ["applied", "screening", "shortlist", "hired"]
# Hand-built counts per gender group: applications that reached each stage.
GENDER = {
    "woman": {"applied": 40, "screening": 10, "shortlist": 2, "hired": 1},
    "man": {"applied": 50, "screening": 20, "shortlist": 10, "hired": 3},
    "another": {"applied": 3, "screening": 1},
    "prefer_not_to_say": {"applied": 8, "screening": 4, "shortlist": 1},
    "unanswered": {"applied": 20, "screening": 5, "shortlist": 2},
}
LABELS = {"woman": ("Woman", "امرأة"), "man": ("Man", "رجل")}


def test_fisher_exact_matches_published_values() -> None:
    # SciPy's documented example and the classic tea tasting table (R's fisher.test).
    assert fisher_exact(8, 2, 1, 5) == pytest.approx(0.0349650349650)
    assert fisher_exact(3, 1, 1, 3) == pytest.approx(0.4857142857142857)
    assert fisher_exact(0, 0, 0, 0) == 1.0
    with pytest.raises(ValueError, match="non-negative"):
        fisher_exact(-1, 0, 0, 0)


def test_cohen_kappa() -> None:
    assert cohen_kappa([0, 1, 2, 0, 1], [0, 1, 1, 0, 2]) == pytest.approx(0.375)
    assert cohen_kappa([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert cohen_kappa([2, 2], [2, 2]) is None  # one category: undefined
    assert cohen_kappa([], []) is None


def test_four_fifths_rule_with_large_groups() -> None:
    step = step_result(GENDER, "applied", "screening", LABELS, BiasSettings())
    by = {g.group: g for g in step.groups}
    assert step.reference_group == "man"
    assert (by["man"].rate, by["man"].impact_ratio, by["man"].judgement) == (0.4, 1.0, "reference")
    woman = by["woman"]
    assert (woman.base, woman.selected, woman.rate) == (40, 10, 0.25)
    assert woman.impact_ratio == 0.625
    assert woman.judgement == "below_four_fifths"
    assert woman.small_sample is False
    assert woman.fisher_p is None
    # Prefer not to say and unanswered are counted, never compared.
    assert by["prefer_not_to_say"].judgement == "not_compared"
    assert by["prefer_not_to_say"].impact_ratio is None
    assert by["unanswered"].base == 20


def test_groups_under_five_are_suppressed() -> None:
    step = step_result(GENDER, "applied", "screening", LABELS, BiasSettings())
    another = next(g for g in step.groups if g.group == "another")
    assert another.suppressed is True
    assert (another.base, another.selected, another.rate, another.impact_ratio) == (
        None,
        None,
        None,
        None,
    )
    assert another.judgement == "suppressed"


def test_small_samples_get_fisher_or_too_small_to_judge() -> None:
    step = step_result(GENDER, "screening", "shortlist", LABELS, BiasSettings())
    woman = next(g for g in step.groups if g.group == "woman")
    # 2 of 10 against 10 of 20: ratio 0.4, but both groups are under 30.
    assert woman.impact_ratio == 0.4
    assert woman.small_sample is True
    assert woman.fisher_p == pytest.approx(0.235270, abs=1e-6)
    assert woman.judgement == "too_small_to_judge"
    # A clear difference on a small sample: reported with its p-value.
    clear = {"woman": {"a": 25, "b": 0}, "man": {"a": 25, "b": 20}}
    result = step_result(clear, "a", "b", LABELS, BiasSettings())
    w = next(g for g in result.groups if g.group == "woman")
    assert w.fisher_p is not None
    assert w.fisher_p < 0.05
    assert w.judgement == "below_four_fifths"
    # The minimum is configurable: with 5 the large-group rule applies.
    relaxed = step_result(GENDER, "screening", "shortlist", LABELS, BiasSettings(min_sample=5))
    assert next(g for g in relaxed.groups if g.group == "woman").judgement == "below_four_fifths"


def test_no_reference_when_every_compared_group_is_suppressed() -> None:
    step = step_result({"woman": {"a": 3, "b": 1}}, "a", "b", LABELS, BiasSettings())
    assert step.reference_group is None
    assert [g.judgement for g in step.groups] == ["suppressed"]


def test_the_audit_pack_holds_counts_results_json_and_the_method() -> None:
    report = build(
        {"gender": GENDER},
        ORDER,
        applications=121,
        settings=BiasSettings(),
        scope={"job_id": None, "since": "2026-09-01", "until": None},
        now=NOW,
    )
    pack = audit_pack(report)
    with zipfile.ZipFile(io.BytesIO(pack)) as archive:
        assert sorted(archive.namelist()) == ["METHOD.md", "data.csv", "report.json", "results.csv"]
        data = list(csv.DictReader(io.StringIO(archive.read("data.csv").decode())))
        results = list(csv.DictReader(io.StringIO(archive.read("results.csv").decode())))
        method = archive.read("METHOD.md").decode()
        body = json.loads(archive.read("report.json"))
    first = {r["group"]: r for r in data if r["from_stage"] == "applied"}
    assert (first["woman"]["base"], first["woman"]["selected"]) == ("40", "10")
    assert (first["another"]["base"], first["another"]["suppressed"]) == ("", "True")
    woman = next(r for r in results if r["group"] == "woman" and r["from_stage"] == "applied")
    assert (woman["impact_ratio"], woman["judgement"]) == ("0.625", "below_four_fifths")
    assert "does not by itself show compliance with any law" in " ".join(method.split())
    assert "Applied from: 2026-09-01" in method
    assert body["applications"] == 121
    assert body["settings"] == {"min_sample": 30, "min_group": 5}
    # Counts only: no application ids or references anywhere in the pack.
    assert "application" not in archive_text(pack).replace("applications", "")
    # The same report always gives the same file (a stable digest for the audit log).
    assert audit_pack(report) == pack


def archive_text(pack: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(pack)) as archive:
        return "".join(archive.read(n).decode() for n in archive.namelist() if n != "METHOD.md")
