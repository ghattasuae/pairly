# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The fake calendar's busy time, stored in ``hire_calendar_blocks`` (tenant session).

``pairly_core.integrations.calendar.fake`` generates each person's busy blocks per local
day from the organisation's seed; this store keeps them the first time a day is read
(``INSERT ... ON CONFLICT DO NOTHING`` on the per day unique index, so two requests
reading the same new day store it once), and keeps the events written through the
provider (booked interviews), moved in place and marked cancelled, never deleted.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.scheduling.availability import Interval
from pairly_hire.scheduling.models import HireCalendarBlock


class DbFakeCalendarStore:
    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self.session = session
        self.org_id = org_id

    async def seeded_days(self, user_key: str, days: Sequence[date]) -> set[date]:
        rows = await self.session.scalars(
            select(HireCalendarBlock.day)
            .where(
                HireCalendarBlock.user_id == uuid.UUID(user_key),
                HireCalendarBlock.kind == "seed",
                HireCalendarBlock.day.in_(list(days)),
            )
            .distinct()
        )
        return set(rows)

    async def add_seed(self, user_key: str, day: date, blocks: Sequence[Interval]) -> None:
        if not blocks:
            return
        await self.session.execute(
            insert(HireCalendarBlock)
            .values(
                [
                    {
                        "id": uuid.uuid4(),
                        "org_id": self.org_id,
                        "user_id": uuid.UUID(user_key),
                        "day": day,
                        "seq": i,
                        "kind": "seed",
                        "starts_at": b.start,
                        "ends_at": b.end,
                    }
                    for i, b in enumerate(blocks)
                ]
            )
            .on_conflict_do_nothing(
                index_elements=["org_id", "user_id", "day", "seq"],
                index_where=text("kind = 'seed'"),
            )
        )

    async def busy(self, user_key: str, window: Interval) -> list[Interval]:
        rows = await self.session.execute(
            select(HireCalendarBlock.starts_at, HireCalendarBlock.ends_at).where(
                HireCalendarBlock.user_id == uuid.UUID(user_key),
                HireCalendarBlock.cancelled_at.is_(None),
                HireCalendarBlock.starts_at < window.end,
                HireCalendarBlock.ends_at > window.start,
            )
        )
        return sorted(Interval(r.starts_at, r.ends_at) for r in rows)

    async def put_event(self, user_key: str, uid: str, interval: Interval) -> None:
        await self.session.execute(
            insert(HireCalendarBlock)
            .values(
                id=uuid.uuid4(),
                org_id=self.org_id,
                user_id=uuid.UUID(user_key),
                day=interval.start.date(),
                seq=0,
                kind="event",
                event_uid=uid,
                starts_at=interval.start,
                ends_at=interval.end,
            )
            .on_conflict_do_update(
                index_elements=["org_id", "user_id", "event_uid"],
                index_where=text("kind = 'event'"),
                set_={
                    "starts_at": interval.start,
                    "ends_at": interval.end,
                    "day": interval.start.date(),
                    "cancelled_at": None,
                },
            )
        )

    async def cancel_event(self, user_key: str, uid: str) -> None:
        row = await self.session.scalar(
            select(HireCalendarBlock).where(
                HireCalendarBlock.user_id == uuid.UUID(user_key),
                HireCalendarBlock.kind == "event",
                HireCalendarBlock.event_uid == uid,
            )
        )
        if row is not None and row.cancelled_at is None:
            row.cancelled_at = datetime.now(UTC)
            await self.session.flush()
