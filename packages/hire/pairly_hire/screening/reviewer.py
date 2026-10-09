# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""What the pipeline and messaging read from screening (``pairly_hire.pipeline.hooks``).

* ``ScreeningBlindSource``: blind mode for the pipeline. ``enabled`` is the
  organisation's setting; ``blind_applications`` is ``service.is_blind`` for many
  applications in one query (``service.blind_application_ids``): the latest screening
  is blind (blind mode lifts at the shortlist), or there is none yet and the setting
  is on.
* ``ScreeningReviewerView``: the latest screening of an application as a
  ``ScreeningSummary`` for the candidate profile and a rejection's optional reason.
  Criterion results come from the credited scores (a person's override wins); quotes
  are the accepted evidence, which comes from the blinded CV text the model read.
* ``ScreeningTextRedactor``: the blind redactor for free text (``blindtext``) with the
  candidate's known identity from the contacts the pipeline passes plus the names,
  email, phone, links and home location the parsed CV gave (the latest screening's
  profile), and the organisation's university setting.

``register()`` installs all three; the plugin module calls it on import.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_hire.blindtext import Redactor, identity_sources, redactor
from pairly_hire.pipeline import hooks
from pairly_hire.rubric import SCALE_MAX
from pairly_hire.screening.models import HireScreening
from pairly_hire.screening.profile import ParsedProfile
from pairly_hire.screening.service import (
    blind_application_ids,
    read_details,
    screening_settings,
)

MAX_QUOTES = 3


class ScreeningBlindSource:
    async def enabled(self, session: AsyncSession) -> bool:
        return (await screening_settings(session)).blind.enabled

    async def blind_applications(
        self, session: AsyncSession, application_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        return await blind_application_ids(session, application_ids)


def criterion_result(score: int, flags: Sequence[str]) -> str:
    if "not_scored" in flags:
        return "unknown"
    if score >= SCALE_MAX - 1:
        return "met"
    if score > 0:
        return "partly_met"
    return "not_met"


class ScreeningReviewerView:
    async def summary(
        self, session: AsyncSession, application_id: uuid.UUID
    ) -> hooks.ScreeningSummary | None:
        row = await session.scalar(
            select(HireScreening)
            .where(HireScreening.application_id == application_id)
            .order_by(HireScreening.created_at.desc())
            .limit(1)
        )
        if row is None:
            return None
        details = read_details(row)
        scoring_raw = details.get("scoring")
        scoring: dict[str, Any] = scoring_raw if isinstance(scoring_raw, dict) else {}
        override_raw = details.get("override")
        override: dict[str, Any] = override_raw if isinstance(override_raw, dict) else {}
        overridden: dict[str, int] = dict(override.get("scores") or {})
        criteria: list[hooks.CriterionEvidence] = []
        for c in scoring.get("criteria", []):
            if not isinstance(c, dict):
                continue
            cid = str(c.get("criterion_id", ""))
            score = overridden[cid] if cid in overridden else int(c.get("score", 0))
            flags = [str(f) for f in c.get("flags", [])]
            quotes = tuple(
                str(e.get("quote", ""))
                for e in c.get("evidence", [])
                if isinstance(e, dict) and e.get("quote")
            )[:MAX_QUOTES]
            criteria.append(
                hooks.CriterionEvidence(
                    criterion=str(c.get("name", cid)),
                    result=criterion_result(score, flags),
                    quotes=quotes,
                )
            )
        overall = (
            f"{float(row.overall_score):.0f} / 100"
            if row.overall_score is not None
            else None
        )
        scored_at = row.updated_at.isoformat() if scoring and row.updated_at else None
        return hooks.ScreeningSummary(
            status=row.status,
            overall=overall,
            scored_at=scored_at,
            criteria=tuple(criteria),
        )


class ScreeningTextRedactor:
    async def redactor(
        self,
        session: AsyncSession,
        application_id: uuid.UUID,
        contacts: Sequence[Mapping[str, Any]],
    ) -> Redactor:
        row = await session.scalar(
            select(HireScreening)
            .where(HireScreening.application_id == application_id)
            .order_by(HireScreening.created_at.desc())
            .limit(1)
        )
        profile: ParsedProfile | None = None
        if row is not None:
            raw = read_details(row).get("profile")
            profile = ParsedProfile.from_dict(raw) if isinstance(raw, dict) else None
        universities = (await screening_settings(session)).blind.universities
        return redactor(identity_sources(*contacts, profile=profile), universities=universities)


def register() -> None:
    hooks.set_blind_mode_source(ScreeningBlindSource())
    hooks.set_screening_view(ScreeningReviewerView())
    hooks.set_text_redactor(ScreeningTextRedactor())
