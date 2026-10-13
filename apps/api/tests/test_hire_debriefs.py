# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire debrief summaries (M3) on Postgres, through the API.

* Waiting until every panelist has submitted, showing who is due and nothing else; a
  panelist who has not submitted never reads it.
* Ready: ratings per criterion with mean and spread, disagreements and open questions;
  from the model when it answers (a recorded response here), else from the numbers.
* Blind safe: while the candidate is blind, notes reach the debrief (and the model)
  without their name.
* The hiring manager decides with a reason (second factor): a decision record, once.
* Permissions and tenant isolation.
"""

from __future__ import annotations

import json
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
from pairly_hire.interviewing import debrief
from pairly_hire.recordings import entry_for

from .hire_support import HireApi, HireWorld, hire_api, owner_rows
from .test_hire_interview_kits import NAME, interview_world
from .test_hire_scorecards import interview_with_panel

__all__ = ["hire_api"]  # fixture


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> HireWorld:
    return await interview_world(owner_engine)


@pytest.fixture
async def other(owner_engine: AsyncEngine) -> HireWorld:
    return await interview_world(owner_engine)


async def submit(
    api: HireApi, world: HireWorld, who: str, interview_id: str, first: int, rest: int, rec: str
) -> dict[str, Any]:
    card = (
        await api.call("GET", f"/v1/hire/interviews/{interview_id}/scorecard", world, who)
    ).json()["scorecard"]
    ids = [c["id"] for c in card["criteria"]]
    body = {
        "ratings": {cid: (first if i == 0 else rest) for i, cid in enumerate(ids)},
        "notes": f"{NAME} gave one example about {ids[0]}.",
        "recommendation": rec,
    }
    done = await api.call(
        "POST", f"/v1/hire/interviews/{interview_id}/scorecard/submit", world, who, json=body
    )
    assert done.status_code == 200, done.text
    return dict(done.json()["scorecard"])


async def test_debrief_waits_then_summarises_blind_and_records_the_decision(
    hire_api: HireApi, world: HireWorld, other: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    app_id, interview_id, _ = await interview_with_panel(
        api, world, tmp_path, ["interviewer", "recruiter"]
    )
    path = f"/v1/hire/applications/{app_id}/debrief"

    waiting = await api.call("GET", path, world, "manager")
    assert waiting.status_code == 200, waiting.text
    body = waiting.json()
    assert body["status"] == "waiting"
    assert body["expected"] == 2
    assert body["submitted"] == 0
    assert len(body["waiting_for"]) == 2
    assert body["criteria"] == []
    assert body["voices"] == []
    assert body["can_decide"] is False
    # A panelist who has not submitted, and people without the permission, never read it.
    assert (await api.call("GET", path, world, "recruiter")).status_code == 403
    assert (await api.call("GET", path, world, "interviewer")).status_code == 403
    assert (await api.call("GET", path, world, "employee")).status_code == 403

    card = await submit(api, world, "interviewer", interview_id, 1, 3, "no")
    still = (await api.call("GET", path, world, "manager")).json()
    assert still["status"] == "waiting"
    assert still["submitted"] == 1
    assert still["voices"] == []  # nothing before everyone has submitted
    early = await api.call(
        "POST",
        f"{path}/decision",
        world,
        "manager",
        json={"decision": "advance", "reason": "Strong panel."},
    )
    assert early.status_code == 409

    await submit(api, world, "recruiter", interview_id, 4, 3, "yes")
    ready = await api.call("GET", path, world, "manager")
    assert ready.status_code == 200, ready.text
    body = ready.json()
    assert body["status"] == "ready"
    assert body["identity_hidden"] is True
    first = body["criteria"][0]
    assert first["id"] == card["criteria"][0]["id"]
    assert sorted(r["rating"] for r in first["ratings"]) == [1, 4]
    assert first["spread"] == 3
    assert first["disagreement"] is True
    assert body["criteria"][1]["disagreement"] is False
    # No recorded response for these inputs: the summary from the numbers, not stored.
    assert body["source"] == "template"
    assert body["disagreements"]
    assert any("split" in d for d in body["disagreements"])
    assert body["open_questions"]
    assert {v["recommendation"] for v in body["voices"]} == {"no", "yes"}
    assert NAME not in json.dumps(body)  # notes redacted while blind
    assert body["can_decide"] is True
    # Now the recruiter (submitted) reads it too.
    assert (await api.call("GET", path, world, "recruiter")).status_code == 200

    # Decide: a second factor and a reason.
    no_otp = await api.client.post(
        f"{path}/decision",
        json={"decision": "advance", "reason": "Strong panel."},
        headers=api.headers(world, "manager", otp=False),
    )
    assert no_otp.status_code == 403
    no_reason = await api.call(
        "POST", f"{path}/decision", world, "manager", json={"decision": "advance", "reason": ""}
    )
    assert no_reason.status_code == 422
    interviewer_tries = await api.call(
        "POST",
        f"{path}/decision",
        world,
        "interviewer",
        json={"decision": "reject", "reason": "No."},
    )
    assert interviewer_tries.status_code == 403
    decided = await api.call(
        "POST",
        f"{path}/decision",
        world,
        "manager",
        json={"decision": "hold", "reason": "Split on the first criterion; one more interview."},
    )
    assert decided.status_code == 200, decided.text
    decision = decided.json()["decision"]
    assert decision["decision"] == "hold"
    assert decision["decision_record_id"]
    assert decided.json()["can_decide"] is False
    again = await api.call(
        "POST",
        f"{path}/decision",
        world,
        "manager",
        json={"decision": "advance", "reason": "Changed."},
    )
    assert again.status_code == 409
    (record,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT kind, entity_type, entity_id, output FROM decision_records WHERE id = :d",
        {"d": decision["decision_record_id"]},
    )
    assert record.kind == "approval"
    assert record.entity_type == "hire.application"
    assert record.entity_id == app_id
    assert record.output["decision"] == "hold"
    actions = [
        r.action
        for r in await owner_rows(
            owner_engine,
            world.org_id,
            "SELECT action FROM audit_events WHERE entity_id = :a ORDER BY seq",
            {"a": app_id},
        )
    ]
    assert "hire.debrief.decided" in actions

    assert (await api.call("GET", path, other, "manager")).status_code == 404


async def test_debrief_from_a_recorded_model_response_is_kept(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    # The default pipeline reveals identity at the interview stage: not blind here.
    from .hire_support import open_job
    from .test_hire_interview_kits import applicant
    from .test_hire_scheduling import book, slots

    job = await open_job(api, world)
    app_id = await applicant(api, world, job)
    panel = [str(world.people["interviewer"][0])]
    found = await slots(api, world, app_id, panel)
    booked = await book(api, world, app_id, panel, found["slots"][0]["start"])
    assert booked.status_code == 201, booked.text
    interview_id = booked.json()["id"]
    card = await submit(api, world, "interviewer", interview_id, 3, 3, "yes")

    interviewer_name = (
        await api.call("GET", f"/v1/hire/applications/{app_id}/scorecards", world, "recruiter")
    ).json()["items"][0]["interviewer_name"]
    rated = [
        debrief.Rated(c["id"], c["name"], [("Interviewer 1", interviewer_name, 3)])
        for c in card["criteria"]
    ]
    voice = debrief.Voice(
        "Interviewer 1",
        interviewer_name,
        "yes",
        f"{NAME} gave one example about {card['criteria'][0]['id']}.",
    )
    inputs = debrief.debrief_inputs(
        job_title=job["title"], stage="Interview", locale="en", rated=rated, voices=[voice]
    )
    answer = debrief.DebriefDraft(
        summary="Interviewer 1 rated every criterion 3 and recommends yes.",
        criteria=[
            debrief.CriterionNote(criterion_id=c["id"], summary="Rated 3 of 4.")
            for c in card["criteria"]
        ],
        disagreements=[],
        open_questions=["Ask for a second example in the next round."],
    )
    bundle = dumps_bundle(
        {
            "format": BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "entries": [entry_for(debrief.TASK, debrief.DebriefDraft, inputs, answer)],
        }
    )
    session = create_sessionmaker(owner_engine)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            await import_recordings(session, world.org_id, parse_bundle(bundle))

    path = f"/v1/hire/applications/{app_id}/debrief"
    got = (await api.call("GET", path, world, "manager")).json()
    assert got["status"] == "ready", got
    assert got["source"] == "model"
    assert got["from_recording"] is True
    assert got["prompt_version"] == "hire_debrief_summary/v1"
    assert got["summary"] == answer.summary
    assert got["open_questions"] == answer.open_questions
    assert all(c["summary"] == "Rated 3 of 4." for c in got["criteria"])
    again = (await api.call("GET", path, world, "manager")).json()
    assert again["summary"] == answer.summary
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT source, from_recording FROM hire_debriefs WHERE application_id = :a",
        {"a": app_id},
    )
    assert [(r.source, r.from_recording) for r in rows] == [("model", True)]


async def test_the_demo_debrief_opens_with_its_recorded_summary(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    """``make demo``: the job and scorecard the demo seed makes, the committed
    recordings, and the debrief the hiring manager opens is the recorded one."""
    from scripts.demo_seed import seed_jobs
    from synth.hire.interviews import DEMO_SCORECARD, demo_ratings, demo_rubric
    from synth.hire.interviews import recordings_text as demo_recordings

    from .hire_support import cv_pdf
    from .test_hire_scheduling import book, slots

    api = hire_api
    lines = await seed_jobs(create_sessionmaker(owner_engine), world.org_id)
    assert any("recorded interview kits and debriefs" in line for line in lines)
    jobs = (await api.call("GET", "/v1/hire/jobs?status=open", world, "recruiter")).json()
    job = next(j for j in jobs["items"] if j["slug"] == "backend-engineer-payments")
    reference = await api.apply(
        world,
        job["slug"],
        cv_pdf("demo-debrief"),
        email="demo.debrief@example.test",
        full_name="Demo Candidate",
        knockouts={k.id: "yes" for k in demo_rubric().knockout_questions},
    )
    listed = await api.call("GET", f"/v1/hire/applications?job_id={job['id']}", world, "recruiter")
    app_id = next(a["id"] for a in listed.json()["items"] if a["reference"] == reference)
    panel = [str(world.people["interviewer"][0])]
    found = await slots(api, world, app_id, panel)
    booked = await book(api, world, app_id, panel, found["slots"][0]["start"])
    assert booked.status_code == 201, booked.text
    interview_id = booked.json()["id"]
    card = (
        await api.call("GET", f"/v1/hire/interviews/{interview_id}/scorecard", world, "interviewer")
    ).json()["scorecard"]
    ratings = demo_ratings()
    assert sorted(c["id"] for c in card["criteria"]) == sorted(ratings)
    done = await api.call(
        "POST",
        f"/v1/hire/interviews/{interview_id}/scorecard/submit",
        world,
        "interviewer",
        json={"ratings": ratings, **DEMO_SCORECARD},
    )
    assert done.status_code == 200, done.text

    bundle = json.loads(demo_recordings())
    recorded = {
        e["response"]["summary"]
        for e in bundle["entries"]
        if e["task"] == debrief.TASK.name and isinstance(e.get("response"), dict)
    }
    assert recorded, "the demo bundle holds no debrief summary"
    for locale in ("en", "ar"):
        got = (
            await api.call(
                "GET", f"/v1/hire/applications/{app_id}/debrief?locale={locale}", world, "manager"
            )
        ).json()
        assert got["status"] == "ready", got
        assert got["source"] == "model"
        assert got["from_recording"] is True
        assert got["summary"] in recorded
    # One debrief row, holding both languages' recorded summaries.
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT content, source, from_recording FROM hire_debriefs WHERE application_id = :a",
        {"a": app_id},
    )
    assert sorted(row.content) == ["ar", "en"]
    assert (row.source, row.from_recording) == ("model", True)
