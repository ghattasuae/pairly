# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Microsoft Graph and Zoom providers against a mocked HTTP transport (never the real
services): request shapes, Teams and Zoom links, idempotency keys, errors and OAuth."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from pairly_core.integrations.calendar.base import (
    CalendarError,
    CalendarNotConnectedError,
    CalendarUser,
    EventAttendee,
    EventRef,
    EventSpec,
)
from pairly_core.integrations.calendar.microsoft import (
    UID_PROPERTY,
    MicrosoftGraphCalendarProvider,
    MicrosoftOAuth,
    MicrosoftOAuthConfig,
    load_microsoft_oauth_config,
)
from pairly_core.integrations.calendar.zoom import (
    TOKEN_URL as ZOOM_TOKEN_URL,
)
from pairly_core.integrations.calendar.zoom import (
    ZoomCredentials,
    ZoomMeetingProvider,
)
from pairly_core.scheduling.availability import Interval

START = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)
END = START + timedelta(minutes=45)
TEAMS = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc"
GRAPH_ID = "AAMkAGI2TG93AAA="


class _Tokens:
    def __init__(self, token: str) -> None:
        self.token = token

    async def access_token(self) -> str:
        return self.token


def _user(key: str = "u1", email: str = "omar@acme.example") -> CalendarUser:
    return CalendarUser(key, email, "Asia/Dubai", tokens=_Tokens(f"token-{key}"))


def _spec(**kw: Any) -> EventSpec:
    values: dict[str, Any] = {
        "uid": "iv-0123456789abcdef@pairly.localhost",
        "sequence": 0,
        "summary": "Interview: Senior Backend Engineer",
        "start": START,
        "end": END,
        "attendees": (
            EventAttendee("layla@example.test", "Layla Haddad"),
            EventAttendee("sara@acme.example", "Sara", user_key="u2"),
        ),
        "description": "Interview with Acme Logistics",
        "client_request_id": "b" * 32,
    }
    return EventSpec(**(values | kw))


# ------------------------------------------------------------------ Microsoft Graph


