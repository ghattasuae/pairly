# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Microsoft 365 calendars and Teams through Microsoft Graph, OAuth per person.

Off unless an organisation turns on its feature flag (``hire.scheduling.microsoft``) and
the deployment has an Entra ID (Azure AD) app registration
(``PAIRLY_MICROSOFT_CLIENT_ID``, ``PAIRLY_MICROSOFT_CLIENT_SECRET``,
``PAIRLY_MICROSOFT_REDIRECT_URL``, optional ``PAIRLY_MICROSOFT_TENANT``, by default
``organizations``: work and school accounts only). Without them nothing here is used and
the product shows "not connected". Tested against a mocked HTTP transport only; it never
calls Microsoft in tests.

OAuth (``MicrosoftOAuth``): the authorisation code flow on the v2.0 endpoints with
``offline_access``, so Graph returns a refresh token; the caller stores it encrypted
(``pairly_core.crypto``, like ``google``) and gets short-lived access tokens with
``refresh``. Scopes are the narrow delegated ones: ``Calendars.ReadWrite`` and
``User.Read`` (plus ``openid``, ``email``, ``offline_access``).

Calendar (``MicrosoftGraphCalendarProvider``):

* ``free_busy``: ``POST /me/calendar/getSchedule`` with the person's own address in
  ``schedules``, the window in UTC and ``Prefer: outlook.timezone="UTC"``; one request
  per person with that person's token. ``busy``, ``tentative`` and ``oof`` count as busy.
* ``create_event``: ``POST /me/events`` with the attendees, the times in UTC,
  ``transactionId`` (the caller's ``client_request_id``: Graph returns the same event
  when a create is retried), and, when a meeting is wanted and no link was chosen,
  ``isOnlineMeeting: true`` with ``onlineMeetingProvider: teamsForBusiness`` (the Teams
  join link comes back in ``onlineMeeting.joinUrl``). Graph assigns its own ``iCalUId``
  (read only); it is returned on ``EventRef.ical_uid``, and our ICS UID is kept on the
  event as a single value extended property so the event can be found by it.
* ``update_event``: ``PATCH /me/events/{id}``; ``cancel_event``:
  ``POST /me/events/{id}/cancel`` (404 and 410 count as done).

Graph has no ``sendUpdates=none``: Exchange sends its own meeting request to attendees
of the organiser's event. So the Outlook event lists only the interview panel (the
attendees with a ``user_key``, Pairly users); the candidate is never an attendee and
gets Pairly's own invitation email with the ICS file only (ADR 0011). Pairly in turn
sends the panel no ICS file for a Microsoft event, so nobody gets two invitations.

``MicrosoftTokens.refresh_token`` on a refresh is the rotated token when Microsoft sends
one; the caller stores it (``pairly_hire.scheduling.providers``).

HTTP 401 and 403 are ``microsoft_unauthorized`` (not transient: the person reconnects);
429 and 5xx and network errors are transient.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import quote, urlencode

import httpx

from pairly_core.integrations.calendar.base import (
    CalendarError,
    CalendarNotConnectedError,
    CalendarUser,
    EventRef,
    EventSpec,
)
from pairly_core.integrations.calendar.http import json_object, status_error, utc_naive
from pairly_core.scheduling.availability import Interval

API_BASE: Final = "https://graph.microsoft.com/v1.0"
LOGIN_BASE: Final = "https://login.microsoftonline.com"
SCOPES: Final = (
    "offline_access",
    "openid",
    "email",
    "https://graph.microsoft.com/Calendars.ReadWrite",
    "https://graph.microsoft.com/User.Read",
)
TIMEOUT_SECONDS: Final = 15.0
BUSY_STATUSES: Final = frozenset({"busy", "tentative", "oof"})
# Our ICS UID on the Outlook event (a named string property in Pairly's own GUID space).
UID_PROPERTY: Final = "String {8d2a6f0e-4c1b-4f57-9a3e-5b7c2d1e9f40} Name PairlyIcsUid"
_EVENT_ID: Final = re.compile(r"^[A-Za-z0-9=_+/-]{1,512}$")
_TENANT: Final = re.compile(r"^[A-Za-z0-9.-]{1,64}$")
_REQUEST_ID: Final = re.compile(r"^[0-9a-f]{32}$")


def _event_path(event_id: str) -> str:
    """The event's URL path; an id outside Graph's alphabet is refused, and the id is
    percent-encoded (Graph ids may carry ``/`` and ``+``)."""
    if not _EVENT_ID.match(event_id):
        raise CalendarError("microsoft_bad_event_id", transient=False)
    return f"/me/events/{quote(event_id, safe='')}"


@dataclass(frozen=True, slots=True)
class MicrosoftOAuthConfig:
    client_id: str
    client_secret: str
    redirect_url: str
    tenant: str = "organizations"

    def __repr__(self) -> str:
        return (
            f"MicrosoftOAuthConfig(client_id={self.client_id!r}, "
            f"redirect_url={self.redirect_url!r}, tenant={self.tenant!r})"
        )


