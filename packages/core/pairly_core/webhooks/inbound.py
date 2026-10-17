# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Inbound webhook deduplication (brief 6.6): each provider event is processed once.

``inbound_events`` has a unique (organisation, provider, event id). The handler pattern:

1. Verify the provider's signature first (never record an unverified event).
2. ``record_inbound_event`` in the same transaction as the processing. It inserts the
   row (``ON CONFLICT DO NOTHING``): True means first delivery, process it; False means
   already processed, answer 2xx and do nothing.
3. Process, then commit. Only then answer 2xx. Any failure rolls back the row with the
   work and answers non-2xx, so the provider retries and the retry is processed.

A concurrent duplicate waits on the first one's insert: if that commits, the duplicate
sees a conflict (False); if it rolls back, the duplicate processes the event.

The key includes the organisation (RLS: one tenant's events never collide with or
reveal another's). ``process_inbound`` wraps the pattern for callers outside a request
(a worker consuming a queue).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.db import TenantError, current_org, set_tenant
from pairly_core.errors import BadRequestError
from pairly_core.webhooks.models import InboundEvent

PROVIDER_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


async def record_inbound_event(session: AsyncSession, *, provider: str, event_id: str) -> bool:
    """True when this is the first time the event is seen (process it), else False."""
    org = current_org(session)
    if org is None:
        raise TenantError("inbound events need a tenant context")
    if not PROVIDER_RE.fullmatch(provider) or not 1 <= len(event_id) <= 255:
        raise BadRequestError("invalid provider or event id")
    inserted = await session.scalar(
        insert(InboundEvent)
        .values(org_id=org, provider=provider, event_id=event_id)
        .on_conflict_do_nothing(constraint="uq_inbound_events_provider_event")
        .returning(InboundEvent.id)
    )
    return inserted is not None


@dataclass(frozen=True, slots=True)
class InboundResult:
    duplicate: bool


async def process_inbound(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    org_id: uuid.UUID,
    provider: str,
    event_id: str,
    handler: Callable[[AsyncSession], Awaitable[None]],
) -> InboundResult:
    """Record and process one event in one transaction. Raises if processing fails."""
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            if not await record_inbound_event(session, provider=provider, event_id=event_id):
                return InboundResult(duplicate=True)
            await handler(session)
    return InboundResult(duplicate=False)
