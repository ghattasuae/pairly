# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Calendar files (RFC 5545), the fake calendar, the Google provider against a mocked
transport (never Google itself), and calendar attachments on email."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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
from pairly_core.integrations.calendar.fake import (
    FakeCalendarProvider,
    FakeMeetingProvider,
    InMemoryFakeCalendarStore,
    fake_meeting_url,
    generated_blocks,
)
from pairly_core.integrations.calendar.google import (
    API_BASE,
    TOKEN_URL,
    GoogleCalendarProvider,
    GoogleOAuth,
    GoogleOAuthConfig,
    conference_request_id,
    load_google_oauth_config,
)
from pairly_core.notifications.email import (
    Attachment,
    OutgoingEmail,
    ResendTransport,
    build_message,
)
from pairly_core.scheduling.availability import Interval
from pairly_core.scheduling.ics import (
    IcsError,
    IcsEvent,
    IcsPerson,
    build_ics,
    escape_text,
    fold,
    read_ics,
    unescape_text,
)

START = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)
END = START + timedelta(minutes=45)


def _event(**kw: Any) -> IcsEvent:
    values: dict[str, Any] = {
        "uid": "iv-0123456789abcdef@pairly.localhost",
        "sequence": 0,
        "start": START,
        "end": END,
        "summary": "Interview: Senior Backend Engineer",
        "organizer": IcsPerson("no-reply@pairly.localhost", "Acme Logistics"),
        "attendees": (
            IcsPerson("layla@example.test", "Layla Haddad"),
            IcsPerson("omar@example.test", None),
        ),
        "description": "Join: https://meet.pairly.example/abc-defg-hij\nBring, nothing; just you.",
        "location": "https://meet.pairly.example/abc-defg-hij",
        "url": "https://meet.pairly.example/abc-defg-hij",
    }
    values.update(kw)
    return IcsEvent(**values)


# ------------------------------------------------------------------ ICS


def test_request_has_the_rfc5545_essentials() -> None:
    text = build_ics(_event(), "REQUEST", stamp=START - timedelta(days=1))
    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")
    for line in text.split("\r\n"):
        assert len(line.encode()) <= 75
    parsed = read_ics(text)
    assert parsed.calendar["METHOD"] == ["REQUEST"]
    assert parsed.calendar["VERSION"] == ["2.0"]
    assert "PRODID" in parsed.calendar
    (event,) = parsed.events
    assert event["UID"] == ["iv-0123456789abcdef@pairly.localhost"]
    assert event["SEQUENCE"] == ["0"]
    assert event["DTSTART"] == ["20261013T060000Z"]
    assert event["DTEND"] == ["20261013T064500Z"]
    assert event["DTSTAMP"] == ["20261012T060000Z"]
    assert event["STATUS"] == ["CONFIRMED"]
    assert event["ORGANIZER"] == ["mailto:no-reply@pairly.localhost"]
    assert event["ATTENDEE"] == ["mailto:layla@example.test", "mailto:omar@example.test"]
    assert "RSVP=TRUE" in text
    assert 'CN="Layla Haddad"' in text
    assert unescape_text(event["DESCRIPTION"][0]) == (
        "Join: https://meet.pairly.example/abc-defg-hij\nBring, nothing; just you."
    )


def test_cancel_and_sequence() -> None:
    text = build_ics(_event(sequence=2), "CANCEL")
    parsed = read_ics(text)
    assert parsed.calendar["METHOD"] == ["CANCEL"]
    assert parsed.events[0]["SEQUENCE"] == ["2"]
    assert parsed.events[0]["STATUS"] == ["CANCELLED"]
    assert "RSVP" not in text


def test_text_escaping_folding_and_injection() -> None:
    assert escape_text("a\\b;c,d\r\ne") == "a\\\\b\\;c\\,d\\ne"
    arabic = "SUMMARY:" + "مقابلة " * 30
    folded = fold(arabic)
    for line in folded.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    assert folded.replace("\r\n ", "") == arabic  # no character split
    # A name or summary cannot add a property line.
    text = build_ics(
        _event(
            summary="Hi\r\nATTENDEE:mailto:evil@example.test",
            attendees=(IcsPerson("layla@example.test", 'L"ay:la\r\nX-EVIL:1'),),
        ),
        "REQUEST",
    )
    parsed = read_ics(text)
    assert parsed.events[0]["ATTENDEE"] == ["mailto:layla@example.test"]
    assert "X-EVIL" not in parsed.events[0]
    with pytest.raises(IcsError):
        build_ics(_event(uid="bad uid with spaces"), "REQUEST")
    with pytest.raises(IcsError):
        build_ics(_event(attendees=(IcsPerson("not an email"),)), "REQUEST")
    with pytest.raises(IcsError):
        build_ics(_event(end=START), "REQUEST")
    with pytest.raises(IcsError):
        build_ics(_event(start=datetime(2026, 1, 1), end=datetime(2026, 1, 2)), "REQUEST")


