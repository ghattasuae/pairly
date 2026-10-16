# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Blind mode: the one reveal state of an application (brief 3.3.3).

Reviewers may see who a candidate is once one of two things happened, whichever came
first:

* a person approved the screening's shortlist (``screening.actions.shortlist``);
* a person moved the application into a stage that reveals identity (shortlist and
  later by default, ``pipeline.moves``).

Both call ``reveal_identity``, which sets ``hire_applications.identity_revealed_at`` and
``identity_revealed_by`` once and audits it (``hire.candidate.identity_revealed``). The
flag is never cleared: an undo or a later move back does not hide again what a person
has already seen. Every reader (the screening explainability panel, the pipeline board
and profile, messaging previews, the approval view of a message, the application list
and detail) decides blindness from this flag and the blind mode the application was
screened under (``screening.service.blind_application_ids``).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_hire.models import HireApplication

WORKFLOW: Final = "hire"
ENTITY: Final = "hire.application"
REVEALED: Final = "hire.candidate.identity_revealed"
SHORTLIST_SOURCE: Final = "shortlist"
_SOURCE_RE: Final = re.compile(r"^(shortlist|stage:[a-z][a-z0-9_]{0,31})$")


def stage_source(stage_key: str) -> str:
    return f"stage:{stage_key}"


async def reveal_identity(
    session: AsyncSession,
    app: HireApplication,
    *,
    source: str,
    actor_type: ActorType | str,
    actor_id: str,
    payload: dict[str, str] | None = None,
) -> bool:
    """Reveal ``app``'s identity to reviewers. True when this call revealed it, False
    when it already was. The caller holds the application row (ideally locked)."""
    if not _SOURCE_RE.fullmatch(source):
        raise ValueError(f"invalid reveal source {source!r}")
    if app.identity_revealed_at is not None:
        return False
    now = datetime.now(UTC)
    app.identity_revealed_at = now
    app.identity_revealed_by = source
    await session.flush()
    await append_audit(
        session,
        action=REVEALED,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type=ENTITY,
        entity_id=app.id,
        workflow=WORKFLOW,
        payload={"source": source, **(payload or {})},
    )
    return True


async def reveal_application(
    session: AsyncSession,
    application_id: uuid.UUID,
    *,
    source: str,
    actor_type: ActorType | str,
    actor_id: str,
    payload: dict[str, str] | None = None,
) -> bool:
    """``reveal_identity`` for an application by id (locks its row)."""
    app = await session.scalar(
        select(HireApplication).where(HireApplication.id == application_id).with_for_update()
    )
    if app is None:
        return False
    return await reveal_identity(
        session, app, source=source, actor_type=actor_type, actor_id=actor_id, payload=payload
    )


async def revealed_ids(
    session: AsyncSession, application_ids: Sequence[uuid.UUID]
) -> set[uuid.UUID]:
    """The ids among ``application_ids`` whose identity was revealed."""
    ids = list(dict.fromkeys(application_ids))
    if not ids:
        return set()
    rows = await session.scalars(
        select(HireApplication.id).where(
            HireApplication.id.in_(ids), HireApplication.identity_revealed_at.is_not(None)
        )
    )
    return set(rows)
