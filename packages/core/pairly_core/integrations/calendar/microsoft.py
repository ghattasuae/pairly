# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Microsoft 365 calendars through Microsoft Graph: planned for M3, not available yet.

The provider will use ``POST /me/calendar/getSchedule`` for free/busy and
``/me/events`` with ``isOnlineMeeting`` and ``onlineMeetingProvider=teamsForBusiness``
for events with a Teams link, with OAuth per person like ``google``. Until then every
call raises ``CalendarError("not_available")`` and the product shows it as planned.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, NoReturn

from pairly_core.integrations.calendar.base import (
    CalendarError,
    CalendarUser,
    EventRef,
    EventSpec,
)
from pairly_core.scheduling.availability import Interval

MILESTONE: Final = "M3"


def _unavailable() -> NoReturn:
    raise CalendarError("not_available", transient=False)


class MicrosoftGraphCalendarProvider:
    """Stub (M3): every method raises ``CalendarError("not_available")``."""

    name = "microsoft"

    async def free_busy(
        self, users: Sequence[CalendarUser], window: Interval
    ) -> Mapping[str, list[Interval]]:
        _unavailable()

    async def create_event(self, organizer: CalendarUser, event: EventSpec) -> EventRef:
        _unavailable()

    async def update_event(
        self, organizer: CalendarUser, ref: EventRef, event: EventSpec
    ) -> EventRef:
        _unavailable()

    async def cancel_event(self, organizer: CalendarUser, ref: EventRef, event: EventSpec) -> None:
        _unavailable()
