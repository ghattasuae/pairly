# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The fake calendar: deterministic busy time for development, tests and the demo.

No external account, no network. Each person's busy blocks for a local day are derived
from ``seed``, their key and the date (SHA-256, so the same inputs always give the same
calendar), and stored through a ``FakeCalendarStore`` the first time a day is read. The
Hire workflow stores them in its database (``pairly_hire.scheduling.fake_store``); tests
use ``InMemoryFakeCalendarStore``.

The generated calendar: on working days (Monday to Friday, local time) one to three
meetings of 30 to 90 minutes between 08:00 and 18:00, on half hours; weekends are empty.
Events written through the provider (booked interviews) are stored too and show up as
busy, like on a real calendar. Meeting links look like ``https://meet.pairly.example/abc-
defg-hij`` (the reserved ``example`` name: they never reach a real service).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Final, Protocol

from pairly_core.integrations.calendar.base import (
    CalendarUser,
    EventRef,
    EventSpec,
    MeetingLink,
)
from pairly_core.scheduling.availability import Interval, zone

FAKE_MEET_HOST: Final = "https://meet.pairly.example"
MAX_DAYS: Final = 62


class FakeCalendarStore(Protocol):
    async def seeded_days(self, user_key: str, days: Sequence[date]) -> set[date]:
        """The days among ``days`` whose generated blocks are stored already."""
        ...

    async def add_seed(self, user_key: str, day: date, blocks: Sequence[Interval]) -> None:
        """Store a day's generated blocks (idempotent: a second call changes nothing)."""
        ...

    async def busy(self, user_key: str, window: Interval) -> list[Interval]:
        """Stored blocks (generated and events, not cancelled) overlapping ``window``."""
        ...

    async def put_event(self, user_key: str, uid: str, interval: Interval) -> None:
        """Add or move the event ``uid`` on this person's calendar."""
        ...

    async def cancel_event(self, user_key: str, uid: str) -> None: ...


def generated_blocks(seed: str, user_key: str, day: date, tz: str) -> list[Interval]:
    """The deterministic busy blocks of one local day (see the module docstring)."""
    if day.weekday() >= 5:
        return []
    digest = hashlib.sha256(f"pairly.fake-calendar|{seed}|{user_key}|{day}".encode()).digest()
    count = 1 + digest[0] % 3
    zone_ = zone(tz)
    blocks: list[Interval] = []
    for i in range(count):
        half_hours = 2 * 8 + digest[1 + 2 * i] % 20  # 08:00 to 17:30
        length = 30 * (1 + digest[2 + 2 * i] % 3)  # 30, 60 or 90 minutes
        start = datetime.combine(day, time(half_hours // 2, 30 * (half_hours % 2)), tzinfo=zone_)
        end = min(
            start + timedelta(minutes=length),
            datetime.combine(day, time(18, 0), tzinfo=zone_),
        )
        if end > start:
            blocks.append(Interval(start, end))
    return blocks


def fake_meeting_url(uid: str) -> str:
    code = hashlib.sha256(f"pairly.fake-meet|{uid}".encode()).hexdigest()
    letters = "".join(chr(ord("a") + int(c, 16) % 26) for c in code[:10])
    return f"{FAKE_MEET_HOST}/{letters[:3]}-{letters[3:7]}-{letters[7:10]}"


@dataclass
class InMemoryFakeCalendarStore:
    """``FakeCalendarStore`` in memory (tests)."""

    seeded: dict[str, dict[date, list[Interval]]] = field(default_factory=dict)
    events: dict[str, dict[str, Interval]] = field(default_factory=dict)

    async def seeded_days(self, user_key: str, days: Sequence[date]) -> set[date]:
        mine = self.seeded.get(user_key, {})
        return {d for d in days if d in mine}

    async def add_seed(self, user_key: str, day: date, blocks: Sequence[Interval]) -> None:
        self.seeded.setdefault(user_key, {}).setdefault(day, list(blocks))

    async def busy(self, user_key: str, window: Interval) -> list[Interval]:
        found = [b for blocks in self.seeded.get(user_key, {}).values() for b in blocks]
        found += list(self.events.get(user_key, {}).values())
        return sorted(b for b in found if b.overlaps(window))

    async def put_event(self, user_key: str, uid: str, interval: Interval) -> None:
        self.events.setdefault(user_key, {})[uid] = interval

    async def cancel_event(self, user_key: str, uid: str) -> None:
        self.events.get(user_key, {}).pop(uid, None)


class FakeCalendarProvider:
    """``CalendarProvider`` over a ``FakeCalendarStore`` (see the module docstring)."""

    name = "fake"

    def __init__(self, store: FakeCalendarStore, *, seed: str = "pairly-demo") -> None:
        self.store = store
        self.seed = seed

    async def _ensure(self, user: CalendarUser, window: Interval) -> None:
        tz = zone(user.timezone)
        first = window.start.astimezone(tz).date()
        last = window.end.astimezone(tz).date()
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)][:MAX_DAYS]
        have = await self.store.seeded_days(user.key, days)
        for day in days:
            if day not in have:
                await self.store.add_seed(
                    user.key, day, generated_blocks(self.seed, user.key, day, user.timezone)
                )

    async def free_busy(
        self, users: Sequence[CalendarUser], window: Interval
    ) -> Mapping[str, list[Interval]]:
        out: dict[str, list[Interval]] = {}
        for user in users:
            await self._ensure(user, window)
            out[user.key] = await self.store.busy(user.key, window)
        return out

    async def _write(self, organizer: CalendarUser, event: EventSpec) -> None:
        interval = Interval(event.start, event.end)
        keys = {organizer.key, *(a.user_key for a in event.attendees if a.user_key)}
        for key in sorted(keys):
            await self.store.put_event(key, event.uid, interval)

    def _ref(self, event: EventSpec) -> EventRef:
        url = event.meeting_url or (fake_meeting_url(event.uid) if event.want_meeting else None)
        return EventRef(
            provider=self.name,
            event_id=f"fake-{event.uid}",
            meeting_url=url,
            meeting_provider="fake" if url and not event.meeting_url else None,
        )

    async def create_event(self, organizer: CalendarUser, event: EventSpec) -> EventRef:
        await self._write(organizer, event)
        return self._ref(event)

    async def update_event(
        self, organizer: CalendarUser, ref: EventRef, event: EventSpec
    ) -> EventRef:
        await self._write(organizer, event)
        new = self._ref(event)
        return EventRef(ref.provider, ref.event_id, new.meeting_url, new.meeting_provider)

    async def cancel_event(self, organizer: CalendarUser, ref: EventRef, event: EventSpec) -> None:
        keys = {organizer.key, *(a.user_key for a in event.attendees if a.user_key)}
        for key in sorted(keys):
            await self.store.cancel_event(key, event.uid)


class FakeMeetingProvider:
    """Meeting links without a calendar (tests and the demo)."""

    name = "fake"

    async def create_meeting(self, organizer: CalendarUser, event: EventSpec) -> MeetingLink:
        return MeetingLink(fake_meeting_url(event.uid), self.name, f"fake-{event.uid}")

    async def update_meeting(
        self, organizer: CalendarUser, meeting_id: str, event: EventSpec
    ) -> None:
        return None

    async def delete_meeting(self, organizer: CalendarUser, meeting_id: str) -> None:
        return None
