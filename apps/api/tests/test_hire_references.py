# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire reference checks (M3) on Postgres, through the API.

* The referee's email carries a single use link bound to the organisation, the request
  (referee and application) and its row; it expires, and a new link replaces it.
* The link opens a short referee session: the form with clear context, progress saved,
  every required answer before submitting, final once submitted.
* Referee data is encrypted at rest; every step is audited.
* The hiring team reads the answers, the summary (recorded model response, else from
  the answers) and the consistency flags; blind safe until reveal.
* Permissions and tenant isolation.
"""

from __future__ import annotations

import importlib
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.llm.bundle import (
    BUNDLE_FORMAT,
    BUNDLE_VERSION,
    dumps_bundle,
    import_recordings,
    parse_bundle,
)
from pairly_hire.recordings import entry_for
from pairly_hire.references import summary as summaries

from .hire_support import HireApi, HireWorld, hire_api, open_job, owner_rows
from .test_hire_interview_kits import NAME, applicant, interview_world
from .test_hire_pipeline import move
from .test_hire_scheduling import deliver
from .test_hire_scorecards import keep_interviews_blind

__all__ = ["hire_api"]  # fixture

TOKEN_RE = re.compile(r"/reference#(rr1\.[A-Za-z0-9._-]+)")
SESSION = "X-Pairly-Reference-Session"
REFEREE = "referee.omar@example.test"
ANSWERS: dict[str, Any] = {
    "known_as": "manager",
    "company": "Gulf Logistics",
    "candidate_title": "Backend Engineer",
    "worked_from": "2021-03",
    "worked_to": "2023-08",
    "strengths": "Calm under pressure; led the payments retry redesign with clear trade offs.",
    "development": "Could delegate more on large projects.",
    "reliability": 5,
    "would_rehire": "yes",
}


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> HireWorld:
    return await interview_world(owner_engine)


@pytest.fixture
async def other(owner_engine: AsyncEngine) -> HireWorld:
    return await interview_world(owner_engine)


async def ask(
    api: HireApi, world: HireWorld, app_id: str, email: str = REFEREE, who: str = "recruiter"
) -> Any:
    return await api.call(
        "POST",
        f"/v1/hire/applications/{app_id}/references",
        world,
        who,
        json={"name": "Omar Haddad", "email": email, "relationship": "manager", "locale": "en"},
    )


def tokens(mails: list[dict[str, Any]], email: str = REFEREE) -> list[str]:
    found: list[str] = []
    for mail in mails:
        if any(email in to for to in mail["to"]):
            found += TOKEN_RE.findall(mail["text"])
    return found


async def open_link(api: HireApi, token: str) -> Any:
    return await api.client.post(
        "/v1/hire/public/references/open", json={"token": token}, headers=api.public()
    )


async def test_referee_link_progress_submit_and_the_teams_view(
    hire_api: HireApi, world: HireWorld, other: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    await keep_interviews_blind(api, world, job["id"])
    app_id = await applicant(api, world, job)

    # Only the people who decide on candidates ask referees.
    assert (await ask(api, world, app_id, who="interviewer")).status_code == 403
    asked = await ask(api, world, app_id)
    assert asked.status_code == 201, asked.text
    assert (await ask(api, world, app_id)).status_code == 409  # the same referee twice
    (ref,) = asked.json()["items"]
    assert ref["status"] == "requested"
    mails = await deliver(api, world, tmp_path)
    (mail,) = [m for m in mails if REFEREE in m["to"]]
    assert NAME in mail["text"]  # clear context: who the reference is about
    assert "—" not in mail["text"]
    (token,) = tokens(mails)

    # Encrypted at rest: the referee's name, address and answers never appear as text.
    (raw,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT referee_name, referee_email FROM hire_reference_requests WHERE id = :i",
        {"i": ref["id"]},
    )
    assert "Omar" not in raw.referee_name
    assert REFEREE not in raw.referee_email

    # Bound: another organisation, another request, a changed secret: not valid.
    parts = token.split(".")
    forged = [
        ".".join([parts[0], other.org_id.hex, *parts[2:]]),
        ".".join([*parts[:3], uuid.uuid4().hex, parts[4]]),
        ".".join([*parts[:4], ("A" if parts[4][0] != "A" else "B") + parts[4][1:]]),
        "rs1." + ".".join(parts[1:]),
    ]
    for bad in forged:
        assert (await open_link(api, bad)).status_code == 404, bad
    opened = await open_link(api, token)
    assert opened.status_code == 200, opened.text
    session_token = opened.json()["session_token"]
    assert session_token.startswith("rs1.")
    assert (await open_link(api, token)).status_code == 404  # single use
    headers = {SESSION: session_token}

    page = await api.client.get("/v1/hire/reference-session/me", headers=headers)
    assert page.status_code == 200, page.text
    assert page.headers["cache-control"] == "no-store"
    body = page.json()
    assert body["candidate_name"] == NAME
    assert body["job_title"] == job["title"]
    assert body["referee_name"] == "Omar Haddad"
    assert 3 <= len([q for q in body["questions"] if q["required"]]) <= 10
    arabic = await api.client.get("/v1/hire/reference-session/me?locale=ar", headers=headers)
    assert arabic.json()["questions"][0]["label"] != body["questions"][0]["label"]
    # A staff route never accepts the referee session.
    assert (await api.client.get("/v1/hire/requisitions", headers=headers)).status_code == 401

    # Progress is saved; a submission needs every required answer, valid values only.
    partial = {"known_as": "manager", "company": "Gulf Logistics"}
    saved = await api.client.put(
        "/v1/hire/reference-session/me", json={"answers": partial}, headers=headers
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["answers"] == partial
    again = await api.client.get("/v1/hire/reference-session/me", headers=headers)
    assert again.json()["answers"] == partial
    early = await api.client.post(
        "/v1/hire/reference-session/me/submit", json={"answers": partial}, headers=headers
    )
    assert early.status_code == 400
    for bad_answers in (
        {**ANSWERS, "reliability": 9},
        {**ANSWERS, "worked_from": "March 2021"},
        {**ANSWERS, "known_as": "cousin"},
        {**ANSWERS, "worked_to": "2019-01"},
        {**ANSWERS, "shoe_size": "42"},
    ):
        refused = await api.client.post(
            "/v1/hire/reference-session/me/submit", json={"answers": bad_answers}, headers=headers
        )
        assert refused.status_code in {400, 422}, bad_answers
    done = await api.client.post(
        "/v1/hire/reference-session/me/submit", json={"answers": ANSWERS}, headers=headers
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "submitted"
    # Final: the session ends, the request cannot be sent again.
    after = await api.client.get("/v1/hire/reference-session/me", headers=headers)
    assert after.status_code == 401
    resend = await api.call("POST", f"/v1/hire/references/{ref['id']}/send", world, "recruiter")
    assert resend.status_code == 409

    # The hiring team: blind (interviews are blind here), so no referee name or address.
    view = await api.call("GET", f"/v1/hire/applications/{app_id}/references", world, "manager")
    assert view.status_code == 200, view.text
    got = view.json()
    assert got["identity_hidden"] is True
    (item,) = got["items"]
    assert item["referee_name"] is None
    assert item["referee_email"] is None
    assert item["label"] == "Referee 1"
    assert item["answers"]["company"] == "Gulf Logistics"
    assert got["submitted"] == 1
    assert got["summary"]["source"] == "template"
    assert "Referee 1: yes" in got["summary"]["summary"]
    # No screening profile here: the CV comparison says so.
    assert [f["kind"] for f in got["flags"]] == ["no_cv_profile"]
    for who in ("interviewer", "auditor"):
        denied = await api.call("GET", f"/v1/hire/applications/{app_id}/references", world, who)
        assert denied.status_code == 403, who
    elsewhere = await api.call(
        "GET", f"/v1/hire/applications/{app_id}/references", other, "recruiter"
    )
    assert elsewhere.status_code == 404

    actions = [
        r.action
        for r in await owner_rows(
            owner_engine,
            world.org_id,
            "SELECT action FROM audit_events WHERE entity_id = :i ORDER BY seq",
            {"i": ref["id"]},
        )
    ]
    for action in (
        "hire.reference.requested",
        "hire.reference.sent",
        "hire.reference.link_used",
        "hire.reference.saved",
        "hire.reference.submitted",
    ):
        assert action in actions
    events = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT payload::text AS p FROM audit_events WHERE entity_id = :i",
        {"i": ref["id"]},
    )
    assert not any("Omar" in e.p or "Gulf Logistics" in e.p for e in events)


async def test_links_expire_a_new_link_replaces_the_old_and_cancel_ends_it(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id = await applicant(api, world, job)
    asked = await ask(api, world, app_id)
    ref_id = asked.json()["items"][0]["id"]
    (first,) = tokens(await deliver(api, world, tmp_path))
    resent = await api.call("POST", f"/v1/hire/references/{ref_id}/send", world, "recruiter")
    assert resent.status_code == 200, resent.text
    second = [t for t in tokens(await deliver(api, world, tmp_path)) if t != first]
    assert len(second) == 1
    assert (await open_link(api, first)).status_code == 404  # replaced

    session = create_sessionmaker(owner_engine)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            from sqlalchemy import text

            await session.execute(
                text(
                    "UPDATE hire_reference_links SET expires_at = now() - interval '1 minute' "
                    "WHERE request_id = :r"
                ),
                {"r": ref_id},
            )
    assert (await open_link(api, second[0])).status_code == 404  # expired

    third_send = await api.call("POST", f"/v1/hire/references/{ref_id}/send", world, "recruiter")
    assert third_send.status_code == 200
    (third,) = [t for t in tokens(await deliver(api, world, tmp_path)) if t not in {first, *second}]
    opened = await open_link(api, third)
    assert opened.status_code == 200
    headers = {SESSION: opened.json()["session_token"]}
    cancelled = await api.call("POST", f"/v1/hire/references/{ref_id}/cancel", world, "recruiter")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["items"][0]["status"] == "cancelled"
    gone = await api.client.get("/v1/hire/reference-session/me", headers=headers)
    assert gone.status_code == 401


async def test_summary_from_a_recorded_model_response_is_kept_and_revealed(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id = await applicant(api, world, job)
    await ask(api, world, app_id)
    (token,) = tokens(await deliver(api, world, tmp_path))
    opened = await open_link(api, token)
    headers = {SESSION: opened.json()["session_token"]}
    low = {**ANSWERS, "reliability": 2, "would_rehire": "no"}
    done = await api.client.post(
        "/v1/hire/reference-session/me/submit", json={"answers": low}, headers=headers
    )
    assert done.status_code == 200, done.text

    # Revealed (the default pipeline reveals at interview): the referee's name shows.
    moved = await move(api, world, app_id, "interview")
    assert moved.status_code == 200, moved.text
    revealed = (
        await api.call("GET", f"/v1/hire/applications/{app_id}/references", world, "recruiter")
    ).json()
    assert revealed["identity_hidden"] is False
    assert revealed["items"][0]["referee_name"] == "Omar Haddad"
    kinds = {f["kind"] for f in revealed["flags"]}
    assert {"would_not_rehire", "low_reliability"} <= kinds

    inputs = summaries.summary_inputs(
        job_title=job["title"], locale="en", references=[("Referee 1", low)]
    )
    answer = summaries.ReferenceDraft(
        summary="Referee 1 describes calm delivery but would not work with them again.",
        strengths=["Calm under pressure"],
        development=["Delegation"],
        inconsistencies=[],
    )
    bundle = dumps_bundle(
        {
            "format": BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "entries": [entry_for(summaries.TASK, summaries.ReferenceDraft, inputs, answer)],
        }
    )
    session = create_sessionmaker(owner_engine)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            await import_recordings(session, world.org_id, parse_bundle(bundle))
    path = f"/v1/hire/applications/{app_id}/references"
    got = (await api.call("GET", path, world, "manager")).json()
    assert got["summary"]["source"] == "model"
    assert got["summary"]["from_recording"] is True
    assert got["summary"]["summary"] == answer.summary
    again = (await api.call("GET", path, world, "manager")).json()
    assert again["summary"]["summary"] == answer.summary
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT content, source FROM hire_reference_summaries WHERE application_id = :a",
        {"a": app_id},
    )
    assert [r.source for r in rows] == ["model"]
    assert "calm delivery" not in rows[0].content  # encrypted at rest


async def test_the_demo_references_open_with_their_recorded_summary(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    """``make demo``: the seeded job and answered references, the committed recordings,
    and the summary the hiring team opens is the recorded one in both languages."""
    import json

    # Loaded by name: mypy checks scripts/ as top-level modules (one module, one name).
    demo_seed = importlib.import_module("scripts.demo_seed")
    seed_jobs, _demo_references = demo_seed.seed_jobs, demo_seed._demo_references
    from synth.hire.interviews import demo_rubric
    from synth.hire.references import recordings_text

    from .hire_support import cv_pdf

    api = hire_api
    lines = await seed_jobs(create_sessionmaker(owner_engine), world.org_id)
    assert any("recorded reference summaries: 2 imported" in line for line in lines)
    jobs = (await api.call("GET", "/v1/hire/jobs?status=open", world, "recruiter")).json()
    job = next(j for j in jobs["items"] if j["slug"] == "backend-engineer-payments")
    reference = await api.apply(
        world,
        job["slug"],
        cv_pdf("demo-references"),
        email="demo.references@example.test",
        full_name="Demo Candidate",
        knockouts={k.id: "yes" for k in demo_rubric().knockout_questions},
    )
    listed = await api.call("GET", f"/v1/hire/applications?job_id={job['id']}", world, "recruiter")
    app_id = next(a["id"] for a in listed.json()["items"] if a["reference"] == reference)
    assert (await move(api, world, app_id, "interview")).status_code == 200
    session = create_sessionmaker(owner_engine)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            added = await _demo_references(session, uuid.UUID(app_id), world.people["recruiter"][0])
            assert added == 2
            assert (
                await _demo_references(session, uuid.UUID(app_id), world.people["recruiter"][0])
                == 0
            )
    recorded = {e["response"]["summary"] for e in json.loads(recordings_text())["entries"]}
    for locale in ("en", "ar"):
        got = (
            await api.call(
                "GET",
                f"/v1/hire/applications/{app_id}/references?locale={locale}",
                world,
                "manager",
            )
        ).json()
        assert got["submitted"] == 2
        assert got["summary"]["source"] == "model", got["summary"]
        assert got["summary"]["from_recording"] is True
        assert got["summary"]["summary"] in recorded
