# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Zoom meeting links with a Server-to-Server OAuth app of the organisation's own account.

Off unless an organisation turns on its feature flag (``hire.scheduling.zoom``) and an
administrator saved the app's account id, client id and client secret (stored encrypted
by the workflow, never here). Zoom is a meeting-link provider only: it is combined with
the organiser's calendar provider (the fake calendar, Google or Microsoft), which writes
the event with the Zoom link as its location. Tested against a mocked HTTP transport
only; it never calls Zoom in tests.

* Token (``ZoomTokens``): ``POST https://zoom.us/oauth/token`` with
  ``grant_type=account_credentials`` and ``account_id``, the client id and secret as
  HTTP Basic credentials. The access token lives an hour; it is kept in memory for the
  instance only, never stored.
* ``create_meeting``: ``POST /users/{host}/meetings``: a scheduled meeting (``type`` 2)
  at the event's start in UTC, its length in minutes, the waiting room on and joining
  before the host off. ``host`` is the configured host user, or the organiser's email
  (they need a licensed user in the same Zoom account). Returns the join URL and the
  meeting id; the host's start URL is never kept.
* ``update_meeting``: ``PATCH /meetings/{id}``; ``delete_meeting``:
  ``DELETE /meetings/{id}`` (404 counts as done).

HTTP 401 and 403 are ``zoom_unauthorized`` (not transient: an administrator fixes the
app); 429 and 5xx and network errors are transient.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import quote

import httpx

from pairly_core.integrations.calendar.base import (
    CalendarError,
    CalendarUser,
    EventSpec,
    MeetingLink,
)
from pairly_core.integrations.calendar.http import json_object, status_error, utc_z

API_BASE: Final = "https://api.zoom.us/v2"
TOKEN_URL: Final = "https://zoom.us/oauth/token"  # noqa: S105 (a URL, not a secret)
TIMEOUT_SECONDS: Final = 15.0
_MEETING_ID: Final = re.compile(r"^[0-9]{6,20}$")
_HOST: Final = re.compile(r"^[A-Za-z0-9._+@-]{1,254}$")


def _meeting_path(meeting_id: str) -> str:
    if not _MEETING_ID.match(meeting_id):
        raise CalendarError("zoom_bad_meeting_id", transient=False)
    return f"/meetings/{meeting_id}"


@dataclass(frozen=True, slots=True)
class ZoomCredentials:
    account_id: str
    client_id: str
    client_secret: str
    # The Zoom user that hosts the meetings; empty: the organiser's email.
    host: str = ""

    def __repr__(self) -> str:
        return f"ZoomCredentials(account_id={self.account_id!r}, client_id={self.client_id!r})"


class ZoomTokens:
    """Access tokens of one Server-to-Server OAuth app, cached for this instance."""

    def __init__(self, credentials: ZoomCredentials, client: httpx.AsyncClient) -> None:
        self.credentials = credentials
        self.client = client
        self._token: str | None = None
        self._until: datetime | None = None

    async def access_token(self) -> str:
        now = datetime.now(UTC)
        if self._token is not None and self._until is not None and now < self._until:
            return self._token
        try:
            response = await self.client.post(
                TOKEN_URL,
                params={
                    "grant_type": "account_credentials",
                    "account_id": self.credentials.account_id,
                },
                auth=(self.credentials.client_id, self.credentials.client_secret),
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            raise CalendarError("zoom_oauth_unavailable", transient=True) from None
        if response.status_code in (400, 401):
            # invalid_client or a wrong account id: an administrator fixes the app.
            raise CalendarError("zoom_unauthorized", transient=False)
        if response.status_code != 200:
            raise status_error(response, "zoom_oauth")
        data = json_object(response, "zoom_oauth")
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise CalendarError("zoom_oauth_bad_response", transient=True)
        expires = data.get("expires_in")
        seconds = int(expires) if isinstance(expires, int | float) else 3600
        self._token = token
        self._until = now + timedelta(seconds=max(seconds - 60, 30))
        return token


def meeting_body(event: EventSpec) -> dict[str, Any]:
    minutes = max(1, int((event.end - event.start).total_seconds() // 60))
    return {
        "topic": event.summary[:200],
        "type": 2,
        "start_time": utc_z(event.start),
        "duration": minutes,
        "timezone": "UTC",
        "agenda": event.description[:2000],
        "settings": {
            "join_before_host": False,
            "waiting_room": True,
            "mute_upon_entry": True,
            "auto_recording": "none",
            "email_notification": False,
        },
    }


class ZoomMeetingProvider:
    """``MeetingProvider`` for Zoom (see the module docstring)."""

    name = "zoom"

    def __init__(
        self,
        client: httpx.AsyncClient,
        credentials: ZoomCredentials,
        *,
        base_url: str = API_BASE,
    ) -> None:
        self.client = client
        self.credentials = credentials
        self.tokens = ZoomTokens(credentials, client)
        self.base = base_url.rstrip("/")

    async def _request(self, method: str, path: str, *, json: Any = None) -> httpx.Response:
        token = await self.tokens.access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            return await self.client.request(
                method, f"{self.base}{path}", json=json, headers=headers, timeout=TIMEOUT_SECONDS
            )
        except httpx.HTTPError:
            raise CalendarError("zoom_unavailable", transient=True) from None

    def _host(self, organizer: CalendarUser) -> str:
        host = self.credentials.host or organizer.email
        if not _HOST.match(host):
            raise CalendarError("zoom_bad_host", transient=False)
        return quote(host, safe="@")

    async def create_meeting(self, organizer: CalendarUser, event: EventSpec) -> MeetingLink:
        response = await self._request(
            "POST", f"/users/{self._host(organizer)}/meetings", json=meeting_body(event)
        )
        if response.status_code not in (200, 201):
            raise status_error(response, "zoom")
        data = json_object(response, "zoom")
        meeting_id, url = data.get("id"), data.get("join_url")
        if not isinstance(meeting_id, int | str) or not _MEETING_ID.match(str(meeting_id)):
            raise CalendarError("zoom_bad_response", transient=True)
        if not isinstance(url, str) or not url.startswith("https://"):
            raise CalendarError("zoom_bad_response", transient=True)
        return MeetingLink(url, self.name, str(meeting_id))

    async def update_meeting(
        self, organizer: CalendarUser, meeting_id: str, event: EventSpec
    ) -> None:
        response = await self._request("PATCH", _meeting_path(meeting_id), json=meeting_body(event))
        if response.status_code not in (200, 204):
            raise status_error(response, "zoom")

    async def delete_meeting(self, organizer: CalendarUser, meeting_id: str) -> None:
        response = await self._request("DELETE", _meeting_path(meeting_id))
        if response.status_code in (200, 204, 404):
            return
        raise status_error(response, "zoom")
