# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Scheduling with Microsoft 365 against a mocked transport (never Microsoft itself):
the flag, the per person connection with an encrypted refresh token, free/busy through
getSchedule and a booking that writes an Outlook event with a Teams link."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_hire.scheduling import providers

from .hire_support import (
    HireApi,
    HireWorld,
    hire_api,
    open_job,
    owner_rows,
    retire_documents,
    seed_hire_world,
)
from .test_hire_scheduling import applicants, book, slots, uid

__all__ = ["hire_api"]  # fixture

TEAMS = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_test"


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> AsyncIterator[HireWorld]:
    made = await seed_hire_world(owner_engine)
    yield made
    await retire_documents(owner_engine, made.org_id)


@pytest.fixture(autouse=True)
def _reset() -> Any:
    yield
    providers.set_google_transport(None)


class MockServices:
    """Answers Microsoft Graph, the Microsoft identity platform and Zoom like the real
    services, records every request, and can be told to fail a path."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.status: dict[tuple[str, str], int] = {}
        self.events: dict[str, dict[str, Any]] = {}
        self.meetings: dict[str, dict[str, Any]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        host, path, method = request.url.host, request.url.path, request.method
        assert host in {
            "login.microsoftonline.com",
            "graph.microsoft.com",
            "zoom.us",
            "api.zoom.us",
        }, host
        for (m, prefix), code in self.status.items():
            if m == method and path.startswith(prefix):
                return httpx.Response(code, json={"error": "x"})
        if host == "login.microsoftonline.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "eyJ.test",
                    "refresh_token": "M.R3_test-refresh",
                    "expires_in": 3600,
                    "scope": "Calendars.ReadWrite User.Read",
                },
            )
        if host == "zoom.us":
            return httpx.Response(200, json={"access_token": "zoom.test", "expires_in": 3600})
        if host == "api.zoom.us":
            return self._zoom(request)
        if path.endswith("/getSchedule"):
            email = json.loads(request.content)["schedules"][0]
            return httpx.Response(200, json={"value": [{"scheduleId": email, "scheduleItems": []}]})
        if path == "/v1.0/me/events" and method == "POST":
            body = json.loads(request.content)
            event_id = f"AAMk{uuid.uuid4().hex}="
            self.events[event_id] = {"body": body, "cancelled": False}
            return httpx.Response(
                201,
                json={"id": event_id, "iCalUId": "0400", "onlineMeeting": {"joinUrl": TEAMS}},
            )
        match = re.fullmatch(r"/v1\.0/me/events/([^/]+)(/cancel)?", path)
        if match and match.group(1) in self.events:
            event = self.events[match.group(1)]
            if match.group(2):
                event["cancelled"] = True
                return httpx.Response(202)
            event["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": match.group(1), "onlineMeeting": None})
        return httpx.Response(404)

    def _zoom(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path.endswith("/meetings"):
            meeting_id = str(80000000000 + len(self.meetings))
            self.meetings[meeting_id] = {"body": json.loads(request.content), "deleted": False}
            return httpx.Response(
                201,
                json={"id": int(meeting_id), "join_url": f"https://zoom.example/j/{meeting_id}"},
            )
        match = re.fullmatch(r"/v2/meetings/(\d+)", path)
        if match and match.group(1) in self.meetings:
            meeting = self.meetings[match.group(1)]
            if method == "DELETE":
                meeting["deleted"] = True
            else:
                meeting["body"] = json.loads(request.content)
            return httpx.Response(204)
        return httpx.Response(404)

    def live_events(self) -> list[str]:
        return [k for k, v in self.events.items() if not v["cancelled"]]


def microsoft_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAIRLY_MICROSOFT_CLIENT_ID", "00000000-app")
    monkeypatch.setenv("PAIRLY_MICROSOFT_CLIENT_SECRET", "not-a-real-secret")
    monkeypatch.setenv(
        "PAIRLY_MICROSOFT_REDIRECT_URL", "https://app.pairly.example/en/settings/calendar"
    )


async def connect_microsoft(api: HireApi, world: HireWorld, who: str) -> None:
    start = await api.call("POST", "/v1/hire/scheduling/integrations/microsoft/start", world, who)
    assert start.status_code == 200, start.text
    url = start.json()["url"]
    assert url.startswith("https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize")
    state = re.search(r"state=([^&]+)", url).group(1)  # type: ignore[union-attr]
    done = await api.call(
        "POST",
        "/v1/hire/scheduling/integrations/microsoft/callback",
        world,
        who,
        json={"code": "0.AX-code", "state": state},
    )
    assert done.status_code == 200, done.text
    assert done.json()["using"] == "microsoft"


async def test_microsoft_connection_free_busy_and_teams_booking(
    hire_api: HireApi, world: HireWorld, monkeypatch: pytest.MonkeyPatch, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    # Flag off (the default): no connect.
    start = await api.call(
        "POST", "/v1/hire/scheduling/integrations/microsoft/start", world, "recruiter"
    )
    assert start.status_code == 409
    mock = MockServices()
    providers.set_google_transport(httpx.MockTransport(mock))
    microsoft_env(monkeypatch)
    flag = await api.call(
        "PUT", "/v1/flags/hire.scheduling.microsoft", world, "admin", json={"enabled": True}
    )
    assert flag.status_code == 200, flag.text
    await connect_microsoft(api, world, "recruiter")
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT refresh_token FROM hire_calendar_connections WHERE provider = 'microsoft'",
    )
    assert "M.R3_test-refresh" not in rows[0].refresh_token  # encrypted

    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Micro Soft"])
    panel = [uid(world, "recruiter")]
    found = await slots(api, world, ids["Micro Soft"], panel)
    assert any(c.url.path.endswith("/getSchedule") for c in mock.calls)
    booked = await book(api, world, ids["Micro Soft"], panel, found["slots"][0]["start"])
    assert booked.status_code == 201, booked.text
    interview = booked.json()
    assert interview["calendar_provider"] == "microsoft"
    assert interview["meeting_url"] == TEAMS
    (event,) = mock.events.values()
    assert event["body"]["isOnlineMeeting"] is True
    assert event["body"]["onlineMeetingProvider"] == "teamsForBusiness"

    status = await api.call("GET", "/v1/hire/scheduling/integrations", world, "recruiter")
    microsoft = {p["provider"]: p for p in status.json()["providers"]}["microsoft"]
    assert microsoft["connected"] is True
    assert microsoft["availability"] == "available"
    off = await api.call(
        "POST", "/v1/hire/scheduling/integrations/microsoft/disconnect", world, "recruiter"
    )
    assert off.json()["using"] == "fake"


ZOOM_APP = {"account_id": "acct-1", "client_id": "zoom-client", "client_secret": "zoom-secret-1"}


async def save_zoom(api: HireApi, world: HireWorld, who: str = "admin", **extra: Any) -> Any:
    return await api.call(
        "PUT",
        "/v1/hire/settings/scheduling/integrations/zoom",
        world,
        who,
        json=ZOOM_APP | extra,
    )


async def test_zoom_links_with_the_fake_calendar(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    mock = MockServices()
    providers.set_google_transport(httpx.MockTransport(mock))
    # Only an administrator saves the app; a refused app is not kept.
    assert (await save_zoom(api, world, "recruiter")).status_code == 403
    mock.status[("POST", "/oauth/token")] = 401
    refused = await save_zoom(api, world)
    assert refused.status_code == 400, refused.text
    del mock.status[("POST", "/oauth/token")]
    saved = await save_zoom(api, world)
    assert saved.status_code == 200, saved.text
    zoom = {p["provider"]: p for p in saved.json()["providers"]}["zoom"]
    assert zoom["configured"] is True
    assert zoom["availability"] == "needs_setup"  # the flag is still off
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT client_secret, account_id FROM hire_meeting_integrations",
    )
    assert "zoom-secret-1" not in rows[0].client_secret  # encrypted
    assert "acct-1" not in rows[0].account_id

    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Zoe Oom", "Flag Off"])
    panel = [uid(world, "recruiter")]
    # Flag off: the fake calendar's own link, no Zoom meeting.
    found = await slots(api, world, ids["Flag Off"], panel)
    plain = await book(api, world, ids["Flag Off"], panel, found["slots"][0]["start"])
    assert plain.json()["meeting_url"].startswith("https://meet.pairly.example/")
    assert not mock.meetings

    flag = await api.call(
        "PUT", "/v1/flags/hire.scheduling.zoom", world, "admin", json={"enabled": True}
    )
    assert flag.status_code == 200, flag.text
    found = await slots(api, world, ids["Zoe Oom"], panel)
    booked = await book(api, world, ids["Zoe Oom"], panel, found["slots"][0]["start"])
    assert booked.status_code == 201, booked.text
    interview = booked.json()
    ((meeting_id, meeting),) = mock.meetings.items()
    assert interview["meeting_url"] == f"https://zoom.example/j/{meeting_id}"
    assert interview["calendar_provider"] == "fake"
    assert meeting["body"]["duration"] == 45
    create = next(c for c in mock.calls if c.url.path.endswith("/meetings"))
    assert re.fullmatch(r"/v2/users/[^/]+@[^/]+/meetings", create.url.path)
    first_start = meeting["body"]["start_time"]

    moved = await api.call(
        "PATCH",
        f"/v1/hire/scheduling/interviews/{interview['id']}",
        world,
        "recruiter",
        json={"starts_at": found["slots"][-1]["start"]},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["meeting_url"] == interview["meeting_url"]  # same meeting, moved
    assert mock.meetings[meeting_id]["body"]["start_time"] != first_start
    assert len(mock.meetings) == 1
    cancelled = await api.client.post(
        f"/v1/hire/scheduling/interviews/{interview['id']}/cancel",
        headers=api.headers(world, "recruiter", extra={"Idempotency-Key": uuid.uuid4().hex}),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert mock.meetings[meeting_id]["deleted"] is True

    status = await api.call("GET", "/v1/hire/scheduling/integrations", world, "interviewer")
    zoom = {p["provider"]: p for p in status.json()["providers"]}["zoom"]
    assert zoom["availability"] == "available"
    removed = await api.call(
        "POST", "/v1/hire/settings/scheduling/integrations/zoom/remove", world, "admin"
    )
    zoom = {p["provider"]: p for p in removed.json()["providers"]}["zoom"]
    assert zoom["configured"] is False
    actions = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT action FROM audit_events WHERE action LIKE 'hire.meetings.%' ORDER BY seq",
    )
    assert [r.action for r in actions] == ["hire.meetings.zoom_saved", "hire.meetings.zoom_removed"]
