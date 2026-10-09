# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Google Calendar and Google Meet, with OAuth per person (ADR 0011).

Off unless an organisation turns on its feature flag (``hire.scheduling.google``) and
the deployment has OAuth client credentials (``PAIRLY_GOOGLE_CLIENT_ID``,
``PAIRLY_GOOGLE_CLIENT_SECRET``, ``PAIRLY_GOOGLE_REDIRECT_URL``). Without them nothing
here is used and the product shows "not connected". The code is tested against a mocked
HTTP transport only; it never calls Google in tests.

OAuth (``GoogleOAuth``): the authorisation code flow with ``access_type=offline`` and
``prompt=consent``, so Google returns a refresh token; the caller stores it encrypted
(``pairly_core.crypto``) and gets short-lived access tokens with ``refresh``. Scopes are
the narrow ones: free/busy and events.

Calendar (``GoogleCalendarProvider``):

* ``free_busy``: ``POST /freeBusy`` with ``timeMin``, ``timeMax`` (UTC) and the person's
  own address in ``items``, one request per person with that person's token.
* ``create_event``: ``POST /calendars/primary/events?conferenceDataVersion=1
  &sendUpdates=none`` with the attendees, the ICS UID (``iCalUID``), the sequence and,
  when a meeting is wanted, ``conferenceData.createRequest`` for Google Meet
  (``hangoutsMeet``) with a request id derived from the UID (a retry never makes a
  second meeting).
* ``update_event``: ``PATCH`` of the same fields; ``cancel_event``: ``DELETE``
  (404 and 410 count as done).

``sendUpdates=none`` everywhere: Pairly emails its own invitations with an ICS file, so
attendees never get a second invitation from Google.

HTTP 401 and 403 are ``google_unauthorized`` (not transient: the person reconnects);
429 and 5xx and network errors are transient.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlencode

import httpx

from pairly_core.integrations.calendar.base import (
    CalendarError,
    CalendarNotConnectedError,
    CalendarUser,
    EventRef,
    EventSpec,
)
from pairly_core.scheduling.availability import Interval

API_BASE: Final = "https://www.googleapis.com/calendar/v3"
AUTH_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL: Final = "https://oauth2.googleapis.com/token"  # noqa: S105 (a URL, not a secret)
SCOPES: Final = (
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/calendar.events",
    "openid",
    "email",
)
TIMEOUT_SECONDS: Final = 15.0
_EVENT_ID: Final = re.compile(r"^[A-Za-z0-9_-]{1,1024}$")


def _event_path(event_id: str) -> str:
    """The event's URL path; an id that is not Google's alphabet is refused (no path
    tricks through a stored value)."""
    if not _EVENT_ID.match(event_id):
        raise CalendarError("google_bad_event_id", transient=False)
    return f"/calendars/primary/events/{event_id}"


@dataclass(frozen=True, slots=True)
class GoogleOAuthConfig:
    client_id: str
    client_secret: str
    redirect_url: str

    def __repr__(self) -> str:
        return (
            f"GoogleOAuthConfig(client_id={self.client_id!r}, redirect_url={self.redirect_url!r})"
        )


def load_google_oauth_config(env: Mapping[str, str] | None = None) -> GoogleOAuthConfig | None:
    """The deployment's OAuth client, or None when it is not configured (the default)."""
    source = os.environ if env is None else env
    client_id = source.get("PAIRLY_GOOGLE_CLIENT_ID", "").strip()
    secret = source.get("PAIRLY_GOOGLE_CLIENT_SECRET", "").strip()
    redirect = source.get("PAIRLY_GOOGLE_REDIRECT_URL", "").strip()
    if not (client_id and secret and redirect.startswith(("https://", "http://localhost"))):
        return None
    return GoogleOAuthConfig(client_id, secret, redirect)


@dataclass(frozen=True, slots=True)
class GoogleTokens:
    access_token: str
    expires_in: int
    refresh_token: str | None
    scope: str

    def __repr__(self) -> str:
        return f"GoogleTokens(expires_in={self.expires_in}, scope={self.scope!r})"


def _status_error(response: httpx.Response, prefix: str) -> CalendarError:
    code = response.status_code
    if code in (401, 403):
        return CalendarError(f"{prefix}_unauthorized", transient=False)
    if code == 429 or code >= 500:
        return CalendarError(f"{prefix}_{code}", transient=True)
    return CalendarError(f"{prefix}_{code}", transient=False)


def _json(response: httpx.Response, prefix: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        raise CalendarError(f"{prefix}_bad_response", transient=True) from None
    if not isinstance(data, dict):
        raise CalendarError(f"{prefix}_bad_response", transient=True)
    return data


class GoogleOAuth:
    def __init__(self, config: GoogleOAuthConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self.client = client

    def authorization_url(self, state: str, *, login_hint: str | None = None) -> str:
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_url,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        if login_hint:
            params["login_hint"] = login_hint
        return f"{AUTH_URL}?{urlencode(params)}"

    async def _token(self, form: dict[str, str]) -> GoogleTokens:
        try:
            response = await self.client.post(TOKEN_URL, data=form, timeout=TIMEOUT_SECONDS)
        except httpx.HTTPError:
            raise CalendarError("google_oauth_unavailable", transient=True) from None
        if response.status_code == 400:
            # invalid_grant: the refresh token was revoked or expired.
            raise CalendarNotConnectedError("google_grant_invalid")
        if response.status_code != 200:
            raise _status_error(response, "google_oauth")
        data = _json(response, "google_oauth")
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise CalendarError("google_oauth_bad_response", transient=True)
        refresh = data.get("refresh_token")
        expires = data.get("expires_in")
        return GoogleTokens(
            access_token=token,
            expires_in=int(expires) if isinstance(expires, int | float) else 3600,
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            scope=str(data.get("scope") or ""),
        )

    async def exchange_code(self, code: str) -> GoogleTokens:
        return await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "redirect_uri": self.config.redirect_url,
            }
        )

    async def refresh(self, refresh_token: str) -> GoogleTokens:
        return await self._token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
            }
        )


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CalendarError("google_bad_response", transient=True)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CalendarError("google_bad_response", transient=True) from None
    if parsed.tzinfo is None:
        raise CalendarError("google_bad_response", transient=True)
    return parsed


