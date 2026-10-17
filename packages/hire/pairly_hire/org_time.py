# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Calendar days in the organisation's time zone.

A date a person enters (a requisition or offer start date, a year in a job text) is a
calendar date where the organisation works, not a UTC date and never the server's own
zone. The zone is the organisation's scheduling setting (Asia/Dubai by default).

* ``local_today(now, tz)``: the calendar date at ``now`` in ``tz`` (pure).
* ``org_timezone(session, org_id)``: the organisation's zone, filtered by ``org_id``
  explicitly (safe on any session, tenant-bound or not).
* ``org_today(session, org_id, now)``: today in that zone.
* ``org_day(session, org_id, at)``: the calendar date of a past instant in that zone.

Timestamps (deadlines, TTLs, dedupe keys, report windows) stay in UTC.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_hire.scheduling.models import DEFAULT_TIMEZONE, HireSchedulingSettings


def local_today(now: datetime, timezone: str) -> date:
    """The calendar date at ``now`` (timezone aware) in ``timezone``."""
    return now.astimezone(ZoneInfo(timezone)).date()


async def org_timezone(session: AsyncSession, org_id: uuid.UUID) -> str:
    zone = await session.scalar(
        select(HireSchedulingSettings.default_timezone).where(
            HireSchedulingSettings.org_id == org_id
        )
    )
    return zone or DEFAULT_TIMEZONE


async def org_day(session: AsyncSession, org_id: uuid.UUID, at: datetime) -> date:
    """The calendar date of the instant ``at`` in the organisation's zone."""
    return local_today(at, await org_timezone(session, org_id))


async def org_today(session: AsyncSession, org_id: uuid.UUID, now: datetime | None = None) -> date:
    """Today in the organisation's zone."""
    return await org_day(session, org_id, now or datetime.now(UTC))
