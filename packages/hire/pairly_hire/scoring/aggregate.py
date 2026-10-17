# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The overall score: computed in code from criterion scores and weights, never by a model.

``overall_score(rubric, scores)`` is the weighted mean of the criterion scores as a
percentage of the scale maximum, rounded to two decimals:

    overall = round(100 * sum(weight * score) / (sum(weight) * SCALE_MAX), 2)

A criterion without a score counts as 0 (no evidence, no credit). The synthetic labels
(``synth.hire.labels``) import this very function, so the data and the product cannot
compute it differently. ``breakdown`` gives the same number with each criterion's share,
for the explainability panel and the decision record.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pairly_hire.rubric import SCALE_MAX, SCALE_MIN, Rubric


class ScoreRangeError(ValueError):
    """A criterion score is outside the scale (a programming error upstream)."""


def _checked(score: int) -> int:
    if isinstance(score, bool) or not isinstance(score, int):
        raise ScoreRangeError("criterion scores are whole numbers")
    if not SCALE_MIN <= score <= SCALE_MAX:
        raise ScoreRangeError(f"criterion scores are {SCALE_MIN} to {SCALE_MAX}")
    return score


def overall_score(rubric: Rubric, scores: Mapping[str, int]) -> float:
    """Deterministic overall score, 0 to 100. A missing criterion counts as 0."""
    criteria = rubric.criteria
    total = sum(c.weight for c in criteria)
    points = sum(c.weight * _checked(scores.get(c.id, 0)) for c in criteria)
    return round(100 * points / (total * SCALE_MAX), 2)


@dataclass(frozen=True, slots=True)
class CriterionShare:
    criterion_id: str
    weight: int
    score: int
    # Points this criterion adds to the overall score (0 to 100 scale, unrounded sum).
    points: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "weight": self.weight,
            "score": self.score,
            "points": round(self.points, 4),
        }


def breakdown(rubric: Rubric, scores: Mapping[str, int]) -> tuple[float, list[CriterionShare]]:
    """The overall score and what each criterion contributes to it."""
    total = sum(c.weight for c in rubric.criteria)
    shares = [
        CriterionShare(
            c.id,
            c.weight,
            _checked(scores.get(c.id, 0)),
            100 * c.weight * _checked(scores.get(c.id, 0)) / (total * SCALE_MAX),
        )
        for c in rubric.criteria
    ]
    return overall_score(rubric, scores), shares
