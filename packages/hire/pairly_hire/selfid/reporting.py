# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Bias reporting's one reader of self-identification answers: counts per group only.

``stage_counts`` takes, from the caller, which stages each application reached (never
anything about who the candidate is) and gives back, for one category, how many
applications of each group reached each stage. Application ids go in; only counts come
out: no id, no answer, no row ever leaves this module. Applications without an answer
to the category count as ``unanswered``.

Its only importer is ``pairly_hire.analytics.bias`` (checked by
``tests/test_selfid_isolation.py``); scoring, screening, the pipeline, messaging and
scheduling may not import this package at all (import-linter contracts in the root
``pyproject.toml``). Suppressing small groups is the report's job (``analytics.bias``),
on these counts.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Collection, Mapping
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.crypto.types import unseal
from pairly_hire.selfid.models import HireSelfIdentification
from pairly_hire.selfid.schema import CATEGORIES, PREFER_NOT, Category

__all__ = ["PREFER_NOT", "UNANSWERED", "categories", "stage_counts"]

UNANSWERED: Final = "unanswered"
_CHUNK: Final = 1000


def categories() -> dict[str, Category]:
    return dict(CATEGORIES)


async def stage_counts(
    session: AsyncSession, reached: Mapping[uuid.UUID, Collection[str]], *, category: str
) -> dict[str, dict[str, int]]:
    """``{group: {stage: applications that reached it}}`` for ``category``. Groups are
    the category's option ids plus ``unanswered``."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown self-identification category {category!r}")
    valid = CATEGORIES[category].option_ids
    answer_of: dict[uuid.UUID, str] = {}
    ids = list(reached)
    for i in range(0, len(ids), _CHUNK):
        rows = await session.scalars(
            select(HireSelfIdentification).where(
                HireSelfIdentification.application_id.in_(ids[i : i + _CHUNK])
            )
        )
        for row in rows:
            answers = unseal(row, "answers")
            value = answers.get(category) if isinstance(answers, dict) else None
            if isinstance(value, str) and value in valid:
                answer_of[row.application_id] = value
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for app_id, stages in reached.items():
        group = answer_of.get(app_id, UNANSWERED)
        for stage in stages:
            counts[group][stage] += 1
    return {group: dict(per_stage) for group, per_stage in counts.items()}