def conference_request_id(uid: str) -> str:
    return hashlib.sha256(f"pairly.meet|{uid}".encode()).hexdigest()[:32]


def event_body(event: EventSpec, *, with_conference: bool) -> dict[str, Any]:
    """The Calendar API event resource for ``event``."""
    body: dict[str, Any] = {
        "summary": event.summary[:300],
        "description": event.description[:8000],
        "start": {"dateTime": _rfc3339(event.start), "timeZone": "UTC"},
        "end": {"dateTime": _rfc3339(event.end), "timeZone": "UTC"},
        "attendees": [
            {"email": a.email, **({"displayName": a.name} if a.name else {})}
            for a in event.attendees
        ],
        "iCalUID": event.uid,
        "sequence": event.sequence,
        "guestsCanModify": False,
        "reminders": {"useDefault": True},
    }
    if event.meeting_url:
        body["location"] = event.meeting_url
    if with_conference and event.want_meeting and not event.meeting_url:
        body["conferenceData"] = {
            "createRequest": {
                "requestId": conference_request_id(event.uid),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
    return body


def _meeting_url(data: Mapping[str, Any]) -> str | None:
    link = data.get("hangoutLink")
    if isinstance(link, str) and link.startswith("https://"):
        return link
    conference = data.get("conferenceData")
    if isinstance(conference, dict):
        for entry in conference.get("entryPoints") or []:
            if (
                isinstance(entry, dict)
                and entry.get("entryPointType") == "video"
                and isinstance(entry.get("uri"), str)
                and entry["uri"].startswith("https://")
            ):
                return str(entry["uri"])
    return None


class GoogleCalendarProvider:
    """``CalendarProvider`` for Google Calendar (see the module docstring)."""

    name = "google"

    def __init__(self, client: httpx.AsyncClient, *, base_url: str = API_BASE) -> None:
        self.client = client
        self.base = base_url.rstrip("/")

    async def _headers(self, user: CalendarUser) -> dict[str, str]:
        if user.tokens is None:
            raise CalendarNotConnectedError()
        token = await user.tokens.access_token()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        user: CalendarUser,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        headers = await self._headers(user)
        try:
            return await self.client.request(
                method,
                f"{self.base}{path}",
                params=params,
                json=json,
                headers=headers,
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            raise CalendarError("google_unavailable", transient=True) from None

    async def free_busy(
        self, users: Sequence[CalendarUser], window: Interval
    ) -> Mapping[str, list[Interval]]:
        out: dict[str, list[Interval]] = {}
        for user in users:
            response = await self._request(
                "POST",
                "/freeBusy",
                user,
                json={
                    "timeMin": _rfc3339(window.start),
                    "timeMax": _rfc3339(window.end),
                    "timeZone": "UTC",
                    "items": [{"id": user.email}],
                },
            )
            if response.status_code != 200:
                raise _status_error(response, "google")
            data = _json(response, "google")
            calendars = data.get("calendars")
            entry = calendars.get(user.email) if isinstance(calendars, dict) else None
            if not isinstance(entry, dict) or entry.get("errors"):
                raise CalendarError("google_calendar_error", transient=False)
            busy: list[Interval] = []
            for block in entry.get("busy") or []:
                if not isinstance(block, dict):
                    continue
                start, end = _parse_time(block.get("start")), _parse_time(block.get("end"))
                if end > start:
                    busy.append(Interval(start, end))
            out[user.key] = sorted(busy)
        return out

    async def create_event(self, organizer: CalendarUser, event: EventSpec) -> EventRef:
        response = await self._request(
            "POST",
            "/calendars/primary/events",
            organizer,
            params={"conferenceDataVersion": "1", "sendUpdates": "none"},
            json=event_body(event, with_conference=True),
        )
        if response.status_code not in (200, 201):
            raise _status_error(response, "google")
        data = _json(response, "google")
        event_id = data.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise CalendarError("google_bad_response", transient=True)
        url = event.meeting_url or _meeting_url(data)
        return EventRef(
            provider=self.name,
            event_id=event_id,
            meeting_url=url,
            meeting_provider="google_meet" if url and not event.meeting_url else None,
        )

    async def update_event(
        self, organizer: CalendarUser, ref: EventRef, event: EventSpec
    ) -> EventRef:
        response = await self._request(
            "PATCH",
            _event_path(ref.event_id),
            organizer,
            params={"conferenceDataVersion": "1", "sendUpdates": "none"},
            json=event_body(event, with_conference=ref.meeting_url is None),
        )
        if response.status_code != 200:
            raise _status_error(response, "google")
        data = _json(response, "google")
        url = event.meeting_url or _meeting_url(data) or ref.meeting_url
        return EventRef(self.name, ref.event_id, url, ref.meeting_provider)

    async def cancel_event(self, organizer: CalendarUser, ref: EventRef, event: EventSpec) -> None:
        response = await self._request(
            "DELETE",
            _event_path(ref.event_id),
            organizer,
            params={"sendUpdates": "none"},
        )
        if response.status_code in (200, 204, 404, 410):
            return
        raise _status_error(response, "google")