def load_microsoft_oauth_config(
    env: Mapping[str, str] | None = None,
) -> MicrosoftOAuthConfig | None:
    """The deployment's app registration, or None when it is not configured (the default)."""
    source = os.environ if env is None else env
    client_id = source.get("PAIRLY_MICROSOFT_CLIENT_ID", "").strip()
    secret = source.get("PAIRLY_MICROSOFT_CLIENT_SECRET", "").strip()
    redirect = source.get("PAIRLY_MICROSOFT_REDIRECT_URL", "").strip()
    tenant = source.get("PAIRLY_MICROSOFT_TENANT", "").strip() or "organizations"
    if not (client_id and secret and redirect.startswith(("https://", "http://localhost"))):
        return None
    if not _TENANT.match(tenant):
        return None
    return MicrosoftOAuthConfig(client_id, secret, redirect, tenant)


@dataclass(frozen=True, slots=True)
class MicrosoftTokens:
    access_token: str
    expires_in: int
    refresh_token: str | None
    scope: str

    def __repr__(self) -> str:
        return f"MicrosoftTokens(expires_in={self.expires_in}, scope={self.scope!r})"


class MicrosoftOAuth:
    def __init__(self, config: MicrosoftOAuthConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self.client = client

    @property
    def token_url(self) -> str:
        return f"{LOGIN_BASE}/{self.config.tenant}/oauth2/v2.0/token"

    def authorization_url(self, state: str, *, login_hint: str | None = None) -> str:
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_url,
            "response_type": "code",
            "response_mode": "query",
            "scope": " ".join(SCOPES),
            "state": state,
        }
        if login_hint:
            params["login_hint"] = login_hint
        return f"{LOGIN_BASE}/{self.config.tenant}/oauth2/v2.0/authorize?{urlencode(params)}"

    async def _token(self, form: dict[str, str]) -> MicrosoftTokens:
        try:
            response = await self.client.post(self.token_url, data=form, timeout=TIMEOUT_SECONDS)
        except httpx.HTTPError:
            raise CalendarError("microsoft_oauth_unavailable", transient=True) from None
        if response.status_code == 400:
            # invalid_grant: the refresh token was revoked or expired.
            raise CalendarNotConnectedError("microsoft_grant_invalid")
        if response.status_code != 200:
            raise status_error(response, "microsoft_oauth")
        data = json_object(response, "microsoft_oauth")
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise CalendarError("microsoft_oauth_bad_response", transient=True)
        refresh = data.get("refresh_token")
        expires = data.get("expires_in")
        return MicrosoftTokens(
            access_token=token,
            expires_in=int(expires) if isinstance(expires, int | float) else 3600,
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            scope=str(data.get("scope") or ""),
        )

    async def exchange_code(self, code: str) -> MicrosoftTokens:
        return await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "redirect_uri": self.config.redirect_url,
                "scope": " ".join(SCOPES),
            }
        )

    async def refresh(self, refresh_token: str) -> MicrosoftTokens:
        return await self._token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "scope": " ".join(SCOPES),
            }
        )


def _parse_graph_time(value: Any) -> datetime:
    """A Graph ``dateTimeTimeZone`` in UTC (we ask for UTC everywhere)."""
    if not isinstance(value, dict) or value.get("timeZone", "UTC") != "UTC":
        raise CalendarError("microsoft_bad_response", transient=True)
    raw = value.get("dateTime")
    if not isinstance(raw, str):
        raise CalendarError("microsoft_bad_response", transient=True)
    # Graph writes seven fractional digits; seconds are precise enough here.
    try:
        parsed = datetime.fromisoformat(raw.split(".", 1)[0].removesuffix("Z"))
    except ValueError:
        raise CalendarError("microsoft_bad_response", transient=True) from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def event_body(event: EventSpec, *, create: bool) -> dict[str, Any]:
    """The Graph event resource for ``event``."""
    body: dict[str, Any] = {
        "subject": event.summary[:255],
        "body": {"contentType": "text", "content": event.description[:8000]},
        "start": {"dateTime": utc_naive(event.start), "timeZone": "UTC"},
        "end": {"dateTime": utc_naive(event.end), "timeZone": "UTC"},
        # The panel only (Pairly users): Exchange invites every attendee itself, and
        # the candidate gets Pairly's own invitation instead.
        "attendees": [
            {
                "emailAddress": {"address": a.email, **({"name": a.name} if a.name else {})},
                "type": "required",
            }
            for a in event.attendees
            if a.user_key is not None
        ],
        "allowNewTimeProposals": False,
        "isReminderOn": True,
        "singleValueExtendedProperties": [{"id": UID_PROPERTY, "value": event.uid}],
    }
    if event.meeting_url:
        body["location"] = {"displayName": event.meeting_url[:255]}
        body["isOnlineMeeting"] = False
    elif event.want_meeting:
        body["isOnlineMeeting"] = True
        body["onlineMeetingProvider"] = "teamsForBusiness"
    if create and event.client_request_id:
        if not _REQUEST_ID.match(event.client_request_id):
            raise CalendarError("microsoft_bad_request_id", transient=False)
        body["transactionId"] = event.client_request_id
    return body


