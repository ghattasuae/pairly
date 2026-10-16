# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The application form builder (brief 3.2.1): what a job's application form asks.

A form has four parts:

* ``standard``: the contact fields. Full name and email are always asked and required;
  phone, location and a profile link are required, optional or hidden per job.
* ``questions``: custom questions (``text``, ``choice``, ``yes_no``).
* ``files``: the CV is always asked and required (``cv``); a job may add up to three
  optional files (a portfolio, a cover letter).
* ``self_identification``: the voluntary categories (``pairly_hire.selfid.schema``).
  Always optional, stored apart, never shown to or used by anything that assesses.

Knockout questions are not defined here: they belong to the locked rubric
(``pairly_hire.rubric.KnockoutQuestion``) and the public form shows them after the
custom questions. An answer that differs from what the job needs sends the application
to a person (``needs_human_review``), never to a rejection.

Forms are versioned like rubrics: saving a form publishes a policy version (kind
``hire_application_form``, name ``hire.job.<job id hex>``); applications record the form
version they answered.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from pairly_core.errors import BadRequestError
from pairly_core.policy_versions import PolicyError
from pairly_hire.rubric import Rubric
from pairly_hire.selfid.schema import CATEGORIES, DEFAULT_CATEGORIES

POLICY_KIND: Final = "hire_application_form"
CV_FIELD: Final = "cv"
MAX_QUESTIONS: Final = 20
MAX_EXTRA_FILES: Final = 3
MAX_TEXT_ANSWER: Final = 2000

