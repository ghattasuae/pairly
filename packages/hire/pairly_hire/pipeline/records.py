# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""One application with everything the pipeline and messaging need about it.

``load_record`` reads the application (submitted ones only), its candidate, job, pipeline
entry (if any) and the job's stages in one place, so every route and job applies the
same rules:

* an application without an entry is in the first stage and never had an update;
* identity is hidden while the application is blind (``hooks.blind_applications``,
  screening's answer, which already reads the application's one reveal state,
  ``pairly_hire.reveal``) and was never revealed (``identity_hidden``); hidden, the
  candidate reads as "Candidate <reference>". ``hidden_identities`` answers the same for
  many applications at once (lists that do not load whole records).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.crypto.types import unseal
from pairly_core.errors import NotFoundError
from pairly_core.models import Organisation
from pairly_hire.models import ApplicationStatus, HireApplication, HireCandidate, HireJob
from pairly_hire.pipeline.hooks import blind_applications
from pairly_hire.pipeline.models import HirePipelineEntry
from pairly_hire.pipeline.stages import FIRST, Stage, StageSet, stages_for

HIDDEN_LABEL: Final = "Candidate {reference}"


def stage_of(stages: StageSet, entry: HirePipelineEntry | None) -> Stage:
    key = entry.stage_key if entry is not None else FIRST
    return stages.get(key) or stages.stages[0]


def is_revealed(app: HireApplication) -> bool:
    """The application's one reveal state (``pairly_hire.reveal``): set by the shortlist
    approval or a move into a stage that reveals identity, never cleared."""
    return app.identity_revealed_at is not None


def hidden_label(app: HireApplication) -> str:
    return HIDDEN_LABEL.format(reference=app.reference)


@dataclass
class AppRecord:
    app: HireApplication
    candidate: HireCandidate
    job: HireJob
    entry: HirePipelineEntry | None
    stages: StageSet

    @property
    def stage_key(self) -> str:
        return self.entry.stage_key if self.entry is not None else FIRST

    @property
    def stage(self) -> Stage:
        return stage_of(self.stages, self.entry)

    @property
    def revealed(self) -> bool:
        return is_revealed(self.app)

    def identity_hidden(self, blind_mode: bool) -> bool:
        return blind_mode and not self.revealed

    @property
    def contact(self) -> dict[str, Any]:
        value = unseal(self.app, "contact")
        return value if isinstance(value, dict) else {}

    @property
    def full_name(self) -> str:
        return str(self.contact.get("full_name") or unseal(self.candidate, "full_name") or "")

    @property
    def email(self) -> str:
        return str(unseal(self.candidate, "email") or "")

    @property
    def locale(self) -> str:
        return self.app.consent_locale if self.app.consent_locale in {"en", "ar"} else "en"

    def label(self, blind_mode: bool) -> str:
        if self.identity_hidden(blind_mode):
            return hidden_label(self.app)
        return self.full_name

    @property
    def last_update_at(self) -> datetime | None:
        return self.entry.last_update_at if self.entry is not None else None

    def waiting_since(self) -> datetime:
        """When the candidate last heard from us (applying counts as the first time)."""
        submitted = self.app.submitted_at or self.app.created_at
        last = self.last_update_at
        return max(submitted, last) if last is not None else submitted


async def hidden_identities(
    session: AsyncSession, apps: Sequence[HireApplication]
) -> set[uuid.UUID]:
    """The ids of ``apps`` whose candidate identity reviewers must not see now: blind
    (``hooks.blind_applications``) and never revealed (``pairly_hire.reveal``)."""
    blind = await blind_applications(session, [a.id for a in apps])
    return {a.id for a in apps if a.id in blind and not is_revealed(a)}


async def load_record(
    session: AsyncSession, application_id: uuid.UUID, *, lock: bool = False
) -> AppRecord:
    query = select(HireApplication).where(
        HireApplication.id == application_id,
        HireApplication.status != ApplicationStatus.INCOMPLETE.value,
    )
    if lock:
        query = query.with_for_update()
    app = await session.scalar(query)
    if app is None:
        raise NotFoundError("application not found")
    candidate = await session.scalar(
        select(HireCandidate).where(HireCandidate.id == app.candidate_id)
    )
    job = await session.scalar(select(HireJob).where(HireJob.id == app.job_id))
    if candidate is None or job is None:  # pragma: no cover - foreign keys
        raise NotFoundError("application not found")
    entry_query = select(HirePipelineEntry).where(HirePipelineEntry.application_id == app.id)
    if lock:
        entry_query = entry_query.with_for_update()
    entry = await session.scalar(entry_query)
    return AppRecord(app, candidate, job, entry, await stages_for(session, job.id))


async def ensure_entry(session: AsyncSession, record: AppRecord) -> HirePipelineEntry:
    """The application's entry, created in the first stage if it has none (a write: the
    caller audits its own change)."""
    if record.entry is not None:
        return record.entry
    now = datetime.now(UTC)
    entry = HirePipelineEntry(
        id=uuid.uuid4(),
        org_id=record.app.org_id,
        application_id=record.app.id,
        job_id=record.app.job_id,
        stage_key=FIRST,
        stage_entered_at=record.app.submitted_at or now,
        tags=[],
        created_at=now,
        updated_at=now,
    )
    session.add(entry)
    await session.flush()
    record.entry = entry
    return entry


async def organisation_name(session: AsyncSession, org_id: uuid.UUID) -> str:
    name = await session.scalar(select(Organisation.name).where(Organisation.id == org_id))
    return str(name or "")
