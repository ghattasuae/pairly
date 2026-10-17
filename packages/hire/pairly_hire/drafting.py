# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""AI drafts through the model router: job descriptions and rubrics (brief 3.1.2, 3.1.4).

Both are suggestions for a person. Nothing a model writes is published or locked
without a person's action (``pairly_hire.api.jobs``: a recruiter saves the job text; a
person edits and locks the rubric).

* ``draft_job_description(router, org_id, brief)``: prompt ``hire_jd_draft/v1``,
  output ``JobDescriptionDraft`` (English and Arabic), then the deterministic
  inclusive-language and sanity checks run on the draft, so the recruiter sees both.
* ``draft_rubric(router, org_id, title, description)``: prompt ``hire_rubric_draft/v1``,
  output a ``Rubric`` (``pairly_hire.rubric``).

The router validates every output against its schema and logs every call
(``model_calls``: prompt version and hash, model, inputs, output, cost). It fails
closed with ``ModelUnavailable`` (no key, no recording in recorded mode, budget): the
routes answer 503 with a plain message and the person writes the text themselves.

Offline: in ``recorded`` mode only the recorded requests answer. ``pairly_hire.demo``
holds the demo briefs and answers, and ``pairly_hire.recordings`` builds the bundle.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from pairly_core.llm.types import CallContext, RouterResult, TaskSpec
from pairly_hire.language import Finding
from pairly_hire.language.inclusive import check_inclusive
from pairly_hire.language.sanity import check_sanity
from pairly_hire.rubric import Rubric

if TYPE_CHECKING:
    from pairly_core.llm.router import ModelRouter

JD_TASK: Final = TaskSpec(name="hire_jd_draft", prompt_version="v1")
RUBRIC_TASK: Final = TaskSpec(name="hire_rubric_draft", prompt_version="v1")
MAX_BRIEF: Final = 800
WORKFLOW: Final = "hire"

_Item = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class JobDescriptionDraft(BaseModel):
    """What the model returns for a brief. Every field is shown to the recruiter."""

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    title_ar: Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)] = ""
    summary: Annotated[str, StringConstraints(strip_whitespace=True, max_length=800)]
    responsibilities: list[_Item] = Field(min_length=1, max_length=10)
    requirements: list[_Item] = Field(min_length=1, max_length=10)
    nice_to_have: list[_Item] = Field(default_factory=list, max_length=8)
    description_en: Annotated[str, StringConstraints(min_length=1, max_length=8000)]
    description_ar: Annotated[str, StringConstraints(max_length=8000)] = ""


@dataclass(frozen=True, slots=True)
class TextChecks:
    inclusive_en: list[Finding]
    inclusive_ar: list[Finding]
    sanity_en: list[Finding]
    sanity_ar: list[Finding]

    def as_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "inclusive_en": [f.as_dict() for f in self.inclusive_en],
            "inclusive_ar": [f.as_dict() for f in self.inclusive_ar],
            "sanity_en": [f.as_dict() for f in self.sanity_en],
            "sanity_ar": [f.as_dict() for f in self.sanity_ar],
        }


def check_texts(
    *,
    title: str,
    description_en: str,
    description_ar: str = "",
    title_ar: str = "",
    today: date | None = None,
) -> TextChecks:
    """The deterministic checks of a job's English and Arabic text."""
    return TextChecks(
        inclusive_en=check_inclusive(f"{title}\n{description_en}", lang="en"),
        inclusive_ar=check_inclusive(f"{title_ar}\n{description_ar}", lang="ar"),
        sanity_en=check_sanity(description_en, title=title, today=today),
        sanity_ar=check_sanity(description_ar, title=title_ar or title, today=today),
    )


def jd_inputs(brief: str) -> dict[str, str]:
    """Only the brief: the organisation's name is not needed to draft, and keeping the
    inputs to what the recruiter typed makes recorded drafts work in any organisation."""
    return {"brief": brief.strip()}


def rubric_inputs(title: str, description: str) -> dict[str, str]:
    return {"job_title": title.strip(), "description": description.strip()}


def _context(actor_id: uuid.UUID | None, entity_id: uuid.UUID | None) -> CallContext:
    return CallContext(
        actor_id=str(actor_id) if actor_id else None,
        workflow=WORKFLOW,
        entity_type="hire.job" if entity_id else None,
        entity_id=str(entity_id) if entity_id else None,
    )


async def draft_job_description(
    router: ModelRouter,
    org_id: uuid.UUID,
    *,
    brief: str,
    actor_id: uuid.UUID | None = None,
) -> RouterResult[JobDescriptionDraft]:
    return await router.complete(
        task=JD_TASK,
        inputs=jd_inputs(brief),
        schema=JobDescriptionDraft,
        org_id=org_id,
        context=_context(actor_id, None),
    )


async def draft_rubric(
    router: ModelRouter,
    org_id: uuid.UUID,
    *,
    job_id: uuid.UUID,
    title: str,
    description: str,
    actor_id: uuid.UUID | None = None,
) -> RouterResult[Rubric]:
    return await router.complete(
        task=RUBRIC_TASK,
        inputs=rubric_inputs(title, description),
        schema=Rubric,
        org_id=org_id,
        context=_context(actor_id, job_id),
    )