_ID = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,47}$")]
_LABEL = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
_LABEL_AR = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class FieldMode(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    HIDDEN = "hidden"


class StandardFields(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phone: FieldMode = FieldMode.OPTIONAL
    location: FieldMode = FieldMode.OPTIONAL
    profile_url: FieldMode = FieldMode.OPTIONAL


class ChoiceOption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: _ID
    label: _LABEL
    label_ar: _LABEL_AR = ""


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: _ID
    kind: Literal["text", "choice", "yes_no"]
    label: _LABEL
    label_ar: _LABEL_AR = ""
    required: bool = False
    options: list[ChoiceOption] = Field(default_factory=list, max_length=20)
    max_length: int = Field(default=1000, ge=1, le=MAX_TEXT_ANSWER)

    @model_validator(mode="after")
    def _options_fit_kind(self) -> Question:
        if self.kind == "choice":
            if len(self.options) < 2:
                raise ValueError(f"question {self.id}: a choice needs at least two options")
            if len({o.id for o in self.options}) != len(self.options):
                raise ValueError(f"question {self.id}: option ids must be unique")
        elif self.options:
            raise ValueError(f"question {self.id}: only choice questions have options")
        return self


class FileField(BaseModel):
    """An optional extra file. The CV is implicit and always required."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: _ID
    label: _LABEL
    label_ar: _LABEL_AR = ""


class SelfIdSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    categories: list[str] = Field(default_factory=lambda: list(DEFAULT_CATEGORIES))

    @model_validator(mode="after")
    def _known(self) -> SelfIdSection:
        unknown = [c for c in self.categories if c not in CATEGORIES]
        if unknown:
            raise ValueError(f"unknown self-identification categories: {unknown}")
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("self-identification categories must be unique")
        return self

    @property
    def active(self) -> tuple[str, ...]:
        return tuple(self.categories) if self.enabled else ()


class ApplicationForm(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    standard: StandardFields = Field(default_factory=StandardFields)
    questions: list[Question] = Field(default_factory=list, max_length=MAX_QUESTIONS)
    files: list[FileField] = Field(default_factory=list, max_length=MAX_EXTRA_FILES)
    self_identification: SelfIdSection = Field(default_factory=SelfIdSection)

    @model_validator(mode="after")
    def _unique(self) -> ApplicationForm:
        ids = [q.id for q in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("question ids must be unique")
        file_ids = [f.id for f in self.files]
        if CV_FIELD in file_ids or len(set(file_ids)) != len(file_ids):
            raise ValueError("file ids must be unique and not 'cv'")
        return self

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=100
        )

    @property
    def file_ids(self) -> frozenset[str]:
        return frozenset({CV_FIELD, *(f.id for f in self.files)})


DEFAULT_FORM: Final = ApplicationForm()


def parse_form(text: str) -> ApplicationForm:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise PolicyError("the form is not valid YAML") from None
    if not isinstance(data, dict):
        raise PolicyError("the form must be a mapping")
    try:
        return ApplicationForm.model_validate(data)
    except ValueError as exc:
        errors = getattr(exc, "errors", None)
        message = "invalid form"
        if callable(errors) and errors():
            first = errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            message = f"invalid form: {where}: {first.get('msg', 'invalid value')}"
        raise PolicyError(message) from None


def form_validator() -> Callable[[str], ApplicationForm]:
    return parse_form


# ------------------------------------------------------------------ answers


def clean_text(value: str, limit: int) -> str:
    text = _CONTROL_RE.sub("", value).strip()
    if len(text) > limit:
        raise BadRequestError(f"an answer is longer than {limit} characters")
    return text


@dataclass(frozen=True, slots=True)
class KnockoutResult:
    id: str
    answer: str
    expected: str

    @property
    def matches(self) -> bool:
        return self.answer == self.expected


@dataclass(frozen=True, slots=True)
class CheckedAnswers:
    """Validated answers to the custom and knockout questions (never self-ID)."""

    questions: dict[str, Any] = field(default_factory=dict)
    knockouts: list[KnockoutResult] = field(default_factory=list)

    def stored(self) -> dict[str, Any]:
        return {
            "questions": dict(self.questions),
            "knockouts": {k.id: k.answer for k in self.knockouts},
        }

    @property
    def review_reasons(self) -> list[str]:
        """One reason per knockout answer that differs from what the job needs."""
        return [f"knockout:{k.id}" for k in self.knockouts if not k.matches]


def check_answers(
    form: ApplicationForm,
    rubric: Rubric,
    questions: Mapping[str, Any],
    knockouts: Mapping[str, Any],
) -> CheckedAnswers:
    """Validate answers against the form and the locked rubric, or raise ``BadRequestError``.

    Unknown question ids are refused (a stale form is better caught than ignored).
    Every knockout question must be answered yes or no; the answer is kept as given and
    only compared, never acted on.
    """
    known = {q.id: q for q in form.questions}
    unknown = set(questions) - set(known)
    if unknown:
        raise BadRequestError("the application answers a question this form does not ask")
    checked: dict[str, Any] = {}
    for question in form.questions:
        value = questions.get(question.id)
        if value is None or (isinstance(value, str) and not value.strip()):
            if question.required:
                raise BadRequestError(f"question {question.id} needs an answer")
            continue
        if question.kind == "text":
            if not isinstance(value, str):
                raise BadRequestError(f"question {question.id} needs text")
            checked[question.id] = clean_text(value, question.max_length)
        elif question.kind == "yes_no":
            if value not in ("yes", "no"):
                raise BadRequestError(f"question {question.id} needs yes or no")
            checked[question.id] = value
        else:
            if value not in {o.id for o in question.options}:
                raise BadRequestError(f"question {question.id} needs one of its options")
            checked[question.id] = value
    expected = {k.id: k.expected_answer for k in rubric.knockout_questions}
    if set(knockouts) - set(expected):
        raise BadRequestError("the application answers a knockout question the job does not ask")
    results: list[KnockoutResult] = []
    for knockout_id, want in expected.items():
        answer = knockouts.get(knockout_id)
        if answer not in ("yes", "no"):
            raise BadRequestError(f"knockout question {knockout_id} needs yes or no")
        results.append(KnockoutResult(knockout_id, str(answer), want))
    return CheckedAnswers(questions=checked, knockouts=results)