def _teams_url(data: Mapping[str, Any]) -> str | None:
    meeting = data.get("onlineMeeting")
    if isinstance(meeting, dict):
        url = meeting.get("joinUrl")
        if isinstance(url, str) and url.startswith("https://"):
            return url
    return None


class MicrosoftGraphCalendarProvider:
    """``CalendarProvider`` for Microsoft 365 (see the module docstring)."""

    name = "microsoft"

    def __init__(self, client: httpx.AsyncClient, *, base_url: str = API_BASE) -> None:
        self.client = client
        self.base = base_url.rstrip("/")

    async def _request(
        self, method: str, path: str, user: CalendarUser, *, json: Any = None
    ) -> httpx.Response:
        if user.tokens is None:
            raise CalendarNotConnectedError()
        token = await user.tokens.access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Prefer": 'outlook.timezone="UTC"',
        }
        try:
            return await self.client.request(
                method, f"{self.base}{path}", json=json, headers=headers, timeout=TIMEOUT_SECONDS
            )
        except httpx.HTTPError:
            raise CalendarError("microsoft_unavailable", transient=True) from None

    async def free_busy(
        self, users: Sequence[CalendarUser], window: Interval
    ) -> Mapping[str, list[Interval]]:
        out: dict[str, list[Interval]] = {}
        for user in users:
            response = await self._request(
                "POST",
                "/me/calendar/getSchedule",
                user,
                json={
                    "schedules": [user.email],
                    "startTime": {"dateTime": utc_naive(window.start), "timeZone": "UTC"},
                    "endTime": {"dateTime": utc_naive(window.end), "timeZone": "UTC"},
                    "availabilityViewInterval": 15,
                },
            )
            if response.status_code != 200:
                raise status_error(response, "microsoft")
            values = json_object(response, "microsoft").get("value")
            entry = next(
                (
                    v
                    for v in values or []
                    if isinstance(v, dict)
                    and str(v.get("scheduleId", "")).lower() == user.email.lower()
                ),
                None,
            )
            if entry is None or entry.get("error"):
                raise CalendarError("microsoft_calendar_error", transient=False)
            busy: list[Interval] = []
            for item in entry.get("scheduleItems") or []:
                if not isinstance(item, dict) or item.get("status") not in BUSY_STATUSES:
                    continue
                start, end = (
                    _parse_graph_time(item.get("start")),
                    _parse_graph_time(item.get("end")),
                )
                if end > start:
                    busy.append(Interval(start, end))
            out[user.key] = sorted(busy)
        return out

    def _ref(self, event_id: str, data: Mapping[str, Any], event: EventSpec) -> EventRef:
        url = event.meeting_url or _teams_url(data)
        ical = data.get("iCalUId")
        return EventRef(
            provider=self.name,
            event_id=event_id,
            meeting_url=url,
            meeting_provider="teams" if url and not event.meeting_url else None,
            ical_uid=ical if isinstance(ical, str) and ical else None,
        )

    async def create_event(self, organizer: CalendarUser, event: EventSpec) -> EventRef:
        response = await self._request(
            "POST", "/me/events", organizer, json=event_body(event, create=True)
        )
        if response.status_code not in (200, 201):
            raise status_error(response, "microsoft")
        data = json_object(response, "microsoft")
        event_id = data.get("id")
        if not isinstance(event_id, str) or not _EVENT_ID.match(event_id):
            raise CalendarError("microsoft_bad_response", transient=True)
        return self._ref(event_id, data, event)

    async def update_event(
        self, organizer: CalendarUser, ref: EventRef, event: EventSpec
    ) -> EventRef:
        response = await self._request(
            "PATCH", _event_path(ref.event_id), organizer, json=event_body(event, create=False)
        )
        if response.status_code != 200:
            raise status_error(response, "microsoft")
        data = json_object(response, "microsoft")
        found = self._ref(ref.event_id, data, event)
        return EventRef(
            self.name,
            ref.event_id,
            found.meeting_url or ref.meeting_url,
            found.meeting_provider or ref.meeting_provider,
            found.ical_uid or ref.ical_uid,
        )

    async def cancel_event(self, organizer: CalendarUser, ref: EventRef, event: EventSpec) -> None:
        response = await self._request(
            "POST",
            f"{_event_path(ref.event_id)}/cancel",
            organizer,
            json={"comment": "This interview was cancelled."},
        )
        if response.status_code in (200, 202, 204, 404, 410):
            return
        raise status_error(response, "microsoft")
