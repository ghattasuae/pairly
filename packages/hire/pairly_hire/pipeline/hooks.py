# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""What the pipeline and messaging read from screening, through two small interfaces.

Screening (``pairly_hire.screening``, WP2.4) owns the blind mode setting and the scores.
The pipeline must not depend on how it stores them, so it reads them through:

* ``BlindModeSource.enabled(session)``: whether the organisation's blind mode is on (the
  setting, shown on the board). Default: off.
* ``BlindModeSource.blind_applications(session, ids)``: which of these applications are
  blind for reviewers now (screening's ``is_blind``: the latest screening was blind and
  not shortlisted, or none exists yet and blind mode is on). The pipeline hides their
  identity until a stage that reveals it (shortlist and later by default). Default: none.
* ``ScreeningView.summary(session, application_id)``: the overall result and per
  criterion evidence of an application, for the profile timeline and, for a rejection,
  for an optional short reason. Default: none (the profile shows "not screened yet",
  and a rejection has no AI reason).

Screening registers its implementations with ``set_blind_mode_source`` and
``set_screening_view`` when its module is imported (for example from the plugin
module). Both are read per call, so a test can swap them. Evidence never carries
self-identification answers: screening may not see them (``pairly_hire.scoring``).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class CriterionEvidence:
    """One rubric criterion as screening saw it."""

    criterion: str
    # ``met``, ``partly_met``, ``not_met`` or ``unknown``.
    result: str
    # Short quotes from the CV that support the result (validated by screening).
    quotes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScreeningSummary:
    # e.g. ``scored``, ``needs_review``; free text for the timeline.
    status: str
    overall: str | None = None
    scored_at: str | None = None
    criteria: tuple[CriterionEvidence, ...] = field(default=())


class BlindModeSource(Protocol):
    async def enabled(self, session: AsyncSession) -> bool: ...

    async def blind_applications(
        self, session: AsyncSession, application_ids: Sequence[uuid.UUID]
    ) -> Collection[uuid.UUID]: ...


class ScreeningView(Protocol):
    async def summary(
        self, session: AsyncSession, application_id: uuid.UUID
    ) -> ScreeningSummary | None: ...


class _BlindOff:
    async def enabled(self, session: AsyncSession) -> bool:
        return False

    async def blind_applications(
        self, session: AsyncSession, application_ids: Sequence[uuid.UUID]
    ) -> Collection[uuid.UUID]:
        return ()


class _NoScreening:
    async def summary(
        self, session: AsyncSession, application_id: uuid.UUID
    ) -> ScreeningSummary | None:
        return None


_blind: BlindModeSource = _BlindOff()
_screening: ScreeningView = _NoScreening()


def set_blind_mode_source(source: BlindModeSource | None) -> None:
    """Register the blind mode setting (None: back to the default, off)."""
    global _blind
    _blind = source if source is not None else _BlindOff()


def set_screening_view(view: ScreeningView | None) -> None:
    """Register the screening results (None: back to the default, none)."""
    global _screening
    _screening = view if view is not None else _NoScreening()


async def blind_mode(session: AsyncSession) -> bool:
    try:
        return bool(await _blind.enabled(session))
    except Exception:
        # Fail closed: if the setting cannot be read, hide identities.
        return True


async def blind_applications(
    session: AsyncSession, application_ids: Sequence[uuid.UUID]
) -> set[uuid.UUID]:
    """The applications among ``application_ids`` that are blind for reviewers now."""
    ids = list(dict.fromkeys(application_ids))
    if not ids:
        return set()
    try:
        return set(await _blind.blind_applications(session, ids)) & set(ids)
    except Exception:
        # Fail closed: if blindness cannot be read, hide every identity.
        return set(ids)


async def application_blind(session: AsyncSession, application_id: uuid.UUID) -> bool:
    return application_id in await blind_applications(session, [application_id])


async def screening_summary(
    session: AsyncSession, application_id: uuid.UUID
) -> ScreeningSummary | None:
    return await _screening.summary(session, application_id)
