# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Calibration mode: agreement statistics per criterion, low-agreement rules, the
deterministic suggestion from the anchors, and the model suggestion's checks."""

from __future__ import annotations

import pytest

from pairly_hire.analytics import calibration_recordings
from pairly_hire.analytics.calibration import (
    Pair,
    agreement,
    check_suggestion,
    clarify_inputs,
    deterministic_suggestion,
)
from pairly_hire.demo import BACKEND
from pairly_hire.rubric import DEFAULT_ANCHORS


def pairs(ai: list[int], human: list[int], source: str = "label") -> list[Pair]:
    return [Pair("go", a, h, source) for a, h in zip(ai, human, strict=True)]  # type: ignore[arg-type]


def test_agreement_statistics_on_known_pairs() -> None:
    stats = agreement(
        "go", "Go", pairs([0, 1, 2, 3, 4, 0, 1, 2, 3, 4], [0, 1, 2, 3, 4, 1, 2, 3, 4, 4])
    )
    assert (stats.n, stats.exact, stats.within_one) == (10, 0.6, 1.0)
    assert stats.kappa == pytest.approx(0.5)
    assert stats.kappa_note is None
    assert stats.mean_difference == 0.4
    assert stats.direction == "mixed"
    assert stats.low_agreement is False
    assert stats.top_disagreement == {"ai": 0, "people": 1, "count": 1}


def test_low_agreement_and_direction() -> None:
    stats = agreement(
        "go", "Go", pairs([2, 2, 2, 2, 2, 1, 1, 1, 1, 1], [4, 4, 4, 3, 3, 3, 3, 2, 2, 2])
    )
    assert (stats.exact, stats.within_one) == (0.0, 0.5)
    assert stats.kappa == pytest.approx(-0.1765, abs=1e-4)
    assert stats.mean_difference == 1.5
    assert stats.direction == "people_higher"
    assert stats.low_agreement is True
    assert stats.top_disagreement == {"ai": 2, "people": 4, "count": 3}


def test_small_samples_never_get_kappa_or_a_low_label() -> None:
    few = agreement("go", "Go", pairs([0, 4, 0, 4], [4, 0, 4, 0], "override"))
    assert few.kappa is None
    assert few.kappa_note == "needs at least 10 pairs"
    assert few.low_agreement is False  # under 5 pairs
    assert few.sources == {"override": 4}
    same = agreement("go", "Go", pairs([2] * 10, [2] * 10))
    assert same.kappa is None
    assert same.kappa_note == "undefined: every score is the same"
    assert same.exact == 1.0
    empty = agreement("go", "Go", [])
    assert (empty.n, empty.exact, empty.kappa_note) == (0, None, "no pairs yet")


def test_deterministic_suggestion_quotes_the_anchors() -> None:
    criterion = BACKEND.rubric.criteria[0]
    stats = agreement(
        criterion.id,
        criterion.name,
        [Pair(criterion.id, a, h, "label") for a, h in [(2, 4)] * 4 + [(1, 3)] * 2],
    )
    text = deterministic_suggestion(criterion, stats)
    anchors = criterion.scale_anchors
    assert f'"{anchors[2]}"' in text
    assert f'"{anchors[4]}"' in text
    assert "People score it higher than the AI" in text
    assert "\u2014" not in text
    general = criterion.model_copy(update={"anchors": ()})
    assert "general anchors" in deterministic_suggestion(general, stats)
    assert general.scale_anchors == DEFAULT_ANCHORS


def test_model_suggestions_are_checked_and_see_no_candidate() -> None:
    criterion = BACKEND.rubric.criteria[0]
    inputs = clarify_inputs(criterion, "people_lower")
    assert set(inputs) == {
        "criterion",
        "description",
        "evidence",
        "anchors",
        "direction",
        "language",
    }
    assert check_suggestion("Name the projects that count as evidence.") == []
    assert "an em dash" in check_suggestion("Be concrete \u2014 name projects.")
    assert any("protected" in p for p in check_suggestion("Prefer married applicants."))
    assert check_suggestion("  ") == ["empty"]


def test_the_recorded_suggestions_are_current() -> None:
    assert calibration_recordings.main(["--check"]) == 0
