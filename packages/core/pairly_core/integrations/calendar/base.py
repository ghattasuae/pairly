# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The calendar provider interface (ADR 0011).

A provider answers free/busy for a set of people over a window and creates, updates and
cancels events with attendees, an ICS UID and, when asked, a meeting link. Providers
never send email to attendees: Pairly sends its own invitations with an ICS file, so a
provider that could email (Google) is told not to.

People are ``CalendarUser``: a stable key (the Pairly user id), their email address,
their time zone and, for providers that need it, a ``TokenSource`` that gives a fresh
access token for that person (OAuth per user). Credentials never live in this module.

Errors are ``CalendarError`` with a short ``code`` and ``transient`` (retry later) or
not. ``CalendarNotConnectedError`` means the person has no usable connection: callers fall
back (the fake provider in development, or "connect your calendar" in the product).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pairly_core.scheduling.availability import Interval


class CalendarError(Exception):
    def __init__(self, code: str, *, transient: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.transient = transient


class CalendarNotConnectedError(CalendarError):
    def __init__(self, code: str = "calendar_not_connected") -> None:
        super().__init__(code, transient=False)


class TokenSource(Protocol):
    async def access_token(self) -> str:
        """A valid access token for this person (refreshed when needed)."""
        ...


@dataclass(frozen=True, slots=True)
class CalendarUser:
    key: str
    email: str
    timezone: str = "UTC"
    tokens: TokenSource | None = None


@dataclass(frozen=True, slots=True)
class EventAttendee:
    email: str
    name: str | None = None
    # The Pairly user behind this attendee, when there is one (panelists).
    user_key: str | None = None


@dataclass(frozen=True, slots=True)
class EventSpec:
    """What a provider writes. ``uid`` is the ICS UID shared with our email invitations."""

    uid: str
    sequence: int
    summary: str
    start: datetime
    end: datetime
    attendees: Sequence[EventAttendee] = ()
    description: str = ""
    # Ask the provider for a meeting link (Google Meet through conferenceData).
    want_meeting: bool = True
    # A link chosen by a person; providers keep it as the location.
    meeting_url: str | None = None
    timezone: str = "UTC"
    # The caller's idempotency key for a create (lowercase hex, 32 characters). Providers
    # that can use it make a retried create return the same event: Google takes it as
    # the event id (known before the call), Microsoft Graph as ``transactionId``.
    client_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class EventRef:
    provider: str
    event_id: str
    meeting_url: str | None = None
    meeting_provider: str | None = None
    # The provider's own ICS UID when it does not take ours (Microsoft Graph).
    ical_uid: str | None = None


@dataclass(frozen=True, slots=True)
class MeetingLink:
    url: str
    provider: str
    # The provider's meeting id (Zoom), kept to move or delete the meeting later.
    meeting_id: str | None = None


class CalendarProvider(Protocol):
    name: str

    async def free_busy(
        self, users: Sequence[CalendarUser], window: Interval
    ) -> Mapping[str, list[Interval]]:
        """Busy intervals per ``CalendarUser.key`` inside ``window``."""
        ...

    async def create_event(self, organizer: CalendarUser, event: EventSpec) -> EventRef: ...

    async def update_event(
        self, organizer: CalendarUser, ref: EventRef, event: EventSpec
    ) -> EventRef: ...

    async def cancel_event(self, organizer: CalendarUser, ref: EventRef, event: EventSpec) -> None:
        """Cancel the event. An event that is already gone is not an error."""
        ...


class MeetingProvider(Protocol):
    """Meeting links outside a calendar event (Zoom). Google Meet and Teams links come
    with the calendar event instead."""

    name: str

    async def create_meeting(self, organizer: CalendarUser, event: EventSpec) -> MeetingLink: ...

    async def update_meeting(
        self, organizer: CalendarUser, meeting_id: str, event: EventSpec
    ) -> None: ...

    async def delete_meeting(self, organizer: CalendarUser, meeting_id: str) -> None:
        """Delete the meeting. A meeting that is already gone is not an error."""
        ...
