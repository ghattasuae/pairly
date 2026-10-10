# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Erasing a candidate's personal data, after their deletion request (brief 6.9).

Only ever a person's action: an admin with ``hire.settings.manage`` (second factor)
opens the candidate's open deletion request and confirms it with a written reason
(``POST /v1/hire/data-requests/{id}/erase``). Nothing calls this on a schedule or from
an agent (hard rule 1: people delete data, agents never do).

What it erases, in the request's transaction:

* the candidate row: the email (replaced by an address that cannot receive mail, with a
  blind index nobody can produce from a real address), the name and its blind indexes,
  the phone, location and profile link;
* every application of the candidate: the form answers and the contact details sent
  with it (both become empty);
* the candidate's rediscovery search chunks.

Ids, references, stage history and decisions stay, so the audit trail and the counts
remain whole without naming anyone. The request is completed with the reason as its
outcome (sealed), and the erasure is audited.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import audit_actor_action
from pairly_core.crypto.types import seal
from pairly_core.errors import ConflictError, NotFoundError
from pairly_core.rbac import Actor
from pairly_hire.models import HireApplication, HireCandidate
from pairly_hire.pipeline.rediscovery import remove_candidate
from pairly_hire.portal.models import DataRequestKind, DataRequestStatus, HireDataRequest

ERASED: Final = "hire.data_request.erased"
ERASED_NAME: Final = "Erased candidate"


def erased_email(candidate_id: uuid.UUID) -> str:
    return f"erased-{candidate_id.hex}@erased.invalid"


def erased_bidx(candidate_id: uuid.UUID) -> str:
    """A unique placeholder blind index: no real address hashes to it (it is not keyed)."""
    return hashlib.sha256(f"pairly.erased:{candidate_id}".encode()).hexdigest()


async def erase_for_request(
    session: AsyncSession, actor: Actor, request_id: uuid.UUID, *, reason: str
) -> tuple[HireDataRequest, HireCandidate | None, int]:
    """Erase the candidate of an open deletion request. Returns the completed request,
    the candidate row and how many applications were cleared."""
    row = await session.scalar(
        select(HireDataRequest).where(HireDataRequest.id == request_id).with_for_update()
    )
    if row is None:
        raise NotFoundError("request not found")
    if row.kind != DataRequestKind.DELETION.value:
        raise ConflictError("only a deletion request can erase data")
    if row.status != DataRequestStatus.OPEN.value:
        raise ConflictError("this request was already resolved")
    candidate = await session.scalar(
        select(HireCandidate).where(HireCandidate.id == row.candidate_id).with_for_update()
    )
    now = datetime.now(UTC)
    cleared = 0
    if candidate is not None:
        org, cid = candidate.org_id, candidate.id
        setattr(  # noqa: B010 (typed column, sealed value)
            candidate,
            "email",
            seal(HireCandidate, "email", erased_email(cid), org_id=org, row_id=cid),
        )
        setattr(  # noqa: B010
            candidate,
            "full_name",
            seal(HireCandidate, "full_name", ERASED_NAME, org_id=org, row_id=cid),
        )
        candidate.email_bidx = erased_bidx(cid)
        candidate.name_bidx = []
        candidate.phone = None
        candidate.phone_bidx = None
        candidate.location = None
        candidate.profile_url = None
        candidate.updated_at = now
        apps = await session.scalars(
            select(HireApplication).where(HireApplication.candidate_id == cid).with_for_update()
        )
        for app in apps:
            for column in ("answers", "contact"):
                sealed = seal(HireApplication, column, {}, org_id=app.org_id, row_id=app.id)
                setattr(app, column, sealed)
            cleared += 1
        await remove_candidate(session, cid)
    row.status = DataRequestStatus.COMPLETED.value
    row.handled_by = actor.id
    row.handled_at = now
    outcome = seal(HireDataRequest, "outcome", reason, org_id=row.org_id, row_id=row.id)
    setattr(row, "outcome", outcome)  # noqa: B010 (typed column, sealed value)
    await session.flush()
    if candidate is not None:
        await session.refresh(candidate)
    await session.refresh(row)
    await audit_actor_action(
        session,
        actor,
        ERASED,
        entity_type="hire.data_request",
        entity_id=row.id,
        workflow="hire",
        # The reason is sealed on the request (it may name the person); the audit keeps
        # who, when and what was cleared.
        payload={
            "candidate_id": str(row.candidate_id),
            "applications_cleared": cleared,
            "reason_given": True,
        },
    )
    return row, candidate, cleared
