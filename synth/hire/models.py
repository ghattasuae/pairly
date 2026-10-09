# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pydantic models for the synthetic Hire data: jobs, CV ground truth, rubric labels
and the manifest records.

A job's rubric is the canonical Pairly Hire rubric (``pairly_hire.rubric.Rubric``): the
sample job YAMLs (``synth/hire/jobs/*.yaml``) hold exactly what a recruiter would lock,
and loading them validates it with the plugin's own model, so the two cannot drift.
synth depends on the plugin, never the other way round (``pairly_hire`` never imports
``synth``; ``packages/hire/tests/test_synth_hire_data.py`` checks it).

What only the synthetic data needs sits beside the rubric, in ``Job.label_guide``: the
meaning of each score 0 to 4 for the rule-based labeler (``synth.hire.labels``) and
the Arabic criterion names. Rules the labels follow:

* scores are integers 0 to 4 on every criterion; no evidence means no credit;
* the overall score is computed deterministically (``synth.hire.labels.overall_score``,
  the weighted mean as a percentage of the scale maximum), never by a model;
* a knockout answer that differs from the rubric's ``expected_answer`` never rejects:
  it routes the application to a person (``human_review``).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pairly_hire.rubric import Rubric

_ID = r"^[a-z][a-z0-9_]{1,47}$"
_MONTH = re.compile(r"^\d{4}(-(0[1-9]|1[0-2]))?$")

SCALE_MAX = 4


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ------------------------------------------------------------------ jobs


class CriterionGuide(_Model):
    """Synthetic-data notes on one rubric criterion: its Arabic name and the meaning of
    each score from 0 to ``SCALE_MAX`` (index = score)."""

    name_ar: str | None = Field(default=None, max_length=160)
    anchors: tuple[str, ...]

    @field_validator("anchors")
    @classmethod
    def _five_levels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != SCALE_MAX + 1 or not all(3 <= len(a) <= 300 for a in value):
            raise ValueError("anchors must describe scores 0, 1, 2, 3 and 4 in order")
        return value


class Job(_Model):
    id: str = Field(pattern=_ID)
    title: str
    title_ar: str
    family: str
    seniority: Literal["junior", "mid", "senior", "lead"]
    location: str
    employment_type: Literal["full_time", "part_time", "contract"]
    summary: str
    # The locked policy version this rubric stands for (labels record it).
    rubric_version: int = Field(default=1, ge=1)
    rubric: Rubric
    label_guide: dict[str, CriterionGuide]

    @model_validator(mode="after")
    def _consistent(self) -> Job:
        if self.rubric.job_title != self.title:
            raise ValueError("rubric.job_title must equal the job title")
        if set(self.label_guide) != {c.id for c in self.rubric.criteria}:
            raise ValueError("label_guide must cover exactly the rubric criteria")
        return self


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
    # Synthetic application answers, as an applicant gives them ("yes" or "no").
    knockout_answers: dict[str, Literal["yes", "no"]]
    # "pass" or "human_review" (an answer that differs from the rubric); never "reject".
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
