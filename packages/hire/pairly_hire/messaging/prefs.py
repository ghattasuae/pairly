# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Candidate communication preferences (brief 3.5.6) and talent pool consent.

A candidate opens their preferences from a signed link in our non-transactional emails
(``pairly_hire.links``, ``hp1`` token) and may:

* turn status updates by email off or on (``updates_email``). Transactional messages
  (the outcome of an application, interview invitations and changes, a withdrawal
  confirmation) are always sent: every candidate gets a final outcome (brief 3.5.4);
* agree, or stop agreeing, to be considered for future roles (``talent_pool``). Talent
  rediscovery only ever searches candidates who agreed (``pairly_hire.pipeline.rediscovery``),
  and stopping removes them from the search index at once.

Only the candidate changes these; every change is audited. Defaults without a row:
updates on, talent pool off (consent is never assumed).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_hire.messaging.models import HireCandidatePrefs

WORKFLOW: Final = "hire"
CANDIDATE_ACTOR: Final = "pairly.hire.candidate"


@dataclass(frozen=True, slots=True)
class Prefs:
    updates_email: bool = True
    talent_pool: bool = False


DEFAULT_PREFS: Final = Prefs()


async def prefs_for(session: AsyncSession, candidate_id: uuid.UUID) -> Prefs:
    row = await session.scalar(
        select(HireCandidatePrefs).where(HireCandidatePrefs.candidate_id == candidate_id)
    )
    if row is None:
        return DEFAULT_PREFS
    return Prefs(row.updates_email, row.talent_pool)


async def prefs_for_many(
    session: AsyncSession, candidate_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Prefs]:
    rows = await session.scalars(
        select(HireCandidatePrefs).where(HireCandidatePrefs.candidate_id.in_(candidate_ids))
    )
    found = {r.candidate_id: Prefs(r.updates_email, r.talent_pool) for r in rows}
    return {c: found.get(c, DEFAULT_PREFS) for c in candidate_ids}


async def set_prefs(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    candidate_id: uuid.UUID,
    updates_email: bool | None,
    talent_pool: bool | None,
) -> Prefs:
    """The candidate's own change (through their signed link). Audited."""
    row = await session.scalar(
        select(HireCandidatePrefs)
        .where(HireCandidatePrefs.candidate_id == candidate_id)
        .with_for_update()
    )
    now = datetime.now(UTC)
    if row is None:
        row = HireCandidatePrefs(
            id=uuid.uuid4(),
            org_id=org_id,
            candidate_id=candidate_id,
            updates_email=True,
            talent_pool=False,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    changes: dict[str, bool] = {}
    if updates_email is not None and updates_email != row.updates_email:
        row.updates_email = updates_email
        changes["updates_email"] = updates_email
    if talent_pool is not None and talent_pool != row.talent_pool:
        row.talent_pool = talent_pool
        row.talent_pool_at = now if talent_pool else None
        changes["talent_pool"] = talent_pool
    row.updated_at = now
    await session.flush()
    await append_audit(
        session,
        action="hire.candidate.preferences_changed",
        actor_type=ActorType.SYSTEM,
        actor_id=CANDIDATE_ACTOR,
        entity_type="hire.candidate",
        entity_id=candidate_id,
        workflow=WORKFLOW,
        payload={"changes": changes, "via": "signed_link"},
    )
    if "talent_pool" in changes and not talent_pool:
        # Stopping consent removes the candidate from the search index at once.
        from pairly_hire.pipeline.rediscovery import remove_candidate

        await remove_candidate(session, candidate_id)
    return Prefs(row.updates_email, row.talent_pool)
