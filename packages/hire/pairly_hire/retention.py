# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Retention of rejected and withdrawn candidates (brief 6.9). Default 12 months.

``hire.candidates.closed`` (a subjects rule of ``pairly_core.retention``): a candidate is
due when every one of their applications ended as rejected or withdrawn and the last of
those endings is older than the organisation's months
(``hire_org_settings.rejected_retention_months``, default 12; a rejection ends at
``rejected_at``, a withdrawal at the move into the withdrawn stage). A candidate with an
application still open, or one who applies again, is not due. Deleting one is the proven
candidate erasure of ``pairly_hire.erasure`` (``erase_candidate``, one transaction, then
the stored files with retries), so what stays is the same trail a deletion request
leaves. The engine lists each candidate seven days before, honours legal holds, caps a
day's deletions and records each one as a decision (``pairly_core.retention.service``).

The period is the one Hire setting: a rejected application also stores the moment it
becomes due (``retention_until``), which talent rediscovery reads. Changing the months
(here or in the Hire settings) recomputes it (``apply_retention_months``).

``hire.checklist.documents``: pre-employment checklist files (ID, degree, visa) past
their short retention, ``retention_until`` = hired at + the job's days (default 90,
``pairly_hire.onboarding``). Counted and shown only: the ``hire_checklist_retention``
job removes the files the hiring team's own setting says to remove (encrypted objects
first, then the row's pointer, audited).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, actor_ref
from pairly_core.db import current_org
from pairly_core.ingestion.storage import ObjectStore
from pairly_core.rbac import Actor
from pairly_core.retention.rules import (
    DeleteMode,
    DueSubject,
    PeriodHook,
    RetentionRule,
    SubjectRule,
)
from pairly_hire.models import ApplicationStatus, HireApplication, HireCandidate
from pairly_hire.onboarding.models import HireChecklistItem
from pairly_hire.pipeline.models import HireStageMove
from pairly_hire.pipeline.stages import WITHDRAWN
from pairly_hire.retention_dates import retention_until

RULE_KEY: Final = "hire.candidates.closed"
CHECKLIST_RULE_KEY: Final = "hire.checklist.documents"
SUBJECT_TYPE: Final = "hire.candidate"
DEFAULT_MONTHS: Final = 12
MAX_MONTHS: Final = 120
CLOSED: Final = (ApplicationStatus.REJECTED.value, ApplicationStatus.WITHDRAWN.value)


async def _months(session: AsyncSession) -> int:
    from pairly_hire.jobs import org_settings

    return (await org_settings(session)).rejected_retention_months


async def _set_months(session: AsyncSession, months: int, actor: Actor) -> None:
    from pairly_hire.jobs import save_settings

    org = current_org(session)
    if org is None:  # pragma: no cover - the retention settings run in a tenant session
        raise RuntimeError("no tenant context")
    kind, actor_id = actor_ref(actor)
    await save_settings(
        session,
        org_id=org,
        actor_type=kind,
        actor_id=actor_id,
        user_id=actor.id,
        changes={"rejected_retention_months": months},
    )
    await apply_retention_months(session, months)


async def due_candidates(session: AsyncSession, now: datetime, _days: int) -> list[DueSubject]:
    """Candidates whose applications all ended (rejected or withdrawn) longer ago than
    the organisation's months, not yet erased. Labelled with an application reference."""
    months = await _months(session)
    withdrawn_at = (
        select(func.max(HireStageMove.created_at))
        .where(
            HireStageMove.application_id == HireApplication.id,
            HireStageMove.to_stage == WITHDRAWN,
        )
        .correlate(HireApplication)
        .scalar_subquery()
    )
    ended = case(
        (HireApplication.status == ApplicationStatus.REJECTED.value, HireApplication.rejected_at),
        (
            HireApplication.status == ApplicationStatus.WITHDRAWN.value,
            func.coalesce(withdrawn_at, HireApplication.updated_at),
        ),
        else_=None,
    )
    rows = await session.execute(
        select(
            HireApplication.candidate_id,
            func.max(ended),
            func.bool_and(HireApplication.status.in_(CLOSED)),
            func.max(HireApplication.reference),
        )
        .join(HireCandidate, HireCandidate.id == HireApplication.candidate_id)
        .where(HireCandidate.erased_at.is_(None))
        .group_by(HireApplication.candidate_id)
    )
    due: list[DueSubject] = []
    for candidate_id, last_ended, all_closed, reference in rows:
        if not all_closed or last_ended is None:
            continue
        due_at = retention_until(last_ended, months)
        if due_at <= now:
            due.append(DueSubject(candidate_id, due_at, reference))
    return sorted(due, key=lambda d: (d.due_at, str(d.subject_id)))


async def erase_due_candidate(
    session: AsyncSession, candidate_id: uuid.UUID, actor_id: str
) -> Mapping[str, int]:
    """The database part of erasing one candidate (``pairly_hire.erasure``), as the
    retention job, in the caller's transaction."""
    from pairly_hire.erasure import erase_candidate

    candidate = await session.scalar(
        select(HireCandidate).where(HireCandidate.id == candidate_id).with_for_update()
    )
    if candidate is None:
        raise LookupError("candidate not found")
    counts = await erase_candidate(
        session, candidate, actor_type=ActorType.SYSTEM, actor_id=actor_id
    )
    return counts.as_payload()


async def finish_due_candidate(
    session: AsyncSession, store: ObjectStore | None, candidate_id: uuid.UUID
) -> str | None:
    """The candidate's stored files, after the database part committed."""
    from pairly_hire.erasure import delete_candidate_objects

    removed = await delete_candidate_objects(session, store, candidate_id)
    return removed.error


RETENTION_RULES: Final[tuple[RetentionRule, ...]] = (
    RetentionRule(
        RULE_KEY,
        "Rejected and withdrawn candidates, after the organisation's period (default 12 "
        "months): erased like a deletion request, the decision trail stays",
        HireCandidate,
        "created_at",
        0,
        mode=DeleteMode.SUBJECTS,
        subjects=SubjectRule(
            SUBJECT_TYPE,
            due=due_candidates,
            erase=erase_due_candidate,
            finish=finish_due_candidate,
        ),
        workflow="hire",
        configurable_days=False,
        period=PeriodHook(
            unit="months",
            default=DEFAULT_MONTHS,
            minimum=1,
            maximum=MAX_MONTHS,
            get=_months,
            set=_set_months,
        ),
    ),
    RetentionRule(
        CHECKLIST_RULE_KEY,
        "Pre-employment checklist files past their retention after hire (default 90 days)",
        HireChecklistItem,
        "retention_until",
        0,
        lambda: (HireChecklistItem.purged_at.is_(None), HireChecklistItem.object_key.is_not(None)),
        mode=DeleteMode.REPORT,
        workflow="hire",
        configurable_days=False,
    ),
)


async def apply_retention_months(session: AsyncSession, months: int) -> int:
    """Recompute ``retention_until`` of the organisation's rejected applications."""
    result = await session.execute(
        update(HireApplication)
        .where(
            HireApplication.status == ApplicationStatus.REJECTED.value,
            HireApplication.rejected_at.is_not(None),
        )
        .values(retention_until=HireApplication.rejected_at + func.make_interval(0, int(months)))
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


__all__: Sequence[str] = (
    "CHECKLIST_RULE_KEY",
    "RETENTION_RULES",
    "RULE_KEY",
    "SUBJECT_TYPE",
    "apply_retention_months",
    "due_candidates",
    "retention_until",
)
