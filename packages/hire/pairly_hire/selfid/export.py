# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""A candidate's own self-identification answers, for their data export (WP2.9).

The one reader of ``hire_self_identification`` besides bias reporting
(``pairly_hire.selfid.reporting``, counts per group only): the candidate portal's export
(``pairly_hire.portal.service.export_data``) gives a candidate back the answers they
gave, as data protection law requires. Nothing else may import this module: the
import contracts in the root ``pyproject.toml`` keep it away from scoring, screening,
the pipeline, messaging and scheduling, and ``tests/test_selfid_isolation.py`` checks
that the portal service is its only importer.

``subject_answers`` reads only the rows of the applications it is given (the caller's
own, resolved from the candidate's portal session) and, under the portal subject, Row
Level Security shows only that candidate's rows anyway (hire_0004b). It returns option
labels, never anything about other people, and never aggregates.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.crypto.types import unseal
from pairly_hire.selfid.models import HireSelfIdentification
from pairly_hire.selfid.schema import CATEGORIES

__all__ = ["subject_answers"]


async def subject_answers(
    session: AsyncSession, application_ids: Sequence[uuid.UUID], *, locale: str = "en"
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """The answers per application: ``[{category, question, answer, answer_label}]``.

    Applications without answers are left out. Unknown categories or options (an older
    form) keep their stored id as the label."""
    if not application_ids:
        return {}
    rows = await session.scalars(
        select(HireSelfIdentification).where(
            HireSelfIdentification.application_id.in_(list(application_ids))
        )
    )
    arabic = locale == "ar"
    out: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for row in rows:
        answers = unseal(row, "answers")
        if not isinstance(answers, dict):
            continue
        items: list[dict[str, Any]] = []
        for key, value in sorted(answers.items()):
            category = CATEGORIES.get(str(key))
            option = (
                next((o for o in category.options if o.id == value), None) if category else None
            )
            items.append(
                {
                    "category": str(key),
                    "question": (category.label_ar if arabic else category.label_en)
                    if category
                    else str(key),
                    "answer": str(value),
                    "answer_label": (option.label_ar if arabic else option.label_en)
                    if option
                    else str(value),
                }
            )
        if items:
            out[row.application_id] = items
    return out
