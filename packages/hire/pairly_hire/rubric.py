# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The hiring rubric: the canonical model every Hire component uses (brief 3.1.4).

A rubric says what a job needs and how to recognise it in an application:

* ``must_haves``: criteria a candidate is expected to meet, each with a weight and a
  description of what evidence looks like.
* ``nice_to_haves``: criteria that add to a candidate's fit, same shape.
* ``knockout_questions``: yes or no questions for the application form, each with the
  answer the job needs. A different answer never rejects anybody: it sends the
  application to a person with the reason (brief hard rule 1).

The scale: screening scores every criterion from ``SCALE_MIN`` (0) to ``SCALE_MAX`` (4).
What each score means is an anchor description: ``DEFAULT_ANCHORS`` for every
criterion, or the criterion's own ``anchors`` (five texts, for scores 0 to 4) when the
rubric gives them. The synthetic sample jobs (``synth/hire/jobs``) use the same scale
and anchors of the same shape (``label_guide``). The overall score is never a model's:
it is computed from the criterion scores and weights (``pairly_hire.scoring.aggregate``).

Lifecycle (``pairly_hire.rubrics``): the AI drafts a rubric from the job description,
a person edits it, then locks it. Locking publishes the text as an immutable policy
version (``pairly_core.policy_versions``, kind ``hire_rubric``). Applications record the
locked version they arrived under and are only ever scored against a locked version. A
later lock creates a new version; nothing is rescored silently.

The stored form is canonical YAML (``to_yaml``), so a reviewer reads exactly what was
locked, and ``rubric_validator`` refuses anything that does not parse into this model.

Never part of a rubric: anything about protected characteristics. A criterion is about
skills, experience and evidence, and the voluntary self-identification answers are
kept in a separate table that scoring code cannot import (``pairly_hire.selfid``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Annotated, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from pairly_core.policy_versions import PolicyError

POLICY_KIND: Final = "hire_rubric"
SCHEMA_VERSION: Final = 1
MAX_CRITERIA: Final = 12
MAX_KNOCKOUTS: Final = 6
SCALE_MIN: Final = 0
SCALE_MAX: Final = 4
# What each score means when a criterion gives no anchors of its own (index = score).
DEFAULT_ANCHORS: Final[tuple[str, ...]] = (
    "No evidence in the application.",
    "Mentioned only, for example listed as a skill, with no example of using it.",
    "Some evidence: one concrete example, or a short period of relevant work.",
    "Good evidence: several concrete examples, or a solid period of relevant work.",
    "Strong evidence: many concrete examples, or a long period of relevant work.",
)

_ID_RE = r"^[a-z][a-z0-9_]{0,47}$"

CriterionId = Annotated[str, StringConstraints(pattern=_ID_RE)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)]
OptionalShort = Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)]
AnchorText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]


class Criterion(BaseModel):
    """One thing the job needs, how much it counts, and what evidence of it looks like."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: CriterionId
    name: ShortText
    # What the criterion means for this job.
    description: LongText
    # What evidence in an application looks like (brief 3.1.4), for people and scoring.
    evidence: LongText
    # Relative weight, 1 (a little) to 10 (a lot).
    weight: int = Field(ge=1, le=10)
    # What scores 0 to 4 mean for this criterion (index = score). Empty: the defaults.
    anchors: tuple[AnchorText, ...] = ()

    @model_validator(mode="after")
    def _anchor_levels(self) -> Criterion:
        if self.anchors and len(self.anchors) != SCALE_MAX - SCALE_MIN + 1:
            raise ValueError("anchors must describe every score from 0 to 4, in order")
        return self

    @property
    def scale_anchors(self) -> tuple[str, ...]:
        """The anchor text of each score, index = score."""
        return self.anchors or DEFAULT_ANCHORS


class KnockoutQuestion(BaseModel):
    """A yes or no question with the answer the job needs. A mismatch goes to a person."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: CriterionId
    question: LongText
    question_ar: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1200)] = ""
    expected_answer: Literal["yes", "no"]
    # Why the job needs it, shown to the reviewer.
    reason: LongText


class Rubric(BaseModel):
    """A rubric for one job. See the module docstring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    job_title: ShortText
    must_haves: list[Criterion] = Field(min_length=1, max_length=MAX_CRITERIA)
    nice_to_haves: list[Criterion] = Field(default_factory=list, max_length=MAX_CRITERIA)
    knockout_questions: list[KnockoutQuestion] = Field(
        default_factory=list, max_length=MAX_KNOCKOUTS
    )

    @model_validator(mode="after")
    def _unique_ids(self) -> Rubric:
        ids = [c.id for c in (*self.must_haves, *self.nice_to_haves)]
        if len(ids) != len(set(ids)):
            raise ValueError("criterion ids must be unique")
        knockout_ids = [k.id for k in self.knockout_questions]
        if len(knockout_ids) != len(set(knockout_ids)):
            raise ValueError("knockout question ids must be unique")
        return self

    @property
    def criteria(self) -> list[Criterion]:
        return [*self.must_haves, *self.nice_to_haves]

    def to_yaml(self) -> str:
        """Canonical YAML (stable key order, Unicode kept), the text that gets locked.
        Criteria without anchors of their own leave the key out."""
        data = self.model_dump(mode="json")
        for group in ("must_haves", "nice_to_haves"):
            for item in data[group]:
                if not item.get("anchors"):
                    item.pop("anchors", None)
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def parse_rubric(text: str) -> Rubric:
    """Parse rubric YAML or raise ``PolicyError`` with a message safe to show."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise PolicyError("the rubric is not valid YAML") from None
    if not isinstance(data, dict):
        raise PolicyError("the rubric must be a mapping")
    try:
        return Rubric.model_validate(data)
    except ValueError as exc:
        raise PolicyError(_first_error(exc)) from None


def rubric_validator() -> Callable[[str], Rubric]:
    """The ``validate`` callable ``publish_policy_version`` runs before it stores a rubric."""
    return parse_rubric


def _first_error(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        found = errors()
        if found:
            first = found[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            message = str(first.get("msg", "invalid value"))
            return f"invalid rubric: {where}: {message}" if where else f"invalid rubric: {message}"
    return "invalid rubric"


_NAME_SAFE = re.compile(r"[^a-z0-9]")


def policy_name(job_id_hex: str) -> str:
    """The policy name of a job's rubric versions: ``hire.job.<job id hex>``."""
    return f"hire.job.{_NAME_SAFE.sub('', job_id_hex.lower())}"
