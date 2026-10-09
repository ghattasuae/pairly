# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Zoom meeting links (Server-to-Server OAuth): planned for M3, not available yet.

The provider will create a scheduled meeting (``POST /users/me/meetings``) with an app
of the organisation's own Zoom account. Until then ``create_meeting`` raises
``CalendarError("not_available")``.
"""

from __future__ import annotations

from typing import Final

from pairly_core.integrations.calendar.base import (
    CalendarError,
    CalendarUser,
    EventSpec,
    MeetingLink,
)

MILESTONE: Final = "M3"


class ZoomMeetingProvider:
    """Stub (M3): ``create_meeting`` raises ``CalendarError("not_available")``."""

    name = "zoom"

    async def create_meeting(self, organizer: CalendarUser, event: EventSpec) -> MeetingLink:
        raise CalendarError("not_available", transient=False)
