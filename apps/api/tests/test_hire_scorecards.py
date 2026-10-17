# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire independent scorecards (M3) on Postgres, through the API.

* The briefing carries each interviewer's own one-click link (email and calendar file):
  single use, bound to the organisation, the interview and the person; it opens a
  scorecard session for that interview only.
* Hidden until you submit, enforced by the API on every route; complete before submit;
  final once submitted (API and trigger).
* Feedback reminders 2 and 24 hours after the interview, "remind me later", once each.
* Permissions and tenant isolation.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker, set_tenant
from pairly_hire.interviewing.reminders import send_due_reminders
from pairly_hire.links import LinkConfig
from pairly_hire.scheduling.emails import SchedulingEnv

from .hire_support import HireApi, HireWorld, hire_api, open_job, owner_rows
from .notify_support import LINK_SECRET
from .test_hire_interview_kits import NAME, applicant, interview_world
from .test_hire_scheduling import book, deliver, slots

__all__ = ["hire_api"]  # fixture

TOKEN_RE = re.compile(r"/scorecard#((?:sk1|sl1)\.[A-Za-z0-9._-]+)")
SESSION = "X-Pairly-Scorecard-Session"


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> HireWorld:
    return await interview_world(owner_engine)


@pytest.fixture
async def other(owner_engine: AsyncEngine) -> HireWorld:
    return await interview_world(owner_engine)


def uid(world: HireWorld, who: str) -> str:
    return str(world.people[who][0])


def tokens_for(mails: list[dict[str, Any]], email_part: str, prefix: str = "sk1") -> list[str]:
    found: list[str] = []
    for mail in mails:
        if not any(email_part in to for to in mail["to"]):
            continue
        found += [t for t in TOKEN_RE.findall(mail["text"]) if t.startswith(prefix)]
    return found


async def keep_interviews_blind(api: HireApi, world: HireWorld, job_id: str) -> None:
    """A pipeline whose interview stage does not reveal identity (blind interviews)."""
    got = await api.call("GET", f"/v1/hire/jobs/{job_id}/stages", world, "recruiter")
    keys = ("key", "label", "label_ar", "kind", "reveal_identity")
    stages = [
        {k: s[k] for k in keys if k in s}
        | ({"reveal_identity": False} if s["key"] in {"shortlist", "interview"} else {})
        for s in got.json()["stages"]
    ]
    saved = await api.call("PUT", f"/v1/hire/jobs/{job_id}/stages", world, "recruiter", json=stages)
    assert saved.status_code == 200, saved.text


async def interview_with_panel(
    api: HireApi, world: HireWorld, tmp_path: Path, panel: list[str]
) -> tuple[str, str, list[dict[str, Any]]]:
    job = await open_job(api, world)
    await keep_interviews_blind(api, world, job["id"])
    app_id = await applicant(api, world, job)
    ids = [uid(world, p) for p in panel]
    found = await slots(api, world, app_id, ids)
    booked = await book(api, world, app_id, ids, found["slots"][0]["start"])
    assert booked.status_code == 201, booked.text
    return app_id, booked.json()["id"], await deliver(api, world, tmp_path)


async def open_link(api: HireApi, token: str) -> Any:
    return await api.client.post(
        "/v1/hire/public/scorecards/open", json={"token": token}, headers=api.public()
    )


def full(card: dict[str, Any], rating: int = 3, rec: str = "yes") -> dict[str, Any]:
    return {
        "ratings": {c["id"]: rating for c in card["criteria"]},
        "notes": f"Clear examples. {NAME} explained the migration well.",
        "recommendation": rec,
    }


