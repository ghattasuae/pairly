# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``ScoringInput``: everything scoring may know about one application, and nothing else.

Built from the application row and its locked rubric version. The fields are listed
explicitly (``ALLOWED_FIELDS``); a test checks that no self-identification category
name can appear in them, and the import contract keeps ``pairly_hire.selfid`` out of
this package.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, fields
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.crypto.types import unseal
from pairly_core.errors import ConflictError, NotFoundError
from pairly_core.policy_versions import get_policy
from pairly_hire.models import HireApplication
from pairly_hire.rubric import POLICY_KIND, Rubric, parse_rubric


@dataclass(frozen=True, slots=True)
class ScoringInput:
    application_id: uuid.UUID
    job_id: uuid.UUID
    rubric_policy_id: uuid.UUID
    rubric_version: int
    rubric: Rubric
    cv_document_id: uuid.UUID
    # Custom question answers by question id (text, choice or yes or no).
    answers: dict[str, Any] = field(default_factory=dict)
    # Knockout answers by knockout id ("yes" or "no"), compared with the rubric only.
    knockout_answers: dict[str, str] = field(default_factory=dict)


ALLOWED_FIELDS: Final = frozenset(f.name for f in fields(ScoringInput))


async def scoring_input(session: AsyncSession, application_id: uuid.UUID) -> ScoringInput:
    """The scoring view of an application. Only submitted applications with a locked
    rubric version can be scored; anything else raises (fail closed)."""
    app = await session.scalar(select(HireApplication).where(HireApplication.id == application_id))
    if app is None:
        raise NotFoundError("application not found")
    if app.cv_document_id is None or app.submitted_at is None:
        raise ConflictError("the application is not submitted yet")
    policy = await get_policy(session, app.rubric_policy_id)
    if policy is None or policy.kind != POLICY_KIND:
        raise ConflictError("the application has no locked rubric version")
    stored = unseal(app, "answers") or {}
    questions = stored.get("questions") if isinstance(stored, dict) else None
    knockouts = stored.get("knockouts") if isinstance(stored, dict) else None
    return ScoringInput(
        application_id=app.id,
        job_id=app.job_id,
        rubric_policy_id=policy.id,
        rubric_version=policy.version,
        rubric=parse_rubric(policy.content),
        cv_document_id=app.cv_document_id,
        answers=dict(questions) if isinstance(questions, dict) else {},
        knockout_answers=(
            {str(k): str(v) for k, v in knockouts.items()} if isinstance(knockouts, dict) else {}
        ),
    )
