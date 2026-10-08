# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pydantic models for the synthetic Hire data: jobs and rubrics, CV ground truth,
rubric labels and the manifest records.

The rubric models are the YAML shape a locked rubric uses (``synth/hire/jobs/*.yaml``).
The hire plugin adopts or mirrors them in WP2.2. Rules the shape enforces:

* scores are integers 0 to 4 on every criterion, each level has an anchor sentence;
* weights are positive; the overall score is computed deterministically
  (``Rubric.overall``), never by a model;
* every criterion says what counts as evidence; no evidence means no credit;
* knockout questions never reject: a failed knockout routes to a person
  (``on_fail`` is always ``human_review``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ID = r"^[a-z][a-z0-9_]{1,47}$"
_MONTH = re.compile(r"^\d{4}(-(0[1-9]|1[0-2]))?$")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ------------------------------------------------------------------ jobs and rubrics


class ScoreAnchor(_Model):
    score: int = Field(ge=0, le=4)
    description: str = Field(min_length=3, max_length=300)


class Criterion(_Model):
    id: str = Field(pattern=_ID)
    name: str = Field(min_length=2, max_length=120)
    name_ar: str | None = Field(default=None, max_length=160)
    kind: Literal["must_have", "nice_to_have"]
    weight: float = Field(gt=0, le=100)
    description: str = Field(min_length=3, max_length=600)
    # What counts as evidence in a CV (quoted text with its location).
    evidence: str = Field(min_length=3, max_length=600)
    anchors: tuple[ScoreAnchor, ...]

    @field_validator("anchors")
    @classmethod
    def _five_levels(cls, value: tuple[ScoreAnchor, ...]) -> tuple[ScoreAnchor, ...]:
        if [a.score for a in value] != [0, 1, 2, 3, 4]:
            raise ValueError("anchors must describe scores 0, 1, 2, 3 and 4 in order")
        return value


class KnockoutQuestion(_Model):
    id: str = Field(pattern=_ID)
    question: str = Field(min_length=3, max_length=300)
    question_ar: str | None = Field(default=None, max_length=400)
    answer_type: Literal["yes_no", "number"]
    # yes_no: the answer that passes. number: the inclusive bounds that pass.
    pass_if_yes: bool | None = None
    min_value: float | None = None
    max_value: float | None = None
    # A failed knockout is never a rejection: it goes to a person with a suggestion.
    on_fail: Literal["human_review"] = "human_review"

    @model_validator(mode="after")
    def _rule(self) -> KnockoutQuestion:
        if self.answer_type == "yes_no" and self.pass_if_yes is None:
            raise ValueError("a yes_no knockout needs pass_if_yes")
        if self.answer_type == "number" and self.min_value is None and self.max_value is None:
            raise ValueError("a number knockout needs min_value or max_value")
        return self

    def passes(self, answer: bool | float | int | None) -> bool | None:
        """True or False, or None when there is no answer (a person decides)."""
        if answer is None:
            return None
        if self.answer_type == "yes_no":
            return bool(answer) is self.pass_if_yes
        value = float(answer)
        if self.min_value is not None and value < self.min_value:
            return False
        return not (self.max_value is not None and value > self.max_value)


class Rubric(_Model):
    schema_version: Literal[1] = 1
    version: int = Field(ge=1)
    scale_min: Literal[0] = 0
    scale_max: Literal[4] = 4
    # Overall = weighted mean of criterion scores, as a percentage of the scale maximum.
    overall: Literal["weighted_mean_percent"] = "weighted_mean_percent"
    criteria: tuple[Criterion, ...] = Field(min_length=1, max_length=20)
    knockouts: tuple[KnockoutQuestion, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def _ids(self) -> Rubric:
        ids = [c.id for c in self.criteria]
        if len(set(ids)) != len(ids):
            raise ValueError("criterion ids must be unique")
        kids = [k.id for k in self.knockouts]
        if len(set(kids)) != len(kids):
            raise ValueError("knockout ids must be unique")
        if not any(c.kind == "must_have" for c in self.criteria):
            raise ValueError("a rubric needs at least one must_have criterion")
        return self

    def overall_score(self, scores: Mapping[str, int]) -> float:
        """Deterministic overall score, 0 to 100. A missing criterion counts as 0."""
        total = sum(c.weight for c in self.criteria)
        points = sum(c.weight * scores.get(c.id, 0) for c in self.criteria)
        return round(100 * points / (total * self.scale_max), 2)


class Job(_Model):
    id: str = Field(pattern=_ID)
    title: str
    title_ar: str
    family: str
    seniority: Literal["junior", "mid", "senior", "lead"]
    location: str
    employment_type: Literal["full_time", "part_time", "contract"]
    summary: str
    rubric: Rubric


# ------------------------------------------------------------------ CV ground truth


class Link(_Model):
    kind: Literal["linkedin", "github", "portfolio"]
    url: str


class Contact(_Model):
    name: str
    # The name in the other script for bilingual CVs (Arabic for an English CV).
    name_alt: str | None = None
    email: str
    phone: str
    city: str
    country: str
    links: tuple[Link, ...] = ()


class ExperienceEntry(_Model):
    employer: str
    title: str
    # As displayed: "YYYY-MM" or "YYYY" (when the CV shows years only). None = present.
    start: str
    end: str | None
    location: str
    highlights: tuple[str, ...] = ()

    @field_validator("start", "end")
    @classmethod
    def _month(cls, value: str | None) -> str | None:
        if value is not None and not _MONTH.fullmatch(value):
            raise ValueError("dates are YYYY-MM or YYYY")
        return value


class Education(_Model):
    institution: str
    degree: str
    field: str
    start_year: int
    end_year: int


class Certification(_Model):
    name: str
    issuer: str
    year: int


class LanguageSkill(_Model):
    language: str
    level: str


class CvGroundTruth(_Model):
    """What a perfect CV parser extracts, in the CV's own language and script."""

    contact: Contact
    summary: str
    experience: tuple[ExperienceEntry, ...]
    education: tuple[Education, ...]
    skills: tuple[str, ...]
    certifications: tuple[Certification, ...] = ()
    languages: tuple[LanguageSkill, ...] = ()


class Persona(_Model):
    """Protected and personal attributes. Never an input to the labels (by construction)."""

    gender: Literal["female", "male"]
    nationality: str
    birth_year: int
    photo: bool
    name_locale: str
    # Shown on the CV as a "Personal details" section (common in Gulf CVs).
    personal_details_shown: bool


class CriterionLabel(_Model):
    criterion_id: str
    score: int = Field(ge=0, le=4)
    # Exact text from the CV that justifies the score (empty for a 0).
    evidence: tuple[str, ...] = ()
    # The kind of labeling rule that produced the score (years, demonstrated, credential,
    # language); its parameters are in manifests/summary.json (see synth/hire/labels.py).
    rule: str


class JobLabels(_Model):
    job_id: str
    rubric_version: int
    criteria: tuple[CriterionLabel, ...]
    overall: float
    knockout_answers: dict[str, bool | int | None]
    # "pass" or "human_review" (a failed or missing knockout); never "reject".
    knockout_route: Literal["pass", "human_review"]


class AttackInfo(_Model):
    attack_id: str
    techniques: tuple[str, ...]
    description: str
    language: Literal["en", "ar"]
    # Guardrail finding types a deterministic detector should flag (empty when the
    # attack is out of scope for guardrails, for example inconsistent dates).
    expected_findings: tuple[str, ...]
    # Which check is expected to catch it.
    caught_by: Literal["deterministic", "renderer", "classifier", "claim_consistency"]
    # Strings that must never reach a model (empty for claim attacks).
    payloads: tuple[str, ...] = ()
    # Claim attacks: what a claim consistency check should report.
    claim_flags: tuple[str, ...] = ()
    filename: str | None = None


class FairnessInfo(_Model):
    pair_id: str
    role: Literal["base", "variant"]
    # The attribute this variant changes (None for the base CV).
    dimension: Literal["name", "gender", "nationality", "photo", "age"] | None
    value: str


Format = Literal["pdf", "docx", "png", "jpg", "scan_pdf"]
Layout = Literal["single_column", "two_column", "table", "sidebar", "arabic", "bilingual", "scan"]


class DocumentRecord(_Model):
    id: str
    kind: Literal["cv", "attack", "fairness"]
    file: str
    format: Format
    layout: Layout
    language: Literal["en", "ar", "bilingual"]
    family: str
    seniority: Literal["junior", "mid", "senior", "lead"]
    # "logical": the text layer holds the text as written. "visual_shaped": Arabic is
    # drawn as shaped presentation forms in visual order (PDF). "image": OCR needed.
    text_layer: Literal["logical", "visual_shaped", "image"]
    ground_truth: CvGroundTruth
    persona: Persona
    labels: tuple[JobLabels, ...]
    attack: AttackInfo | None = None
    fairness: FairnessInfo | None = None
