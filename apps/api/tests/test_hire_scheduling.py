# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire interview scheduling (WP2.8) on Postgres, through the API.

* Suggested slots are free for every panelist; booking sends the candidate and the
  panel an invitation with a calendar file (METHOD:REQUEST, SEQUENCE 0); moving it
  sends SEQUENCE 1; cancelling sends METHOD:CANCEL with SEQUENCE 2.
* The self-scheduling link: real slots in the candidate's zone, books once (the old link
  reads as used), the next link changes or cancels; forged and expired links fail.
* No double booking under concurrency: two candidates racing for the same panel and
  time get one booking and one ``slot_taken``; one link used twice at once books once.
* Reminders 24 hours and 1 hour before, once each.
* The interviewer briefing respects blind mode.
* No-show tracking; interview availability and settings; the Google connection with a
  mocked Google (never the real one); tenant isolation of every new table.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker, set_portal_subject, set_tenant
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.notifications.delivery import Transports, deliver_org
from pairly_core.notifications.outbox import OutboxTransport
from pairly_core.scheduling.ics import read_ics
from pairly_hire.portal.auth import portal_subject
from pairly_hire.scheduling import providers
from pairly_hire.scheduling.emails import SchedulingEnv
from pairly_hire.scheduling.reminders import send_due_reminders
from pairly_hire.scheduling.tokens import read_schedule_token
from pairly_hire.screening import reviewer as screening_reviewer

from .hire_support import (
    HireApi,
    HireWorld,
    cv_pdf,
    hire_api,
    open_job,
    owner_rows,
    retire_documents,
    seed_hire_world,
)
from .notify_support import LINK_SECRET, notify_settings

__all__ = ["hire_api"]  # fixture

NEW_TABLES = (
    "hire_scheduling_settings",
    "hire_interviewer_settings",
    "hire_interviews",
    "hire_interview_panelists",
    "hire_calendar_blocks",
    "hire_calendar_connections",
)
TOKEN_RE = re.compile(r"/schedule#(hs1\.[A-Za-z0-9._-]+)")


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> AsyncIterator[HireWorld]:
    made = await seed_hire_world(owner_engine)
    yield made
    await retire_documents(owner_engine, made.org_id)


@pytest.fixture
async def other(owner_engine: AsyncEngine) -> AsyncIterator[HireWorld]:
    made = await seed_hire_world(owner_engine)
    yield made
    await retire_documents(owner_engine, made.org_id)


@pytest.fixture(autouse=True)
def _reset() -> Any:
    yield
    screening_reviewer.register()
    providers.set_google_transport(None)


_IP = iter(range(10, 250))


async def applicants(
    api: HireApi, world: HireWorld, job: dict[str, Any], names: list[str]
) -> dict[str, str]:
    refs: dict[str, str] = {}
    for name in names:
        slug = name.lower().replace(" ", ".")
        refs[name] = await api.apply(
            world,
            job["slug"],
            cv_pdf(f"{name}-{uuid.uuid4().hex}"),
            email=f"{slug}.{uuid.uuid4().hex[:6]}@example.test",
            full_name=name,
            ip=f"198.51.100.{next(_IP)}",
        )
    listed = await api.call("GET", f"/v1/hire/applications?job_id={job['id']}", world, "recruiter")
    by_ref = {a["reference"]: a["id"] for a in listed.json()["items"]}
    return {name: by_ref[ref] for name, ref in refs.items()}


def uid(world: HireWorld, who: str) -> str:
    return str(world.people[who][0])


async def deliver(api: HireApi, world: HireWorld, outbox: Path) -> list[dict[str, Any]]:
    await deliver_org(
        create_sessionmaker(api.engine),
        world.org_id,
        Transports(
            settings=notify_settings(), email=OutboxTransport(outbox), slack=FakeSlackTransport()
        ),
    )
    return [json.loads(p.read_text()) for p in sorted(outbox.glob("*.json"))]


def ics_of(mail: dict[str, Any]) -> Any:
    (attachment,) = mail["attachments"]
    assert attachment["content_type"].startswith("text/calendar")
    return read_ics(attachment["text"])


def to(mails: list[dict[str, Any]], address: str) -> list[dict[str, Any]]:
    """Interview mail to ``address`` (not the acknowledgement of the application)."""
    return [m for m in mails if address in m["to"] and "nterview" in m["subject"]]


