# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The candidate's upload of a checklist document through the portal's file request.

The portal session reaches only the candidate's own checklist items (restrictive RLS by
portal subject, ``hire_0015``), and only the one their own open request points at
(``portal.service.open_request`` checked the request).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType
from pairly_core.errors import NotFoundError
from pairly_core.ingestion.storage import ObjectStore
from pairly_hire.onboarding.models import HireChecklistItem
from pairly_hire.onboarding.service import store_file
from pairly_hire.portal.auth import PortalIdentity
from pairly_hire.portal.models import DocumentRequestStatus, HireDocumentRequest


async def attach_checklist_file(
    session: AsyncSession,
    store: ObjectStore,
    identity: PortalIdentity,
    request: HireDocumentRequest,
    *,
    data: bytes,
    filename: str | None,
) -> HireDocumentRequest:
    if request.checklist_item_id is None or request.candidate_id != identity.candidate_id:
        raise NotFoundError("request not found")
    item = await session.scalar(
        select(HireChecklistItem)
        .where(
            HireChecklistItem.id == request.checklist_item_id,
            HireChecklistItem.candidate_id == identity.candidate_id,
        )
        .with_for_update()
    )
    if item is None:
        raise NotFoundError("request not found")
    await store_file(
        session,
        store,
        item,
        data=data,
        filename=filename,
        actor_type=ActorType.CANDIDATE,
        actor_id=identity.actor_id,
    )
    request.status = DocumentRequestStatus.RECEIVED.value
    request.fulfilled_at = datetime.now(UTC)
    await session.flush()
    return request
