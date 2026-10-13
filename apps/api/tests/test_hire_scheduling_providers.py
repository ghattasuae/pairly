# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Scheduling with Microsoft 365 and Zoom against a mocked transport (never the real
services): flags, the per person Microsoft connection with an encrypted refresh token,
free/busy through getSchedule, Outlook events with Teams links, the organisation's Zoom
app, and the reconciliation of external writes whose transaction failed (orphans)."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.audit import INFO_REQUEST_ID
from pairly_core.db import TenantSession, create_sessionmaker, set_tenant
from pairly_hire.scheduling import providers
from pairly_hire.scheduling.reconcile import reconcile_pending

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


# ------------------------------------------------------------------ orphan reconciliation


class FailingCommits:
    """Makes the next request transaction fail at commit, after everything in it ran
    (the provider already wrote). Side transactions (the journal) are not affected: only
    a request session carries a request id."""

    def __init__(self) -> None:
        self.armed = False

    def __call__(self, session: Any) -> None:
        if self.armed and session.info.get(INFO_REQUEST_ID):
            self.armed = False
            raise RuntimeError("simulated commit failure")


@pytest.fixture
def failing_commits() -> Any:
    hook = FailingCommits()
    event.listen(TenantSession, "before_commit", hook)
    yield hook
    event.remove(TenantSession, "before_commit", hook)


async def _fails(call: Any) -> None:
    try:
        response = await call
    except RuntimeError as exc:
        message = str(exc)
    else:
        assert response.status_code >= 500, response.text
        return
    assert "simulated commit failure" in message


async def operations_of(owner: AsyncEngine, world: HireWorld) -> list[Any]:
    return await owner_rows(
        owner,
        world.org_id,
        "SELECT kind, action, status, outcome, external_id, provider "
        "FROM hire_calendar_operations WHERE org_id = :o ORDER BY created_at",
        {"o": world.org_id},
    )


async def reconcile(api: HireApi, world: HireWorld) -> dict[str, int]:
    session = create_sessionmaker(api.engine)()
    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        # An hour later: every pending operation is stale.
        return await reconcile_pending(session, now=datetime.now(UTC) + timedelta(hours=1))


async def test_a_failed_commit_leaves_no_orphan_event_after_reconciliation(
    hire_api: HireApi,
    world: HireWorld,
    monkeypatch: pytest.MonkeyPatch,
    owner_engine: AsyncEngine,
    failing_commits: FailingCommits,
) -> None:
    api = hire_api
    mock = MockServices()
    providers.set_google_transport(httpx.MockTransport(mock))
    microsoft_env(monkeypatch)
    await api.call(
        "PUT", "/v1/flags/hire.scheduling.microsoft", world, "admin", json={"enabled": True}
    )
    await connect_microsoft(api, world, "recruiter")
    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Orphan Case"])
    app_id = ids["Orphan Case"]
    panel = [uid(world, "recruiter")]
    found = await slots(api, world, app_id, panel)
    first, last = found["slots"][0]["start"], found["slots"][-1]["start"]

    # 1. The booking's transaction fails after Outlook made the event: an orphan.
    failing_commits.armed = True
    await _fails(book(api, world, app_id, panel, first))
    assert len(mock.live_events()) == 1
    (orphan,) = mock.live_events()
    interviews = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT count(*) AS n FROM hire_interviews WHERE application_id = :a",
        {"a": app_id},
    )
    assert interviews[0].n == 0  # rolled back
    (op,) = await operations_of(owner_engine, world)
    assert (op.kind, op.action, op.status, op.external_id) == ("event", "create", "pending", orphan)
    # Microsoft got our idempotency key as transactionId.
    assert re.fullmatch(r"[0-9a-f]{32}", mock.events[orphan]["body"]["transactionId"])

    counts = await reconcile(api, world)
    assert counts["reconciled"] == 1
    assert mock.live_events() == []  # the orphan is cancelled
    (op,) = await operations_of(owner_engine, world)
    assert (op.status, op.outcome) == ("reconciled", "orphan_cancelled")

    # 2. A booking that commits marks its operation done: the job leaves it alone.
    booked = await book(api, world, app_id, panel, first)
    assert booked.status_code == 201, booked.text
    interview = booked.json()
    assert interview["calendar_provider"] == "microsoft"
    (event_id,) = mock.live_events()
    ops = await operations_of(owner_engine, world)
    assert [(o.status, o.outcome) for o in ops][-1] == ("done", "committed")
    assert (await reconcile(api, world))["reconciled"] == 0
    assert mock.live_events() == [event_id]
    booked_start = mock.events[event_id]["body"]["start"]

    # 3. A move whose transaction fails: Outlook moved, Pairly did not. The job writes
    # the committed time back to the same event.
    failing_commits.armed = True
    await _fails(
        api.call(
            "PATCH",
            f"/v1/hire/scheduling/interviews/{interview['id']}",
            world,
            "recruiter",
            json={"starts_at": last},
        )
    )
    assert mock.events[event_id]["body"]["start"] != booked_start
    counts = await reconcile(api, world)
    assert counts["reconciled"] == 1
    assert mock.events[event_id]["body"]["start"] == booked_start
    assert mock.live_events() == [event_id]
    outcomes = [(o.action, o.status, o.outcome) for o in await operations_of(owner_engine, world)]
    assert ("update", "reconciled", "resynced") in outcomes
    assert all(status != "pending" for _, status, _ in outcomes)

    audit = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT action, payload FROM audit_events WHERE action LIKE 'hire.calendar.reconcil%' "
        "ORDER BY seq",
    )
    assert [a.action for a in audit] == ["hire.calendar.reconciled", "hire.calendar.reconciled"]
    assert audit[0].payload["outcome"] == "orphan_cancelled"
    assert "teams" not in json.dumps(audit[0].payload)  # never the meeting link