async def slots(
    api: HireApi, world: HireWorld, app_id: str, panel: list[str], **extra: Any
) -> dict[str, Any]:
    response = await api.call(
        "POST",
        f"/v1/hire/scheduling/applications/{app_id}/slots",
        world,
        "recruiter",
        json={"panel": panel, "duration_min": 45, **extra},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def key() -> dict[str, str]:
    return {"Idempotency-Key": f"k-{uuid.uuid4().hex}"}


async def book(
    api: HireApi, world: HireWorld, app_id: str, panel: list[str], starts_at: str, **extra: Any
) -> httpx.Response:
    return await api.client.post(
        f"/v1/hire/scheduling/applications/{app_id}/interviews",
        json={"mode": "book", "panel": panel, "duration_min": 45, "starts_at": starts_at, **extra},
        headers=api.headers(world, "recruiter", extra=key()),
    )


async def send_link(api: HireApi, world: HireWorld, app_id: str, panel: list[str]) -> Any:
    response = await api.client.post(
        f"/v1/hire/scheduling/applications/{app_id}/interviews",
        json={"mode": "link", "panel": panel, "duration_min": 45},
        headers=api.headers(world, "recruiter", extra=key()),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def candidate_email(owner: AsyncEngine, world: HireWorld, app_id: str) -> str:
    session = create_sessionmaker(owner)()
    from pairly_hire.pipeline.records import load_record

    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        record = await load_record(session, uuid.UUID(app_id))
        return record.email


# ------------------------------------------------------------------ book, move, cancel


async def test_book_move_and_cancel_with_calendar_files(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Layla Haddad"])
    app_id = ids["Layla Haddad"]
    email = await candidate_email(owner_engine, world, app_id)

    data = await api.call("GET", f"/v1/hire/scheduling/applications/{app_id}", world, "recruiter")
    assert data.status_code == 200, data.text
    body = data.json()
    assert body["default_panel"] == [uid(world, "recruiter")]
    assert body["identity_hidden"] is True  # blind mode is on by default
    assert body["label"].startswith("Candidate ")
    assert {m["id"] for m in body["team"]} >= {uid(world, "recruiter"), uid(world, "interviewer")}

    panel = [uid(world, "recruiter"), uid(world, "interviewer")]
    found = await slots(api, world, app_id, panel, candidate_timezone="America/New_York")
    assert found["timezone"] == "America/New_York"
    assert found["slots"]
    first, second = found["slots"][0]["start"], found["slots"][-1]["start"]
    # Every suggested slot is free for each panelist.
    for slot in found["slots"]:
        checked = await api.call(
            "POST",
            f"/v1/hire/scheduling/applications/{app_id}/check",
            world,
            "recruiter",
            json={"panel": panel, "starts_at": slot["start"], "duration_min": 45},
        )
        assert checked.json() == {"ok": True, "conflicts": []}

    booked = await book(api, world, app_id, panel, first, candidate_timezone="America/New_York")
    assert booked.status_code == 201, booked.text
    interview = booked.json()
    assert interview["status"] == "booked"
    assert interview["ics_sequence"] == 0
    assert interview["meeting_url"].startswith("https://meet.pairly.example/")
    assert interview["calendar_provider"] == "fake"
    # A second open interview for the same candidate is refused: move this one.
    again = await book(api, world, app_id, panel, second)
    assert again.status_code == 409

    outbox = tmp_path / "outbox"
    mails = await deliver(api, world, outbox)
    (confirmed,) = to(mails, email)
    assert "Your interview is booked" in confirmed["subject"]
    assert "(America/New_York)" in confirmed["text"]
    assert "—" not in confirmed["text"]
    cal = ics_of(confirmed)
    assert cal.calendar["METHOD"] == ["REQUEST"]
    (event,) = cal.events
    assert event["SEQUENCE"] == ["0"]
    uid_value = event["UID"][0]
    assert uid_value == f"iv-{uuid.UUID(interview['id']).hex}@pairly"
    briefings = [m for m in mails if m["subject"].startswith("Interview: Candidate ")]
    assert len(briefings) == 2
    assert ics_of(briefings[0]).events[0]["UID"] == [uid_value]

    # Move it (the drag in the panel): sequence 1, both sides told.
    moved = await api.call(
        "PATCH",
        f"/v1/hire/scheduling/interviews/{interview['id']}",
        world,
        "recruiter",
        json={"starts_at": second},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["status"] == "rescheduled"
    assert moved.json()["ics_sequence"] == 1
    mails = await deliver(api, world, outbox)
    (reschedule,) = [m for m in to(mails, email) if m["subject"].startswith("New time")]
    cal = ics_of(reschedule)
    assert cal.calendar["METHOD"] == ["REQUEST"]
    assert cal.events[0]["SEQUENCE"] == ["1"]
    assert cal.events[0]["UID"] == [uid_value]
    assert len([m for m in mails if m["subject"].startswith("New time: interview")]) == 2

    # Cancel: METHOD:CANCEL, sequence 2, to both sides.
    cancelled = await api.client.post(
        f"/v1/hire/scheduling/interviews/{interview['id']}/cancel",
        headers=api.headers(world, "recruiter", extra=key()),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    mails = await deliver(api, world, outbox)
    (gone,) = [m for m in to(mails, email) if "cancelled" in m["subject"]]
    cal = ics_of(gone)
    assert cal.calendar["METHOD"] == ["CANCEL"]
    assert cal.events[0]["SEQUENCE"] == ["2"]
    assert cal.events[0]["STATUS"] == ["CANCELLED"]
    assert len([m for m in mails if m["subject"].startswith("Cancelled: interview")]) == 2

    # Everything is audited.
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT action FROM audit_events WHERE entity_id = :i ORDER BY seq",
        {"i": interview["id"]},
    )
    actions = [r.action for r in rows]
    for action in (
        "hire.interview.booked",
        "hire.interview.rescheduled",
        "hire.interview.cancelled",
    ):
        assert action in actions
    # The interviewer cannot book; an outsider cannot read.
    denied = await api.client.post(
        f"/v1/hire/scheduling/applications/{app_id}/interviews",
        json={"mode": "link", "panel": panel, "duration_min": 45},
        headers=api.headers(world, "interviewer", extra=key()),
    )
    assert denied.status_code == 403
    assert (
        await api.call("GET", f"/v1/hire/scheduling/applications/{app_id}", world, "employee")
    ).status_code == 403


async def test_a_conflict_is_refused_unless_forced(hire_api: HireApi, world: HireWorld) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Omar One", "Omar Two"])
    panel = [uid(world, "interviewer")]
    found = await slots(api, world, ids["Omar One"], panel)
    start = found["slots"][0]["start"]
    assert (await book(api, world, ids["Omar One"], panel, start)).status_code == 201
    clash = await api.call(
        "POST",
        f"/v1/hire/scheduling/applications/{ids['Omar Two']}/check",
        world,
        "recruiter",
        json={"panel": panel, "starts_at": start, "duration_min": 45},
    )
    assert clash.json()["ok"] is False
    assert clash.json()["conflicts"][0]["reason"] == "busy"
    refused = await book(api, world, ids["Omar Two"], panel, start)
    assert refused.status_code == 409
    assert refused.json()["code"] == "slot_taken"
    forced = await book(api, world, ids["Omar Two"], panel, start, force=True)
    assert forced.status_code == 201, forced.text


# ------------------------------------------------------------------ self-scheduling


async def test_self_scheduling_link_books_once_in_the_candidates_zone(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Sara Link"])
    app_id = ids["Sara Link"]
    email = await candidate_email(owner_engine, world, app_id)
    panel = [uid(world, "recruiter")]
    interview = await send_link(api, world, app_id, panel)
    assert interview["status"] == "proposed"
    assert interview["link_active"] is True
    mails = await deliver(api, world, tmp_path / "o")
    (invite,) = to(mails, email)
    assert "Choose a time that suits you" in invite["text"]
    assert "attachments" not in invite  # no time yet, no calendar file
    token = TOKEN_RE.search(invite["text"]).group(1)  # type: ignore[union-attr]
    claim = read_schedule_token(LINK_SECRET, token)
    assert claim is not None
    assert str(claim.application_id) == app_id

    public = {"X-Pairly-Client-IP": "203.0.113.9"}
    read = await api.client.post(
        "/v1/hire/public/scheduling/read",
        json={"token": token, "timezone": "Europe/London"},
        headers=public,
    )
    assert read.status_code == 200, read.text
    page = read.json()
    assert page["state"] == "choose"
    assert page["timezone"] == "Europe/London"
    assert page["organisation"] == world.name
    chosen = page["slots"][0]
    start = datetime.fromisoformat(chosen["start"])
    assert start - datetime.now(UTC) >= timedelta(hours=12)  # minimum notice
    # The local date and time are in the candidate's zone.
    from zoneinfo import ZoneInfo

    local = start.astimezone(ZoneInfo("Europe/London"))
    assert (chosen["date"], chosen["time"]) == (local.date().isoformat(), f"{local:%H:%M}")

    booked = await api.client.post(
        "/v1/hire/public/scheduling/book",
        json={"token": token, "starts_at": chosen["start"], "timezone": "Europe/London"},
        headers=public,
    )
    assert booked.status_code == 200, booked.text
    assert booked.json()["state"] == "booked"
    assert booked.json()["meeting_url"]
    # Booking moved the application to the interview stage (a system move).
    profile = await api.call("GET", f"/v1/hire/pipeline/applications/{app_id}", world, "recruiter")
    assert profile.json()["stage_key"] == "interview"
    next_token = booked.json()["token"]
    assert next_token
    assert next_token != token
    # The used link never books again; it reads as used.
    used = await api.client.post(
        "/v1/hire/public/scheduling/book",
        json={"token": token, "starts_at": page["slots"][1]["start"]},
        headers=public,
    )
    assert used.status_code == 410
    assert used.json()["code"] == "link_used"
    mails = await deliver(api, world, tmp_path / "o")
    confirmed = [m for m in to(mails, email) if "booked" in m["subject"]]
    assert len(confirmed) == 1
    assert "(Europe/London)" in confirmed[0]["text"]
    assert ics_of(confirmed[0]).calendar["METHOD"] == ["REQUEST"]
    manage = TOKEN_RE.search(confirmed[0]["text"]).group(1)  # type: ignore[union-attr]
    assert manage == next_token

    # Forged and tampered links fail without saying why.
    for bad in (next_token[:-2] + "xx", next_token.replace("hs1.", "hs2."), "hs1." + "a" * 40):
        r = await api.client.post(
            "/v1/hire/public/scheduling/read", json={"token": bad}, headers=public
        )
        assert r.status_code == 404
    # A link past its expiry reads as nothing.
    assert read_schedule_token(LINK_SECRET, next_token, now=start.timestamp() + 1) is None

    # The candidate cancels with the newest link: METHOD:CANCEL to them and the panel.
    cancelled = await api.client.post(
        "/v1/hire/public/scheduling/cancel", json={"token": next_token}, headers=public
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    mails = await deliver(api, world, tmp_path / "o")
    gone = [m for m in to(mails, email) if "cancelled" in m["subject"]]
    assert ics_of(gone[0]).calendar["METHOD"] == ["CANCEL"]
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT actor_id, payload->>'by' AS by FROM audit_events "
        "WHERE action = 'hire.interview.cancelled' AND entity_id = :i",
        {"i": interview["id"]},
    )
    assert rows[0].by == "candidate"


async def test_no_double_booking_under_concurrency(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Race One", "Race Two"])
    panel = [uid(world, "recruiter"), uid(world, "interviewer")]
    first = await send_link(api, world, ids["Race One"], panel)
    second = await send_link(api, world, ids["Race Two"], panel)
    mails = await deliver(api, world, tmp_path / "o")
    tokens = {}
    for name in ("Race One", "Race Two"):
        email = await candidate_email(owner_engine, world, ids[name])
        (mail,) = to(mails, email)
        tokens[name] = TOKEN_RE.search(mail["text"]).group(1)  # type: ignore[union-attr]
    public = {"X-Pairly-Client-IP": "203.0.113.10"}
    page = (
        await api.client.post(
            "/v1/hire/public/scheduling/read", json={"token": tokens["Race One"]}, headers=public
        )
    ).json()
    start = page["slots"][0]["start"]

    async def take(token: str) -> httpx.Response:
        return await api.client.post(
            "/v1/hire/public/scheduling/book",
            json={"token": token, "starts_at": start},
            headers=public,
        )

    results = await asyncio.gather(take(tokens["Race One"]), take(tokens["Race Two"]))
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [r.text for r in results]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["code"] == "slot_taken"
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT id FROM hire_interviews WHERE starts_at = :s AND status = 'booked'",
        {"s": datetime.fromisoformat(start)},
    )
    assert len(rows) == 1
    assert str(rows[0].id) in {first["id"], second["id"]}

    # One link used twice at the same moment books once.
    winner_name = "Race One" if str(rows[0].id) == first["id"] else "Race Two"
    other_name = "Race Two" if winner_name == "Race One" else "Race One"
    page = (
        await api.client.post(
            "/v1/hire/public/scheduling/read", json={"token": tokens[other_name]}, headers=public
        )
    ).json()
    later = [s["start"] for s in page["slots"] if s["start"] != start][:2]

    async def take_at(at: str) -> httpx.Response:
        return await api.client.post(
            "/v1/hire/public/scheduling/book",
            json={"token": tokens[other_name], "starts_at": at},
            headers=public,
        )

    twice = await asyncio.gather(take_at(later[0]), take_at(later[1]))
    assert sorted(r.status_code for r in twice) == [200, 410], [r.text for r in twice]


# ------------------------------------------------------------------ reminders


async def test_reminders_24_hours_and_1_hour_before_once_each(
    hire_api: HireApi, world: HireWorld
) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Remi Nder"])
    panel = [uid(world, "interviewer")]
    start = (datetime.now(UTC) + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    booked = await book(api, world, ids["Remi Nder"], panel, start.isoformat(), force=True)
    assert booked.status_code == 201, booked.text
    env = SchedulingEnv.from_settings(notify_settings())
    maker = create_sessionmaker(api.engine)

    async def run(at: datetime) -> dict[str, int]:
        session = maker()
        set_tenant(session, world.org_id, None)
        async with session, session.begin():
            return await send_due_reminders(session, env, now=at)

    assert await run(start - timedelta(hours=30)) == {"day": 0, "hour": 0}
    assert await run(start - timedelta(hours=23)) == {"day": 1, "hour": 0}
    assert await run(start - timedelta(hours=22)) == {"day": 0, "hour": 0}
    assert await run(start - timedelta(minutes=50)) == {"day": 0, "hour": 1}
    assert await run(start - timedelta(minutes=10)) == {"day": 0, "hour": 0}
    session = maker()
    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        rows = (
            await session.execute(
                text(
                    "SELECT template_key, channel, urgency FROM notifications "
                    "WHERE dedupe_key LIKE :k ORDER BY created_at"
                ),
                {"k": f"hire.iv:{booked.json()['id']}:%:remind:%"},
            )
        ).all()
    keys = [(r.template_key, r.channel) for r in rows]
    assert keys.count(("hire.candidate.interview_reminder", "email")) == 2
    assert keys.count(("hire.interviewer.reminder", "email")) == 2
    assert keys.count(("hire.interviewer.reminder", "in_app")) == 2
    assert any(r.urgency == "urgent" for r in rows)


# ------------------------------------------------------------------ blind mode


async def test_briefing_respects_blind_mode(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Nadia Hidden", "Rami Shown"])
    email = await candidate_email(owner_engine, world, ids["Nadia Hidden"])
    panel = [uid(world, "interviewer")]
    found = await slots(api, world, ids["Nadia Hidden"], panel)
    assert (
        await book(api, world, ids["Nadia Hidden"], panel, found["slots"][0]["start"])
    ).status_code == 201
    # Rami reaches the shortlist, which reveals identity.
    moved = await api.client.post(
        f"/v1/hire/pipeline/applications/{ids['Rami Shown']}/move",
        json={"to_stage": "shortlist"},
        headers=api.headers(world, "recruiter", extra=key()),
    )
    assert moved.status_code == 200, moved.text
    found = await slots(api, world, ids["Rami Shown"], panel)
    assert (
        await book(api, world, ids["Rami Shown"], panel, found["slots"][-1]["start"])
    ).status_code == 201
    mails = await deliver(api, world, tmp_path / "o")
    briefings = [m for m in mails if m["subject"].startswith("Interview: ")]
    hidden = next(m for m in briefings if "Candidate " in m["subject"])
    shown = next(m for m in briefings if "Rami Shown" in m["subject"])
    assert "Nadia" not in hidden["text"]
    assert "Blind review" in hidden["text"]
    assert email not in hidden["attachments"][0]["text"]  # no candidate address
    assert "Nadia" not in hidden["attachments"][0]["text"]
    assert "Blind review" not in shown["text"]
    assert "Interview kit" in hidden["text"]


# ------------------------------------------------------------------ outcomes, availability


async def test_no_show_availability_and_settings(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Absent Person"])
    panel = [uid(world, "interviewer")]
    future = await book(
        api,
        world,
        ids["Absent Person"],
        panel,
        (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        force=True,
    )
    interview = future.json()
    early = await api.call(
        "POST",
        f"/v1/hire/scheduling/interviews/{interview['id']}/outcome",
        world,
        "interviewer",
        json={"outcome": "no_show"},
    )
    assert early.status_code == 409  # not started yet
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE hire_interviews SET starts_at = now() - interval '2 hours', "
        "ends_at = now() - interval '75 minutes' WHERE id = :i",
        {"i": interview["id"]},
    )
    # The panelist records it (no scheduling permission needed for their own interview).
    done = await api.call(
        "POST",
        f"/v1/hire/scheduling/interviews/{interview['id']}/outcome",
        world,
        "interviewer",
        json={"outcome": "no_show"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "no_show"
    outsider = await api.call(
        "POST",
        f"/v1/hire/scheduling/interviews/{interview['id']}/outcome",
        world,
        "auditor",
        json={"outcome": "completed"},
    )
    assert outsider.status_code == 403

    mine = await api.call("GET", "/v1/hire/scheduling/availability/me", world, "interviewer")
    assert mine.json()["saved"] is False
    assert mine.json()["timezone"] == "Asia/Dubai"
    saved = await api.call(
        "PUT",
        "/v1/hire/scheduling/availability/me",
        world,
        "interviewer",
        json={
            "timezone": "Europe/London",
            "working_hours": {"mon": [["10:00", "12:00"], ["13:00", "16:00"]]},
            "buffer_before_min": 10,
            "buffer_after_min": 10,
            "max_per_day": 2,
            "holidays": ["2026-12-25"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["working_hours"] == {"mon": [["10:00", "12:00"], ["13:00", "16:00"]]}
    bad = await api.call(
        "PUT",
        "/v1/hire/scheduling/availability/me",
        world,
        "interviewer",
        json={
            "timezone": "Mars/Base",
            "working_hours": {},
            "buffer_before_min": 0,
            "buffer_after_min": 0,
        },
    )
    assert bad.status_code == 400
    # Slots now follow the new rules: Mondays only, London time.
    found = await slots(api, world, ids["Absent Person"], panel)
    from zoneinfo import ZoneInfo

    for s in found["slots"]:
        local = datetime.fromisoformat(s["start"]).astimezone(ZoneInfo("Europe/London"))
        assert local.weekday() == 0
        assert 10 <= local.hour < 16
    # Someone else's availability: schedulers only.
    other = await api.call(
        "PUT",
        f"/v1/hire/scheduling/availability/{uid(world, 'recruiter')}",
        world,
        "interviewer",
        json=saved.json() | {"user_id": None, "name": None, "saved": None},
    )
    assert other.status_code in (403, 422)

    settings = await api.call(
        "PUT", "/v1/hire/settings/scheduling", world, "admin", json={"duration_min": 60}
    )
    assert settings.status_code == 200, settings.text
    assert settings.json()["duration_min"] == 60
    assert (
        await api.call(
            "PUT", "/v1/hire/settings/scheduling", world, "recruiter", json={"duration_min": 30}
        )
    ).status_code == 403


# ------------------------------------------------------------------ Google (mocked)


class _MockGoogle:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "ya29.test",
                    "refresh_token": "1//test-refresh",
                    "expires_in": 3600,
                    "scope": "calendar.freebusy calendar.events",
                },
            )
        if request.url.path.endswith("/freeBusy"):
            email = json.loads(request.content)["items"][0]["id"]
            return httpx.Response(200, json={"calendars": {email: {"busy": []}}})
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200, json={"id": "evt1", "hangoutLink": "https://meet.google.com/aaa-bbbb-ccc"}
            )
        return httpx.Response(404)


async def test_google_connection_with_a_mocked_google(
    hire_api: HireApi, world: HireWorld, monkeypatch: pytest.MonkeyPatch, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    status = await api.call("GET", "/v1/hire/scheduling/integrations", world, "interviewer")
    assert status.status_code == 200
    google = status.json()["providers"][0]
    assert google["availability"] == "needs_setup"
    assert google["connected"] is False
    assert status.json()["using"] == "fake"
    assert {p["provider"]: p["availability"] for p in status.json()["providers"]} == {
        "google": "needs_setup",
        "microsoft": "needs_setup",
        "zoom": "planned",
    }
    # Flag off: no connect.
    start = await api.call(
        "POST", "/v1/hire/scheduling/integrations/google/start", world, "interviewer"
    )
    assert start.status_code == 409

    mock = _MockGoogle()
    providers.set_google_transport(httpx.MockTransport(mock))
    monkeypatch.setenv("PAIRLY_GOOGLE_CLIENT_ID", "client.apps.example")
    monkeypatch.setenv("PAIRLY_GOOGLE_CLIENT_SECRET", "not-a-real-secret")
    monkeypatch.setenv(
        "PAIRLY_GOOGLE_REDIRECT_URL", "https://app.pairly.example/en/settings/calendar"
    )
    flag = await api.call(
        "PUT", "/v1/flags/hire.scheduling.google", world, "admin", json={"enabled": True}
    )
    assert flag.status_code == 200, flag.text
    start = await api.call(
        "POST", "/v1/hire/scheduling/integrations/google/start", world, "interviewer"
    )
    assert start.status_code == 200, start.text
    url = start.json()["url"]
    assert url.startswith("https://accounts.google.com/")
    state = re.search(r"state=([^&]+)", url).group(1)  # type: ignore[union-attr]
    # Another person cannot finish this person's connection.
    stolen = await api.call(
        "POST",
        "/v1/hire/scheduling/integrations/google/callback",
        world,
        "recruiter",
        json={"code": "4/abc", "state": state},
    )
    assert stolen.status_code == 400
    done = await api.call(
        "POST",
        "/v1/hire/scheduling/integrations/google/callback",
        world,
        "interviewer",
        json={"code": "4/abc", "state": state},
    )
    assert done.status_code == 200, done.text
    assert done.json()["using"] == "google"
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT refresh_token FROM hire_calendar_connections WHERE provider = 'google'",
    )
    assert "1//test-refresh" not in rows[0].refresh_token  # encrypted

    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Goo Gle"])
    found = await slots(api, world, ids["Goo Gle"], [uid(world, "interviewer")])
    assert found["slots"]
    assert any(c.url.path.endswith("/freeBusy") for c in mock.calls)
    assert all(c.url.host in {"oauth2.googleapis.com", "www.googleapis.com"} for c in mock.calls)

    off = await api.call(
        "POST", "/v1/hire/scheduling/integrations/google/disconnect", world, "interviewer"
    )
    assert off.json()["using"] == "fake"


# ------------------------------------------------------------------ portal interface


async def test_portal_lists_a_candidates_upcoming_interviews(
    hire_api: HireApi, world: HireWorld
) -> None:
    from pairly_hire.scheduling.api import upcoming_interviews_for_candidate

    api = hire_api
    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Port Al"])
    panel = [uid(world, "interviewer")]
    found = await slots(api, world, ids["Port Al"], panel)
    booked = await book(api, world, ids["Port Al"], panel, found["slots"][0]["start"])
    assert booked.status_code == 201
    session = create_sessionmaker(api.engine)()
    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        candidate_id = (
            await session.execute(
                text("SELECT candidate_id FROM hire_applications WHERE id = :a"),
                {"a": ids["Port Al"]},
            )
        ).scalar_one()
        items = await upcoming_interviews_for_candidate(
            session, SchedulingEnv.from_settings(notify_settings()), candidate_id
        )
    (item,) = items
    assert item.status == "booked"
    assert item.meeting_url
    assert item.schedule_url
    assert "/schedule#hs1." in item.schedule_url


# ------------------------------------------------------------------ tenant isolation


async def test_tenant_isolation_of_every_scheduling_table(
    hire_api: HireApi,
    world: HireWorld,
    other: HireWorld,
    owner_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Iso Late", "Not Booked"])
    panel = [uid(world, "recruiter")]
    found = await slots(api, world, ids["Iso Late"], panel)
    booked = await book(api, world, ids["Iso Late"], panel, found["slots"][0]["start"])
    assert booked.status_code == 201
    await api.call("PUT", "/v1/hire/settings/scheduling", world, "admin", json={"link_days": 5})
    await api.call(
        "PUT",
        "/v1/hire/scheduling/availability/me",
        world,
        "recruiter",
        json={
            "timezone": "Asia/Dubai",
            "working_hours": {"mon": [["09:00", "17:00"]]},
            "buffer_before_min": 0,
            "buffer_after_min": 0,
        },
    )
    providers.set_google_transport(httpx.MockTransport(_MockGoogle()))
    monkeypatch.setenv("PAIRLY_GOOGLE_CLIENT_ID", "client.apps.example")
    monkeypatch.setenv("PAIRLY_GOOGLE_CLIENT_SECRET", "not-a-real-secret")
    monkeypatch.setenv("PAIRLY_GOOGLE_REDIRECT_URL", "https://app.pairly.example/cb")
    await api.call(
        "PUT", "/v1/flags/hire.scheduling.google", world, "admin", json={"enabled": True}
    )
    start = await api.call("POST", "/v1/hire/scheduling/integrations/google/start", world, "admin")
    state = re.search(r"state=([^&]+)", start.json()["url"]).group(1)  # type: ignore[union-attr]
    connected = await api.call(
        "POST",
        "/v1/hire/scheduling/integrations/google/callback",
        world,
        "admin",
        json={"code": "4/x", "state": state},
    )
    assert connected.status_code == 200, connected.text

    for table in NEW_TABLES:
        mine = await owner_rows(
            owner_engine,
            world.org_id,
            f"SELECT count(*) AS n FROM {table} WHERE org_id = :o",  # noqa: S608 (fixed names)
            {"o": world.org_id},
        )
        assert mine[0].n > 0, table

    maker = create_sessionmaker(api.engine)
    session = maker()
    set_tenant(session, other.org_id, None)
    async with session, session.begin():
        for table in NEW_TABLES:
            seen = await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            assert seen.scalar_one() == 0, table
    session = maker()
    set_tenant(session, other.org_id, None)
    with pytest.raises(Exception, match="row-level security"):
        async with session, session.begin():
            await session.execute(
                text("INSERT INTO hire_scheduling_settings (org_id) VALUES (:o)"),
                {"o": world.org_id},
            )
    interview_id = booked.json()["id"]
    assert (
        await api.call("GET", f"/v1/hire/scheduling/interviews/{interview_id}", other, "recruiter")
    ).status_code == 404
    assert (
        await api.call(
            "GET", f"/v1/hire/scheduling/applications/{ids['Iso Late']}", other, "recruiter"
        )
    ).status_code == 404
    # No DELETE for the application role.
    session = maker()
    set_tenant(session, world.org_id, None)
    with pytest.raises(Exception, match="permission denied"):
        async with session, session.begin():
            await session.execute(text("DELETE FROM hire_interviews"))

    # The portal's restrictive layer (hire_0004b, extended by hire_0005): a candidate's
    # portal session sees only their own interview and its panel rows, and nothing of
    # the team's settings, calendars or connections; another candidate sees nothing.
    session = maker()
    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        candidates = {
            name: (
                await session.execute(
                    text("SELECT candidate_id FROM hire_applications WHERE id = :a"),
                    {"a": ids[name]},
                )
            ).scalar_one()
            for name in ("Iso Late", "Not Booked")
        }
    expected = {
        "Iso Late": {"hire_interviews": 1, "hire_interview_panelists": 1},
        "Not Booked": {"hire_interviews": 0, "hire_interview_panelists": 0},
    }
    for name, candidate_id in candidates.items():
        session = maker()
        set_tenant(session, world.org_id, None)
        set_portal_subject(session, portal_subject(candidate_id))
        async with session, session.begin():
            for table in NEW_TABLES:
                seen = await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
                assert seen.scalar_one() == expected[name].get(table, 0), (name, table)
    session = maker()
    set_tenant(session, world.org_id, None)
    set_portal_subject(session, portal_subject(candidates["Iso Late"]))
    with pytest.raises(Exception, match="row-level security"):
        async with session, session.begin():
            await session.execute(
                text("INSERT INTO hire_scheduling_settings (org_id) VALUES (:o)"),
                {"o": world.org_id},
            )


# ------------------------------------------------------------------ profile timeline


async def test_interviews_on_the_profile_timeline_blind_safe(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Layla Timeline", "Karim Done"])
    panel = [uid(world, "interviewer")]
    found = await slots(api, world, ids["Layla Timeline"], panel)
    first, second = found["slots"][0]["start"], found["slots"][1]["start"]
    booked = await book(api, world, ids["Layla Timeline"], panel, first)
    assert booked.status_code == 201, booked.text
    interview = booked.json()
    moved = await api.call(
        "PATCH",
        f"/v1/hire/scheduling/interviews/{interview['id']}",
        world,
        "recruiter",
        json={"starts_at": second},
    )
    assert moved.status_code == 200, moved.text
    cancelled = await api.client.post(
        f"/v1/hire/scheduling/interviews/{interview['id']}/cancel",
        headers=api.headers(world, "recruiter", extra=key()),
    )
    assert cancelled.status_code == 200, cancelled.text

    profile = await api.call(
        "GET", f"/v1/hire/pipeline/applications/{ids['Layla Timeline']}", world, "recruiter"
    )
    assert profile.status_code == 200, profile.text
    body = profile.json()
    # Booking moved the application to the interview stage, which reveals identity; the
    # interview events themselves never carry the candidate's details.
    assert body["stage_key"] == "interview"
    assert body["identity_hidden"] is False
    events = [e for e in body["timeline"] if e["kind"] == "interview"]
    assert [e["detail"]["status"] for e in events] == ["cancelled", "rescheduled", "booked"]
    assert all(e["detail"]["interview_id"] == interview["id"] for e in events)
    assert all(e["detail"]["by_candidate"] is False for e in events)
    assert all(e["detail"]["by"] for e in events)
    rescheduled = events[1]["detail"]
    assert datetime.fromisoformat(rescheduled["starts_at"]) == datetime.fromisoformat(second)
    assert datetime.fromisoformat(rescheduled["previous_starts_at"]) == datetime.fromisoformat(
        first
    )
    # Blind: nothing in the interview events names the candidate, and no meeting link.
    dumped = json.dumps(events)
    assert "Layla" not in dumped
    assert "https://" not in dumped
    assert "@" not in dumped

    # An outcome (completed) shows up too.
    other = await book(
        api,
        world,
        ids["Karim Done"],
        panel,
        (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        force=True,
    )
    assert other.status_code == 201, other.text
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE hire_interviews SET starts_at = now() - interval '2 hours', "
        "ends_at = now() - interval '75 minutes' WHERE id = :i",
        {"i": other.json()["id"]},
    )
    done = await api.call(
        "POST",
        f"/v1/hire/scheduling/interviews/{other.json()['id']}/outcome",
        world,
        "interviewer",
        json={"outcome": "completed"},
    )
    assert done.status_code == 200, done.text
    profile = await api.call(
        "GET", f"/v1/hire/pipeline/applications/{ids['Karim Done']}", world, "recruiter"
    )
    kinds = [e["detail"]["status"] for e in profile.json()["timeline"] if e["kind"] == "interview"]
    assert kinds == ["completed", "booked"]
    # Layla's events never leak into Karim's profile.
    assert interview["id"] not in json.dumps(profile.json()["timeline"])


# ------------------------------------------------------------------ booking moves the stage


async def _stage(api: HireApi, world: HireWorld, app_id: str) -> str:
    profile = await api.call("GET", f"/v1/hire/pipeline/applications/{app_id}", world, "recruiter")
    assert profile.status_code == 200, profile.text
    return str(profile.json()["stage_key"])


async def _move(api: HireApi, world: HireWorld, app_id: str, stage: str) -> None:
    moved = await api.client.post(
        f"/v1/hire/pipeline/applications/{app_id}/move",
        json={"to_stage": stage},
        headers=api.headers(world, "recruiter", extra=key()),
    )
    assert moved.status_code == 200, moved.text


async def test_booking_moves_the_application_to_interview_never_backwards(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await applicants(api, world, job, ["Ali Applied", "Omar Offer", "Lina Link"])
    panel = [uid(world, "interviewer")]

    # The team books an applied candidate: a system move to interview, audited.
    app_id = ids["Ali Applied"]
    found = await slots(api, world, app_id, panel)
    assert (await book(api, world, app_id, panel, found["slots"][0]["start"])).status_code == 201
    assert await _stage(api, world, app_id) == "interview"
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT from_stage, to_stage, actor_type, actor_id, reason IS NOT NULL AS has_reason "
        "FROM hire_stage_moves WHERE application_id = :a ORDER BY created_at",
        {"a": app_id},
    )
    assert [(r.from_stage, r.to_stage, r.actor_type, r.actor_id, r.has_reason) for r in rows] == [
        ("applied", "interview", "system", "hire.scheduling", True)
    ]
    audit = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT actor_type, payload->>'cause' AS cause, payload->>'to' AS to_stage "
        "FROM audit_events WHERE action = 'hire.candidate.moved' AND entity_id = :a",
        {"a": app_id},
    )
    assert [(a.actor_type, a.cause, a.to_stage) for a in audit] == [
        ("system", "interview_booked", "interview")
    ]
    # The reason shows on the profile timeline.
    profile = await api.call("GET", f"/v1/hire/pipeline/applications/{app_id}", world, "recruiter")
    assert "interview booked" in profile.text

    # An application at offer stays at offer (never backwards).
    offer_id = ids["Omar Offer"]
    await _move(api, world, offer_id, "offer")
    found = await slots(api, world, offer_id, panel)
    assert (await book(api, world, offer_id, panel, found["slots"][-1]["start"])).status_code == 201
    assert await _stage(api, world, offer_id) == "offer"
    moves = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT count(*) AS n FROM hire_stage_moves "
        "WHERE application_id = :a AND actor_type = 'system'",
        {"a": offer_id},
    )
    assert moves[0].n == 0

    # A second call (another booking later) never moves it again.
    link_id = ids["Lina Link"]
    await _move(api, world, link_id, "shortlist")
    await send_link(api, world, link_id, panel)
    assert await _stage(api, world, link_id) == "shortlist"  # a link alone books nothing
    session = create_sessionmaker(api.engine)()
    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        from pairly_hire.pipeline.moves import move_for_interview

        app_uuid = uuid.UUID(link_id)
        first = await move_for_interview(session, app_uuid, interview_id=uuid.uuid4())
        again = await move_for_interview(session, app_uuid, interview_id=uuid.uuid4())
    assert first is not None
    assert (first.from_stage, first.to_stage) == ("shortlist", "interview")
    assert again is None  # already there: no second move
    assert await _stage(api, world, link_id) == "interview"