class _Graph:
    """A mocked Graph and identity platform: records requests, answers like the APIs."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host in {"graph.microsoft.com", "login.microsoftonline.com"}
        path = request.url.path
        if path in self.status:
            return httpx.Response(self.status[path], json={"error": {"code": "x"}})
        if path.endswith("/oauth2/v2.0/token"):
            form = parse_qs(request.content.decode())
            body: dict[str, Any] = {"access_token": "eyJ.fresh", "expires_in": 3599}
            if form["grant_type"] == ["authorization_code"]:
                body["refresh_token"] = "M.R3_refresh"  # noqa: S105 (a fake token)
            return httpx.Response(200, json=body | {"scope": "Calendars.ReadWrite"})
        if path.endswith("/me/calendar/getSchedule"):
            sent = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "scheduleId": sent["schedules"][0],
                            "scheduleItems": [
                                {
                                    "status": "busy",
                                    "start": {
                                        "dateTime": "2026-10-13T05:00:00.0000000",
                                        "timeZone": "UTC",
                                    },
                                    "end": {
                                        "dateTime": "2026-10-13T06:00:00.0000000",
                                        "timeZone": "UTC",
                                    },
                                },
                                {
                                    "status": "free",
                                    "start": {
                                        "dateTime": "2026-10-13T08:00:00.0000000",
                                        "timeZone": "UTC",
                                    },
                                    "end": {
                                        "dateTime": "2026-10-13T09:00:00.0000000",
                                        "timeZone": "UTC",
                                    },
                                },
                                {
                                    "status": "oof",
                                    "start": {
                                        "dateTime": "2026-10-13T10:00:00.0000000",
                                        "timeZone": "UTC",
                                    },
                                    "end": {
                                        "dateTime": "2026-10-13T11:00:00.0000000",
                                        "timeZone": "UTC",
                                    },
                                },
                            ],
                        }
                    ]
                },
            )
        if path == "/v1.0/me/events" and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": GRAPH_ID,
                    "iCalUId": "040000008200E00074C5B7101A82E008",
                    "onlineMeeting": {"joinUrl": TEAMS},
                },
            )
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": GRAPH_ID, "onlineMeeting": None})
        if path.endswith("/cancel"):
            return httpx.Response(202)
        return httpx.Response(404)


async def test_microsoft_get_schedule_request_shape() -> None:
    graph = _Graph()
    async with httpx.AsyncClient(transport=httpx.MockTransport(graph)) as client:
        provider = MicrosoftGraphCalendarProvider(client)
        busy = await provider.free_busy(
            [_user(), _user("u2", "sara@acme.example")],
            Interval(datetime(2026, 10, 13, tzinfo=UTC), datetime(2026, 10, 14, tzinfo=UTC)),
        )
    assert len(graph.requests) == 2  # one request per person, with their own token
    first = graph.requests[0]
    assert str(first.url) == "https://graph.microsoft.com/v1.0/me/calendar/getSchedule"
    assert first.method == "POST"
    assert first.headers["Authorization"] == "Bearer token-u1"
    assert first.headers["Prefer"] == 'outlook.timezone="UTC"'
    assert json.loads(first.content) == {
        "schedules": ["omar@acme.example"],
        "startTime": {"dateTime": "2026-10-13T00:00:00", "timeZone": "UTC"},
        "endTime": {"dateTime": "2026-10-14T00:00:00", "timeZone": "UTC"},
        "availabilityViewInterval": 15,
    }
    assert graph.requests[1].headers["Authorization"] == "Bearer token-u2"
    # busy and out of office count; free does not.
    assert busy["u1"] == [
        Interval(datetime(2026, 10, 13, 5, tzinfo=UTC), datetime(2026, 10, 13, 6, tzinfo=UTC)),
        Interval(datetime(2026, 10, 13, 10, tzinfo=UTC), datetime(2026, 10, 13, 11, tzinfo=UTC)),
    ]


async def test_microsoft_events_with_teams_link_and_transaction_id() -> None:
    graph = _Graph()
    spec = _spec()
    async with httpx.AsyncClient(transport=httpx.MockTransport(graph)) as client:
        provider = MicrosoftGraphCalendarProvider(client)
        ref = await provider.create_event(_user(), spec)
        moved = _spec(sequence=1, start=START + timedelta(days=1), end=END + timedelta(days=1))
        updated = await provider.update_event(_user(), ref, moved)
        await provider.cancel_event(_user(), updated, moved)
    create, patch, cancel = graph.requests
    assert create.method == "POST"
    assert str(create.url) == "https://graph.microsoft.com/v1.0/me/events"
    body = json.loads(create.content)
    assert body["subject"] == spec.summary
    assert body["start"] == {"dateTime": "2026-10-13T06:00:00", "timeZone": "UTC"}
    assert body["end"] == {"dateTime": "2026-10-13T06:45:00", "timeZone": "UTC"}
    assert body["attendees"] == [
        {
            "emailAddress": {"address": "layla@example.test", "name": "Layla Haddad"},
            "type": "required",
        },
        {"emailAddress": {"address": "sara@acme.example", "name": "Sara"}, "type": "required"},
    ]
    assert body["isOnlineMeeting"] is True
    assert body["onlineMeetingProvider"] == "teamsForBusiness"
    assert body["transactionId"] == "b" * 32
    assert body["singleValueExtendedProperties"] == [{"id": UID_PROPERTY, "value": spec.uid}]
    assert ref == EventRef(
        "microsoft", GRAPH_ID, TEAMS, "teams", "040000008200E00074C5B7101A82E008"
    )
    assert patch.method == "PATCH"
    # Graph ids may carry "=", "/" or "+": always percent-encoded in the path.
    assert patch.url.raw_path.decode().endswith("/me/events/AAMkAGI2TG93AAA%3D")
    patched = json.loads(patch.content)
    assert "transactionId" not in patched
    assert patched["start"]["dateTime"] == "2026-10-14T06:00:00"
    assert updated.meeting_url == TEAMS  # kept from the create
    assert cancel.method == "POST"
    assert cancel.url.raw_path.decode().endswith("/me/events/AAMkAGI2TG93AAA%3D/cancel")


async def test_microsoft_chosen_link_errors_and_oauth() -> None:
    graph = _Graph()
    async with httpx.AsyncClient(transport=httpx.MockTransport(graph)) as client:
        provider = MicrosoftGraphCalendarProvider(client)
        chosen = "https://zoom.example/j/123"
        ref = await provider.create_event(_user(), _spec(meeting_url=chosen))
        body = json.loads(graph.requests[-1].content)
        assert body["isOnlineMeeting"] is False
        assert "onlineMeetingProvider" not in body
        assert body["location"] == {"displayName": chosen}
        assert ref.meeting_url == chosen
        assert ref.meeting_provider is None

        window = Interval(START, END)
        with pytest.raises(CalendarNotConnectedError):
            await provider.free_busy([CalendarUser("x", "x@acme.example")], window)
        graph.status["/v1.0/me/calendar/getSchedule"] = 401
        with pytest.raises(CalendarError, match="microsoft_unauthorized") as err:
            await provider.free_busy([_user()], window)
        assert not err.value.transient
        graph.status["/v1.0/me/calendar/getSchedule"] = 503
        with pytest.raises(CalendarError) as err:
            await provider.free_busy([_user()], window)
        assert err.value.transient
        with pytest.raises(CalendarError, match="bad_event_id"):
            await provider.cancel_event(_user(), EventRef("microsoft", "../../users"), _spec())
        with pytest.raises(CalendarError, match="bad_request_id"):
            await provider.create_event(_user(), _spec(client_request_id="not hex"))
        # A cancel of an event that is gone is done.
        graph.status[f"/v1.0/me/events/{GRAPH_ID}/cancel"] = 404
        await provider.cancel_event(_user(), EventRef("microsoft", GRAPH_ID), _spec())

        config = MicrosoftOAuthConfig("client-id", "client-secret", "https://app.example/cb")
        assert "client-secret" not in repr(config)
        oauth = MicrosoftOAuth(config, client)
        url = oauth.authorization_url("state123", login_hint="omar@acme.example")
        assert url.startswith(
            "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?"
        )
        for part in ("state=state123", "offline_access", "Calendars.ReadWrite", "login_hint="):
            assert part in url
        tokens = await oauth.exchange_code("0.code")
        assert tokens.refresh_token == "M.R3_refresh"  # noqa: S105 (a fake token)
        assert "eyJ" not in repr(tokens)
        sent = parse_qs(graph.requests[-1].content.decode())
        assert sent["grant_type"] == ["authorization_code"]
        assert sent["redirect_uri"] == ["https://app.example/cb"]
        assert "offline_access" in sent["scope"][0]
        refreshed = await oauth.refresh("M.R3_refresh")
        assert refreshed.access_token == "eyJ.fresh"  # noqa: S105 (a fake token)
        graph.status["/organizations/oauth2/v2.0/token"] = 400
        with pytest.raises(CalendarNotConnectedError):
            await oauth.refresh("M.R3_revoked")
    assert load_microsoft_oauth_config({}) is None
    env = {
        "PAIRLY_MICROSOFT_CLIENT_ID": "id",
        "PAIRLY_MICROSOFT_CLIENT_SECRET": "secret",
        "PAIRLY_MICROSOFT_REDIRECT_URL": "https://app.example/cb",
    }
    assert load_microsoft_oauth_config(env) == MicrosoftOAuthConfig(
        "id", "secret", "https://app.example/cb", "organizations"
    )
    assert load_microsoft_oauth_config(env | {"PAIRLY_MICROSOFT_TENANT": "../evil"}) is None
    assert load_microsoft_oauth_config(env | {"PAIRLY_MICROSOFT_REDIRECT_URL": "http://x"}) is None


# ------------------------------------------------------------------ Zoom


class _Zoom:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host in {"zoom.us", "api.zoom.us"}
        path = request.url.path
        if path in self.status:
            return httpx.Response(self.status[path], json={"code": 3001})
        if path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "zoom.at", "expires_in": 3599})
        if path.endswith("/meetings") and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": 85746065432,
                    "join_url": "https://us06web.zoom.us/j/85746065432?pwd=abc",
                    "start_url": "https://us06web.zoom.us/s/85746065432?zak=host-secret",
                },
            )
        if request.method in {"PATCH", "DELETE"}:
            return httpx.Response(204)
        return httpx.Response(404)


CREDS = ZoomCredentials("acct-123", "zoom-client", "zoom-secret")


async def test_zoom_server_to_server_token_and_meetings() -> None:
    zoom = _Zoom()
    spec = _spec()
    async with httpx.AsyncClient(transport=httpx.MockTransport(zoom)) as client:
        provider = ZoomMeetingProvider(client, CREDS)
        link = await provider.create_meeting(_user(), spec)
        await provider.update_meeting(_user(), link.meeting_id or "", _spec(sequence=1))
        await provider.delete_meeting(_user(), link.meeting_id or "")
    token, create, patch, delete = zoom.requests  # one token for the instance
    assert str(token.url).startswith(ZOOM_TOKEN_URL)
    assert dict(token.url.params) == {"grant_type": "account_credentials", "account_id": "acct-123"}
    basic = base64.b64encode(b"zoom-client:zoom-secret").decode()
    assert token.headers["Authorization"] == f"Basic {basic}"
    assert str(create.url) == "https://api.zoom.us/v2/users/omar@acme.example/meetings"
    assert create.headers["Authorization"] == "Bearer zoom.at"
    body = json.loads(create.content)
    assert body["type"] == 2
    assert body["start_time"] == "2026-10-13T06:00:00Z"
    assert body["duration"] == 45
    assert body["timezone"] == "UTC"
    assert body["settings"]["waiting_room"] is True
    assert body["settings"]["join_before_host"] is False
    assert link.url == "https://us06web.zoom.us/j/85746065432?pwd=abc"
    assert link.provider == "zoom"
    assert link.meeting_id == "85746065432"
    assert "zak=" not in repr(link)  # the host's start link is never kept
    assert patch.method == "PATCH"
    assert patch.url.path == "/v2/meetings/85746065432"
    assert delete.method == "DELETE"
    assert "secret" not in repr(CREDS)


async def test_zoom_host_errors_and_bad_credentials() -> None:
    zoom = _Zoom()
    async with httpx.AsyncClient(transport=httpx.MockTransport(zoom)) as client:
        hosted = ZoomMeetingProvider(
            client, ZoomCredentials("a", "b", "c", host="interviews@acme.example")
        )
        await hosted.create_meeting(_user(), _spec())
        assert zoom.requests[-1].url.path == "/v2/users/interviews@acme.example/meetings"
        with pytest.raises(CalendarError, match="zoom_bad_meeting_id"):
            await hosted.delete_meeting(_user(), "../users/me")
        with pytest.raises(CalendarError, match="zoom_bad_host"):
            await ZoomMeetingProvider(client, CREDS).create_meeting(
                CalendarUser("x", "a/b"), _spec()
            )
        # A meeting already deleted is done.
        zoom.status["/v2/meetings/123456789"] = 404
        await hosted.delete_meeting(_user(), "123456789")
        zoom.status["/v2/meetings/123456789"] = 502
        with pytest.raises(CalendarError) as err:
            await hosted.update_meeting(_user(), "123456789", _spec())
        assert err.value.transient

        zoom.status["/oauth/token"] = 400
        fresh = ZoomMeetingProvider(client, CREDS)
        with pytest.raises(CalendarError, match="zoom_unauthorized") as err:
            await fresh.create_meeting(_user(), _spec())
        assert not err.value.transient