# ------------------------------------------------------------------ email attachment


def test_calendar_attachment_on_email_and_resend() -> None:
    ics = build_ics(_event(), "REQUEST")
    attachment = Attachment("invite.ics", "text/calendar", ics, {"method": "REQUEST"})
    email = OutgoingEmail(
        to="layla@example.test",
        sender="Pairly <no-reply@pairly.localhost>",
        subject="Interview",
        text="Hello",
        html=None,
        locale="en",
        message_id="m1@pairly.localhost",
        idempotency_key="org:k",
        attachments=(attachment,),
    )
    msg = build_message(email)
    assert msg.get_content_type() == "multipart/mixed"
    parts = [p for p in msg.walk() if p.get_content_type() == "text/calendar"]
    assert len(parts) == 1
    assert parts[0].get_param("method") == "REQUEST"
    assert parts[0].get_filename() == "invite.ics"
    assert "BEGIN:VCALENDAR" in parts[0].get_content()
    assert Attachment.from_json(attachment.to_json()) == attachment
    assert Attachment.from_json({"filename": "../x", "mime_type": "text/plain"}) is None
    with pytest.raises(ValueError, match="text attachments"):
        Attachment("a.bin", "application/octet-stream", "x")


async def test_resend_carries_the_attachment() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "re_1"})

    ics = build_ics(_event(), "CANCEL")
    email = OutgoingEmail(
        to="layla@example.test",
        sender="no-reply@pairly.localhost",
        subject="Cancelled",
        text="Hello",
        html=None,
        locale="en",
        message_id="m2@pairly.localhost",
        idempotency_key="org:k2",
        attachments=(Attachment("cancel.ics", "text/calendar", ics, {"method": "CANCEL"}),),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ResendTransport(api_key="k", client=client).send(email)
    (sent,) = seen["attachments"]
    assert sent["filename"] == "cancel.ics"
    assert sent["content_type"] == "text/calendar; charset=utf-8; method=CANCEL"
    assert base64.b64decode(sent["content"]).decode() == ics


# ------------------------------------------------------------------ fake calendar


async def test_fake_calendar_is_deterministic_and_records_events() -> None:
    store = InMemoryFakeCalendarStore()
    provider = FakeCalendarProvider(store, seed="demo")
    user = CalendarUser("u1", "u1@example.test", "Asia/Dubai")
    window = Interval(datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 10, 19, tzinfo=UTC))
    first = await provider.free_busy([user], window)
    again = await FakeCalendarProvider(InMemoryFakeCalendarStore(), seed="demo").free_busy(
        [user], window
    )
    assert first == again  # same seed, same calendar
    other_seed = await FakeCalendarProvider(InMemoryFakeCalendarStore(), seed="x").free_busy(
        [user], window
    )
    assert other_seed != first
    zone = ZoneInfo("Asia/Dubai")
    for block in first["u1"]:
        local_start = block.start.astimezone(zone)
        assert local_start.weekday() < 5
        assert 8 <= local_start.hour < 18
        assert local_start.minute in (0, 30)
    assert generated_blocks("demo", "u1", date(2026, 10, 17), "Asia/Dubai") == []  # Saturday
    # An event written through the provider shows as busy for every panelist.
    spec = EventSpec(
        uid="iv-abcdef0123456789@pairly.localhost",
        sequence=0,
        summary="Interview",
        start=datetime(2026, 10, 18, 6, tzinfo=UTC),
        end=datetime(2026, 10, 18, 7, tzinfo=UTC),
        attendees=(EventAttendee("p2@example.test", user_key="u2"),),
    )
    ref = await provider.create_event(user, spec)
    assert ref.meeting_url == fake_meeting_url(spec.uid)
    assert ref.meeting_url is not None
    assert ref.meeting_url.startswith("https://meet.pairly.example/")
    busy = await provider.free_busy([user, CalendarUser("u2", "p2@example.test")], window)
    booked = Interval(spec.start, spec.end)
    assert booked in busy["u1"]
    assert booked in busy["u2"]
    moved = replace(
        spec, sequence=1, start=spec.start + timedelta(hours=1), end=spec.end + timedelta(hours=1)
    )
    await provider.update_event(user, ref, moved)
    busy = await provider.free_busy([user], window)
    assert Interval(moved.start, moved.end) in busy["u1"]
    assert Interval(spec.start, spec.end) not in busy["u1"]
    await provider.cancel_event(user, ref, moved)
    busy = await provider.free_busy([user], window)
    assert Interval(moved.start, moved.end) not in busy["u1"]
    link = await FakeMeetingProvider().create_meeting(user, spec)
    assert link.url == ref.meeting_url


