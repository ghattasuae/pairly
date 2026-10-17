# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The matching eval (``evals.pay.matching``) on the synthetic matching cases."""

from __future__ import annotations

from functools import cache

from evals.pay.matching import evaluate
from synth.pay.dataset import Dataset, build


@cache
def dataset() -> Dataset:
    return build()


def test_every_scenario_ends_as_its_case_expects() -> None:
    result = evaluate(dataset())
    assert result["error_count"] == 0
    assert result["cases"] == len(dataset().cases)
    for scenario, counts in result["by_scenario"].items():
        assert counts["as_expected"] == counts["cases"], scenario
    assert result["exception_recall"] == 1.0
    assert result["exception_precision"] == 1.0
    assert result["match_rate"] == 1.0
    assert result["invoices_without_case_flagged"] == []