async def test_magic_link_single_use_bound_and_hidden_until_submit(
    hire_api: HireApi, world: HireWorld, other: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    app_id, interview_id, mails = await interview_with_panel(
        api, world, tmp_path, ["interviewer", "manager"]
    )
    interviewer_email = (
        await owner_rows(
            owner_engine,
            world.org_id,
            "SELECT email FROM users WHERE id = :u",
            {"u": uid(world, "interviewer")},
        )
    )[0].email
    (token,) = tokens_for(mails, interviewer_email)
    # The calendar file carries the same link for that person.
    briefing = next(m for m in mails if interviewer_email in m["to"][0] and "panel" in m["text"])
    assert token in briefing["attachments"][0]["text"].replace("\r\n ", "")

    # Binding: another person's id, another organisation, a changed secret: not valid.
    parts = token.split(".")
    forged = [
        ".".join([parts[0], parts[1], parts[2], uuid.UUID(uid(world, "manager")).hex, parts[4]]),
        ".".join([parts[0], other.org_id.hex, *parts[2:]]),
        ".".join([*parts[:4], ("A" if parts[4][0] != "A" else "B") + parts[4][1:]]),
        "sl1." + ".".join(parts[1:]),
    ]
    for bad in forged:
        assert (await open_link(api, bad)).status_code == 404, bad

    opened = await open_link(api, token)
    assert opened.status_code == 200, opened.text
    session_token = opened.json()["session_token"]
    assert session_token.startswith("ss1.")
    # Single use.
    assert (await open_link(api, token)).status_code == 404

    me = await api.client.get("/v1/hire/scorecard-session/me", headers={SESSION: session_token})
    assert me.status_code == 200, me.text
    page = me.json()
    card = page["scorecard"]
    assert card["interview_id"] == interview_id
    assert 3 <= len(card["criteria"]) <= 5
    assert all(len(c["anchors"]) == 4 for c in card["criteria"])
    assert card["status"] == "draft"
    assert card["others"] is None
    assert card["candidate_label"].startswith("Candidate ")
    assert card["identity_hidden"] is True
    assert page["kit"] is not None
    assert page["kit"]["items"]
    # A session is never a staff credential, and a bad one is refused.
    assert (
        await api.client.get("/v1/hire/requisitions", headers={SESSION: session_token})
    ).status_code == 401
    assert (
        await api.client.get(
            "/v1/hire/scorecard-session/me", headers={SESSION: session_token[:-2] + "AA"}
        )
    ).status_code == 401

    # The manager submits first (signed in).
    mine = await api.call("GET", f"/v1/hire/interviews/{interview_id}/scorecard", world, "manager")
    assert mine.status_code == 200, mine.text
    manager_card = mine.json()["scorecard"]
    submitted = await api.call(
        "POST",
        f"/v1/hire/interviews/{interview_id}/scorecard/submit",
        world,
        "manager",
        json=full(manager_card, 2, "no"),
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["scorecard"]["others"] == []  # nobody else has submitted

    # Hidden until the interviewer submits: on every route.
    me = (
        await api.client.get("/v1/hire/scorecard-session/me", headers={SESSION: session_token})
    ).json()
    assert me["scorecard"]["others"] is None
    assert me["scorecard"]["others_submitted"] == 1
    listed = await api.call(
        "GET", f"/v1/hire/applications/{app_id}/scorecards", world, "interviewer"
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["hidden"] is True
    assert listed.json()["items"] == []
    staff_view = await api.call(
        "GET", f"/v1/hire/interviews/{interview_id}/scorecard", world, "interviewer"
    )
    assert staff_view.json()["scorecard"]["others"] is None

    # Incomplete submissions are refused; a draft saves.
    partial = full(card) | {"ratings": {card["criteria"][0]["id"]: 4}}
    assert (
        await api.client.post(
            "/v1/hire/scorecard-session/me/submit", json=partial, headers={SESSION: session_token}
        )
    ).status_code == 400
    no_rec = full(card) | {"recommendation": None}
    assert (
        await api.client.post(
            "/v1/hire/scorecard-session/me/submit", json=no_rec, headers={SESSION: session_token}
        )
    ).status_code == 400
    out_of_range = full(card, 5)
    assert (
        await api.client.post(
            "/v1/hire/scorecard-session/me/submit",
            json=out_of_range,
            headers={SESSION: session_token},
        )
    ).status_code == 400
    draft = await api.client.put(
        "/v1/hire/scorecard-session/me", json=partial, headers={SESSION: session_token}
    )
    assert draft.status_code == 200, draft.text
    assert draft.json()["scorecard"]["others"] is None

    done = await api.client.post(
        "/v1/hire/scorecard-session/me/submit",
        json=full(card, 4, "strong_yes"),
        headers={SESSION: session_token},
    )
    assert done.status_code == 200, done.text
    others = done.json()["scorecard"]["others"]
    assert [o["recommendation"] for o in others] == ["no"]
    # Blind: the other's notes reach the interviewer without the candidate's name.
    assert NAME not in others[0]["notes"]
    again = await api.client.post(
        "/v1/hire/scorecard-session/me/submit", json=full(card), headers={SESSION: session_token}
    )
    assert again.status_code == 409
    assert (
        await api.client.put(
            "/v1/hire/scorecard-session/me", json=full(card), headers={SESSION: session_token}
        )
    ).status_code == 409

    # The trigger refuses any change to a submitted scorecard, even as the owner.
    with pytest.raises(DBAPIError):
        await owner_rows(
            owner_engine,
            world.org_id,
            "UPDATE hire_scorecards SET recommendation = 'no' WHERE interview_id = :i "
            "AND interviewer_id = :u",
            {"i": interview_id, "u": uid(world, "interviewer")},
        )

    # The hiring team reads them; others may not; another organisation sees nothing.
    team = (
        await api.call("GET", f"/v1/hire/applications/{app_id}/scorecards", world, "recruiter")
    ).json()
    assert team["hidden"] is False
    assert len(team["items"]) == 2
    assert (
        await api.call("GET", f"/v1/hire/applications/{app_id}/scorecards", world, "employee")
    ).status_code == 403
    assert (
        await api.call("GET", f"/v1/hire/interviews/{interview_id}/scorecard", world, "recruiter")
    ).status_code == 404  # not on the panel
    assert (
        await api.call("GET", f"/v1/hire/interviews/{interview_id}/scorecard", other, "manager")
    ).status_code == 404
    assert (
        await api.call("GET", f"/v1/hire/applications/{app_id}/scorecards", other, "recruiter")
    ).status_code == 404


async def test_feedback_reminders_and_remind_me_later(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    _app_id, interview_id, _mails = await interview_with_panel(
        api, world, tmp_path, ["interviewer"]
    )
    end = datetime.now(UTC) - timedelta(minutes=30)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.org_id', :o, true)"), {"o": str(world.org_id)}
        )
        await conn.execute(
            text("UPDATE hire_interviews SET starts_at = :s, ends_at = :e WHERE id = :i"),
            {"s": end - timedelta(minutes=45), "e": end, "i": interview_id},
        )
    env = SchedulingEnv(LinkConfig(LINK_SECRET, "https://app.test.pairly"))
    (tmp_path / "r1").mkdir()
    (tmp_path / "r2").mkdir()
    sessionmaker = create_sessionmaker(api.engine)

    async def run(at: datetime) -> dict[str, int]:
        session = sessionmaker()
        set_tenant(session, world.org_id, None)
        async with session, session.begin():
            return await send_due_reminders(session, env, now=at)

    assert await run(end + timedelta(hours=1)) == {"2h": 0, "24h": 0, "later": 0}
    assert await run(end + timedelta(hours=2, minutes=1)) == {"2h": 1, "24h": 0, "later": 0}
    assert await run(end + timedelta(hours=2, minutes=6)) == {"2h": 0, "24h": 0, "later": 0}
    first = [
        m for m in await deliver(api, world, tmp_path / "r1") if "scorecard" in m["subject"].lower()
    ]
    assert len(first) == 1
    assert "due by" in first[0]["text"]
    later = [t for t in TOKEN_RE.findall(first[0]["text"]) if t.startswith("sl1")]
    assert len(later) == 1

    snoozed = await api.client.post(
        "/v1/hire/public/scorecards/later", json={"token": later[0]}, headers=api.public()
    )
    assert snoozed.status_code == 200, snoozed.text
    assert (
        await api.client.post(
            "/v1/hire/public/scorecards/later", json={"token": later[0]}, headers=api.public()
        )
    ).status_code == 404  # single use
    until = datetime.fromisoformat(snoozed.json()["snoozed_until"])
    assert await run(until - timedelta(minutes=5)) == {"2h": 0, "24h": 0, "later": 0}
    assert await run(until + timedelta(minutes=1)) == {"2h": 0, "24h": 0, "later": 1}
    assert await run(end + timedelta(hours=24, minutes=1)) == {"2h": 0, "24h": 1, "later": 0}
    assert await run(end + timedelta(hours=30)) == {"2h": 0, "24h": 0, "later": 0}
    rest = [
        m for m in await deliver(api, world, tmp_path / "r2") if "scorecard" in m["subject"].lower()
    ]
    assert len(rest) == 2
    assert rest[-1]["subject"].startswith("Overdue")

    # Submitted: no more reminders.
    (open_token,) = [t for t in TOKEN_RE.findall(rest[-1]["text"]) if t.startswith("sk1")]
    opened = await open_link(api, open_token)
    assert opened.status_code == 200, opened.text
    session_token = opened.json()["session_token"]
    card = (
        await api.client.get("/v1/hire/scorecard-session/me", headers={SESSION: session_token})
    ).json()
    assert card["scorecard"]["sla_state"] == "on_time"
    done = await api.client.post(
        "/v1/hire/scorecard-session/me/submit",
        json=full(card["scorecard"]),
        headers={SESSION: session_token},
    )
    assert done.status_code == 200, done.text
    actions = [
        r.action
        for r in await owner_rows(
            owner_engine,
            world.org_id,
            "SELECT action FROM audit_events WHERE action LIKE 'hire.scorecard.%' ORDER BY seq",
        )
    ]
    assert actions.count("hire.scorecard.reminded") == 3
    assert "hire.scorecard.snoozed" in actions
    assert "hire.scorecard.submitted" in actions