# ------------------------------------------------------------------ Google (mocked)


class _Tokens:
    def __init__(self, token: str) -> None:
        self.token = token

    async def access_token(self) -> str:
        return self.token


class _Google:
    """A mocked Google: records every request and answers like the real API."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        assert request.url.host in {"www.googleapis.com", "oauth2.googleapis.com"}
        if path in self.status:
            return httpx.Response(self.status[path], json={"error": "x"})
        if path == "/token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            body: dict[str, Any] = {"access_token": "ya29.fresh", "expires_in": 3599}
            if form["grant_type"] == "authorization_code":
                body["refresh_token"] = "1//refresh"  # noqa: S105 (a fake token)
            return httpx.Response(200, json=body | {"scope": "calendar"})
        if path.endswith("/freeBusy"):
            sent = json.loads(request.content)
            email = sent["items"][0]["id"]
            return httpx.Response(
                200,
                json={
                    "calendars": {
                        email: {
                            "busy": [
                                {"start": "2026-10-13T05:00:00Z", "end": "2026-10-13T06:00:00Z"}
                            ]
                        }
                    }
                },
            )
        if path.endswith("/events") and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "id": "evt123",
                    "hangoutLink": "https://meet.google.com/abc-defg-hij",
                    "conferenceData": {
                        "entryPoints": [
                            {
                                "entryPointType": "video",
                                "uri": "https://meet.google.com/abc-defg-hij",
                            }
                        ]
                    },
                },
            )
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": "evt123"})
        if request.method == "GET" and path.endswith("/events/" + "a" * 32):
            return httpx.Response(
                200, json={"id": "a" * 32, "hangoutLink": "https://meet.google.com/x"}
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)


def _user(key: str = "u1", email: str = "omar@acme.example") -> CalendarUser:
    return CalendarUser(key, email, "Asia/Dubai", tokens=_Tokens(f"token-{key}"))


async def test_google_free_busy_request_shape() -> None:
    google = _Google()
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        provider = GoogleCalendarProvider(client)
        busy = await provider.free_busy(
            [_user(), _user("u2", "sara@acme.example")],
            Interval(datetime(2026, 10, 13, tzinfo=UTC), datetime(2026, 10, 14, tzinfo=UTC)),
        )
    assert len(google.requests) == 2  # one request per person, with their own token
    first = google.requests[0]
    assert str(first.url) == f"{API_BASE}/freeBusy"
    assert first.method == "POST"
    assert first.headers["Authorization"] == "Bearer token-u1"
    assert json.loads(first.content) == {
        "timeMin": "2026-10-13T00:00:00Z",
        "timeMax": "2026-10-14T00:00:00Z",
        "timeZone": "UTC",
        "items": [{"id": "omar@acme.example"}],
    }
    assert google.requests[1].headers["Authorization"] == "Bearer token-u2"
    assert busy["u1"] == [
        Interval(datetime(2026, 10, 13, 5, tzinfo=UTC), datetime(2026, 10, 13, 6, tzinfo=UTC))
    ]


async def test_google_events_insert_with_meet_conference_data() -> None:
    google = _Google()
    spec = EventSpec(
        uid="iv-0123456789abcdef@pairly.localhost",
        sequence=0,
        summary="Interview: Senior Backend Engineer",
        start=START,
        end=END,
        attendees=(
            EventAttendee("layla@example.test", "Layla Haddad"),
            EventAttendee("sara@acme.example", "Sara", user_key="u2"),
        ),
        description="Interview with Acme Logistics",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        provider = GoogleCalendarProvider(client)
        ref = await provider.create_event(_user(), spec)
        moved = EventSpec(
            uid=spec.uid,
            sequence=1,
            summary=spec.summary,
            start=START + timedelta(days=1),
            end=END + timedelta(days=1),
            attendees=spec.attendees,
        )
        updated = await provider.update_event(_user(), ref, moved)
        await provider.cancel_event(_user(), updated, moved)
    insert, patch, delete = google.requests
    assert insert.method == "POST"
    assert insert.url.path == "/calendar/v3/calendars/primary/events"
    assert dict(insert.url.params) == {"conferenceDataVersion": "1", "sendUpdates": "none"}
    body = json.loads(insert.content)
    assert body["iCalUID"] == spec.uid
    assert body["sequence"] == 0
    assert body["start"] == {"dateTime": "2026-10-13T06:00:00Z", "timeZone": "UTC"}
    assert body["end"] == {"dateTime": "2026-10-13T06:45:00Z", "timeZone": "UTC"}
    assert body["attendees"] == [
        {"email": "layla@example.test", "displayName": "Layla Haddad"},
        {"email": "sara@acme.example", "displayName": "Sara"},
    ]
    assert body["conferenceData"] == {
        "createRequest": {
            "requestId": conference_request_id(spec.uid),
            "conferenceSolutionKey": {"type": "hangoutsMeet"},
        }
    }
    meet = "https://meet.google.com/abc-defg-hij"
    assert ref == EventRef("google", "evt123", meet, "google_meet")
    assert patch.method == "PATCH"
    assert patch.url.path.endswith("/events/evt123")
    patched = json.loads(patch.content)
    assert patched["sequence"] == 1
    assert "conferenceData" not in patched
    assert updated.meeting_url == "https://meet.google.com/abc-defg-hij"
    assert delete.method == "DELETE"
    assert dict(delete.url.params) == {"sendUpdates": "none"}


async def test_google_errors_and_oauth() -> None:
    google = _Google()
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        provider = GoogleCalendarProvider(client)
        window = Interval(START, END)
        with pytest.raises(CalendarNotConnectedError):
            await provider.free_busy([CalendarUser("x", "x@acme.example")], window)
        google.status["/calendar/v3/freeBusy"] = 401
        with pytest.raises(CalendarError, match="google_unauthorized") as err:
            await provider.free_busy([_user()], window)
        assert not err.value.transient
        google.status["/calendar/v3/freeBusy"] = 503
        with pytest.raises(CalendarError) as err:
            await provider.free_busy([_user()], window)
        assert err.value.transient
        with pytest.raises(CalendarError, match="bad_event_id"):
            await provider.cancel_event(
                _user(),
                EventRef("google", "../../users/me"),
                EventSpec(
                    uid="iv-0000000000000000@x", sequence=0, summary="s", start=START, end=END
                ),
            )

        config = GoogleOAuthConfig("client-id", "client-secret", "https://app.example/cb")
        assert "client-secret" not in repr(config)
        oauth = GoogleOAuth(config, client)
        url = oauth.authorization_url("state123", login_hint="omar@acme.example")
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        wanted = ("access_type=offline", "prompt=consent", "state=state123", "calendar.freebusy")
        for part in wanted:
            assert part in url
        tokens = await oauth.exchange_code("4/code")
        assert tokens.refresh_token == "1//refresh"  # noqa: S105 (a fake token)
        assert tokens.access_token == "ya29.fresh"  # noqa: S105 (a fake token)
        assert "ya29" not in repr(tokens)
        refreshed = await oauth.refresh("1//refresh")
        assert refreshed.refresh_token is None
        token_request = google.requests[-1]
        assert str(token_request.url) == TOKEN_URL
        google.status["/token"] = 400
        with pytest.raises(CalendarNotConnectedError):
            await oauth.refresh("1//revoked")
    assert load_google_oauth_config({}) is None
    assert load_google_oauth_config(
        {
            "PAIRLY_GOOGLE_CLIENT_ID": "id",
            "PAIRLY_GOOGLE_CLIENT_SECRET": "secret",
            "PAIRLY_GOOGLE_REDIRECT_URL": "https://app.example/cb",
        }
    ) == GoogleOAuthConfig("id", "secret", "https://app.example/cb")


async def test_google_insert_with_our_event_id_is_idempotent() -> None:
    google = _Google()
    spec = EventSpec(
        uid="iv-0123456789abcdef@pairly.localhost",
        sequence=0,
        summary="Interview",
        start=START,
        end=END,
        client_request_id="a" * 32,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        provider = GoogleCalendarProvider(client)
        await provider.create_event(_user(), spec)
        assert json.loads(google.requests[0].content)["id"] == "a" * 32
        # A retried insert answers 409: the existing event is read, not made twice.
        google.status["/calendar/v3/calendars/primary/events"] = 409
        ref = await provider.create_event(_user(), spec)
        assert ref.event_id == "a" * 32
        assert ref.meeting_url == "https://meet.google.com/x"
        assert [r.method for r in google.requests] == ["POST", "POST", "GET"]
        with pytest.raises(CalendarError, match="bad_request_id"):
            await provider.create_event(_user(), replace(spec, client_request_id="../x"))
