# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The scoring prompt's contract: what the model is given and what it must answer.

Prompt ``hire_cv_score/v1`` (``pairly_hire/prompts``). Inputs (``score_inputs``):

* ``rubric``: the job title, the scale (0 to 4) and, per criterion, its id, name,
  description, what evidence looks like and the anchor text of every score. Weights
  are left out: the model scores each criterion on its own and never weighs them.
* ``document``: the blinded CV text (``pairly_hire.screening.blind``) as the data block
  the guardrails define, never instructions.

Output (``CvScoreOutput``): per criterion a score, a confidence, verbatim evidence
quotes and a short "why not higher". There is no overall score in the output on
purpose: the overall score is computed in code (``pairly_hire.scoring.aggregate``).
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from pairly_core.llm.types import TaskSpec
from pairly_hire.rubric import SCALE_MAX, SCALE_MIN, Rubric

SCORE_TASK_NAME: Final = "hire_cv_score"
SCORE_PROMPT_VERSION: Final = "v1"
# Small tier first at temperature 0; the model's own low confidence escalates the call.
SCORE_TASK: Final = TaskSpec(
    name=SCORE_TASK_NAME, prompt_version=SCORE_PROMPT_VERSION, confidence_threshold=0.5
)
MAX_QUOTES: Final = 5
MAX_QUOTE_CHARS: Final = 240  # the evidence locator's limit
MAX_WHY_NOT_HIGHER: Final = 500

_Quote = Annotated[str, StringConstraints(min_length=1, max_length=MAX_QUOTE_CHARS)]


class CriterionScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(max_length=48)
    score: int = Field(ge=SCALE_MIN, le=SCALE_MAX)
    confidence: float = Field(ge=0, le=1)
    evidence: list[_Quote] = Field(default_factory=list, max_length=MAX_QUOTES)
    why_not_higher: str = Field(default="", max_length=MAX_WHY_NOT_HIGHER)


class CvScoreOutput(BaseModel):
    """The ``hire_cv_score`` prompt's output schema."""

    model_config = ConfigDict(extra="forbid")

    criteria: list[CriterionScore] = Field(max_length=24)
    # How sure the model is about the scoring as a whole (routing only, never a score).
    confidence: float = Field(ge=0, le=1)


def rubric_input(rubric: Rubric) -> dict[str, Any]:
    return {
        "job_title": rubric.job_title,
        "scale": {"min": SCALE_MIN, "max": SCALE_MAX},
        "criteria": [
            {
                "id": c.id,
                "name": c.name,
                "description": c.description,
                "evidence": c.evidence,
                "anchors": {str(i): text for i, text in enumerate(c.scale_anchors)},
            }
            for c in rubric.criteria
        ],
    }


def score_inputs(rubric: Rubric, data_block: str) -> dict[str, Any]:
    """The router inputs of a scoring call (also what recordings are keyed on)."""
    return {"rubric": rubric_input(rubric), "document": data_block}
