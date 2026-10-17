# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Role dashboards (brief 5.1 items 1 to 7): who may open each, what each shows from
existing records, that another organisation's records never show, and that a blind
candidate appears by reference only."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from .hire_support import (
    HireApi,
    HireWorld,
    hire_api,
    open_job,
    owner_rows,
    retire_documents,
    seed_hire_world,
)
from .test_hire_pipeline import apply_many, move
from .test_hire_scheduling import book, slots, uid

__all__ = ["hire_api"]  # fixture

ROLES = ("recruiter", "hiring-manager", "interviewer", "talent", "referrer")


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


async def dash(api: HireApi, world: HireWorld, role: str, who: str) -> Any:
    return await api.call("GET", f"/v1/hire/dashboards/{role}", world, who)


def sections(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["key"]: s for s in [*data["action"], *data["sections"]]}


async def test_who_may_open_each_dashboard(hire_api: HireApi, world: HireWorld) -> None:
    allowed = {
        "recruiter": {"recruiter", "admin"},
        "hiring-manager": {"manager", "recruiter", "admin"},
        "interviewer": {"interviewer", "recruiter", "manager", "admin", "auditor"},
        "talent": {"recruiter", "admin"},
        "referrer": {"employee", "interviewer", "recruiter", "manager", "admin"},
    }
    for role in ROLES:
        for who in (
            "recruiter",
            "manager",
            "interviewer",
            "employee",
            "admin",
            "auditor",
            "finance",
        ):
            response = await dash(hire_api, world, role, who)
            expected = 200 if who in allowed[role] else 403
            assert response.status_code == expected, (role, who, response.text)
            if expected == 200:
                assert response.json()["action"] is not None  # action first, always present


async def test_dashboards_from_existing_records_blind_safe_and_per_organisation(
    hire_api: HireApi, world: HireWorld, other: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    ids = await apply_many(
        api, world, job, ["Nadia Secretname", "Omar Hiddenname", "Lina Shortlist"]
    )
    # A referral by the employee.
    link = await api.call(
        "POST", "/v1/hire/referrals/links", world, "employee", json={"job_id": job["id"]}
    )
    token = link.json()["url"].split("?ref=", 1)[1]
    referred = (await apply_many(api, world, job, ["Rana Referred"], referral=token))[
        "Rana Referred"
    ]
    assert (await move(api, world, ids["Lina Shortlist"], "shortlist")).status_code == 200

    # An interview with the interviewer on the panel, then moved into the past so its
    # scorecard is due.
    panel = [uid(world, "interviewer")]
    found = await slots(api, world, ids["Nadia Secretname"], panel)
    booked = await book(api, world, ids["Nadia Secretname"], panel, found["slots"][0]["start"])
    assert booked.status_code == 201, booked.text
    interview_id = booked.json()["id"]

    upcoming = await dash(api, world, "interviewer", "interviewer")
    assert upcoming.status_code == 200, upcoming.text
    up = sections(upcoming.json())["interviews_upcoming"]
    assert up["count"] == 1
    (item,) = up["items"]
    assert item["id"] == interview_id
    assert item["detail"]["meeting_url"].startswith("https://")  # join link
    assert item["application_id"] == ids["Nadia Secretname"]  # the kit link

    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE hire_interviews SET starts_at = now() - interval '3 hours', "
        "ends_at = now() - interval '2 hours' WHERE id = :i",
        {"i": interview_id},
    )
    mine = (await dash(api, world, "interviewer", "interviewer")).json()
    assert sections(mine)["scorecards_due"]["count"] == 1
    assert sections(mine)["interviews_past"]["items"][0]["detail"]["submitted"] is False
    # Another interviewer's dashboard never shows it.
    assert (
        sections((await dash(api, world, "interviewer", "manager")).json())["scorecards_due"][
            "count"
        ]
        == 0
    )

    recruiter = await dash(api, world, "recruiter", "recruiter")
    assert recruiter.status_code == 200, recruiter.text
    data = recruiter.json()
    found_sections = sections(data)
    assert data["scope"] == "mine"  # the recruiter's own jobs
    assert found_sections["missing_scorecards"]["count"] == 1
    assert found_sections["waiting"]["count"] >= 4
    (job_item,) = found_sections["jobs"]["items"]
    counts = {s["key"]: s["count"] for s in job_item["stages"]}
    assert counts["shortlist"] == 1
    assert sum(counts.values()) == 4
    assert {m["key"] for m in data["metrics"]} >= {"time_to_first_response"}

    manager = (await dash(api, world, "hiring-manager", "manager")).json()
    assert sections(manager)["shortlists"]["count"] == 1
    assert sections(manager)["shortlists"]["items"][0]["kind"] == "shortlist"
    assert {m["key"] for m in manager["metrics"]} == {"time_to_hire"}

    talent = (await dash(api, world, "talent", "recruiter")).json()
    keys = {m["key"] for m in talent["metrics"]}
    assert {"time_to_hire", "ghosting_rate", "override_rate"} <= keys
    assert any(k.startswith("stage_conversion.") for k in keys)
    assert {"analytics", "bias", "surveys"} <= set(talent["links"])
    assert talent["survey"] is not None

    referrer = (await dash(api, world, "referrer", "employee")).json()
    (ref,) = sections(referrer)["referrals"]["items"]
    assert ref["detail"]["stage"] == "in_progress"
    assert set(ref["detail"]) == {"job_title", "stage", "stage_label"}  # nothing more
    assert ref["application_id"] is None  # no link into the pipeline
    assert sections(referrer)["open_jobs"]["count"] >= 1

    # Blind by default: a candidate before the revealing stages shows by reference only
    # (Nadia's interview stage and Lina's shortlist reveal them, as on the board).
    every = json.dumps([data, manager, talent, mine, referrer])
    for name in ("Hiddenname", "Rana Referred"):
        assert name not in every
    waiting_labels = {i["application_id"]: i["label"] for i in found_sections["waiting"]["items"]}
    assert "Candidate" in waiting_labels[ids["Omar Hiddenname"]]
    assert referred not in json.dumps(referrer)

    # Tenant isolation: the other organisation sees none of it.
    for role, who in (
        ("recruiter", "recruiter"),
        ("talent", "admin"),
        ("interviewer", "interviewer"),
    ):
        theirs = (await dash(api, other, role, who)).json()
        text = json.dumps(theirs)
        assert job["id"] not in text
        assert interview_id not in text
        assert all(
            s["count"] == 0 for s in [*theirs["action"], *theirs["sections"]] if s["key"] != "jobs"
        )