async def test_a_refused_write_and_a_lost_zoom_meeting(
    hire_api: HireApi,
    world: HireWorld,
    owner_engine: AsyncEngine,
    failing_commits: FailingCommits,
) -> None:
    api = hire_api
    mock = MockServices()
    providers.set_google_transport(httpx.MockTransport(mock))
    assert (await save_zoom(api, world)).status_code == 200
    await api.call("PUT", "/v1/flags/hire.scheduling.zoom", world, "admin", json={"enabled": True})
    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Zoom Orphan"])
    app_id = ids["Zoom Orphan"]
    panel = [uid(world, "recruiter")]
    found = await slots(api, world, app_id, panel)

    # Zoom refuses: the booking fails closed and nothing was written.
    mock.status[("POST", "/v2/users/")] = 400
    refused = await book(api, world, app_id, panel, found["slots"][0]["start"])
    assert refused.status_code == 503, refused.text
    del mock.status[("POST", "/v2/users/")]
    # A commit failure after Zoom made the meeting: an orphan meeting.
    failing_commits.armed = True
    await _fails(book(api, world, app_id, panel, found["slots"][0]["start"]))
    (meeting_id,) = mock.meetings
    assert mock.meetings[meeting_id]["deleted"] is False

    counts = await reconcile(api, world)
    assert counts["reconciled"] == 2
    assert mock.meetings[meeting_id]["deleted"] is True
    outcomes = sorted(o.outcome for o in await operations_of(owner_engine, world))
    assert outcomes == ["nothing_written", "orphan_meeting_deleted"]


# ------------------------------------------------------------------ organisation status


async def test_organisation_integration_status_for_microsoft_and_zoom(
    hire_api: HireApi, world: HireWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = hire_api
    path = "/v1/hire/settings/scheduling/integrations"
    assert (await api.call("GET", path, world, "recruiter")).status_code == 403
    first = await api.call("GET", path, world, "admin")
    assert first.status_code == 200, first.text
    by = {p["provider"]: p for p in first.json()["providers"]}
    assert set(by) == {"google", "microsoft", "zoom"}
    assert by["microsoft"]["state"] == "needs_setup"
    assert by["microsoft"]["missing"] == ["server_client", "feature_flag", "people_connect"]
    assert by["microsoft"]["flag_key"] == "hire.scheduling.microsoft"
    assert any("Entra" in step for step in by["microsoft"]["steps"])
    assert by["zoom"]["missing"] == ["zoom_app", "feature_flag"]
    assert by["zoom"]["connected_people"] is None
    assert first.json()["operations_needing_review"] == 0

    mock = MockServices()
    providers.set_google_transport(httpx.MockTransport(mock))
    microsoft_env(monkeypatch)
    for flag in ("hire.scheduling.microsoft", "hire.scheduling.zoom"):
        await api.call("PUT", f"/v1/flags/{flag}", world, "admin", json={"enabled": True})
    await connect_microsoft(api, world, "interviewer")
    assert (await save_zoom(api, world, host="interviews@acme.example")).status_code == 200
    after = await api.call("GET", path, world, "admin")
    by = {p["provider"]: p for p in after.json()["providers"]}
    assert by["microsoft"]["state"] == "connected"
    assert by["microsoft"]["connected_people"] == 1
    assert by["microsoft"]["missing"] == []
    assert by["zoom"]["state"] == "connected"
    assert by["zoom"]["status"] == "active"
    assert by["zoom"]["verified_at"]
    assert by["google"]["state"] == "needs_setup"
    assert "zoom-secret-1" not in after.text
