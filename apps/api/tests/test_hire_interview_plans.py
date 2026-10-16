# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire interview plans (M3) on Postgres, through the API.

* Without a locked rubric a job has no plan; with one, the default plan scores 3 to 5
  rubric criteria at the interview stage with 1 to 4 anchors.
* Saving: permissions, validation against the rubric, the pipeline and the team, the
  version check (no lost updates), audit; the plan's interviewers become the default
  panel when scheduling; tenant isolation.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from .hire_support import (
    HireApi,
    HireWorld,
    cv_pdf,
    hire_api,
    open_job,
    owner_rows,
    seed_hire_world,
)

__all__ = ["hire_api"]  # fixture


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> HireWorld:
    return await seed_hire_world(owner_engine)


@pytest.fixture
async def other(owner_engine: AsyncEngine) -> HireWorld:
    return await seed_hire_world(owner_engine)


def _uid(world: HireWorld, who: str) -> str:
    return str(world.people[who][0])


async def _put(
    api: HireApi, world: HireWorld, who: str, job_id: str, plan: Any, version: int | None
) -> Any:
    return await api.call(
        "PUT",
        f"/v1/hire/jobs/{job_id}/interview-plan",
        world,
        who,
        json={"plan": plan, "expected_version": version},
    )


async def test_default_plan_save_and_validation(
    hire_api: HireApi, world: HireWorld, other: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    draft = await api.call(
        "POST",
        "/v1/hire/jobs",
        world,
        "recruiter",
        json={
            "title": "Draft Role",
            "location": "Dubai",
            "work_model": "onsite",
            "employment_type": "full_time",
        },
    )
    assert draft.status_code == 201, draft.text
    none = await api.call(
        "GET", f"/v1/hire/jobs/{draft.json()['id']}/interview-plan", world, "manager"
    )
    assert none.status_code == 200, none.text
    assert none.json()["plan"] is None
    assert none.json()["rubric_locked"] is False

    job = await open_job(api, world)
    got = await api.call("GET", f"/v1/hire/jobs/{job['id']}/interview-plan", world, "interviewer")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["saved"] is False
    assert body["version"] is None
    assert body["can_edit"] is False
    criteria = {c["id"] for c in body["criteria"]}
    (stage,) = body["plan"]["stages"]
    assert stage["key"] == "interview"
    assert 3 <= len(stage["questions"]) <= 5
    assert {q["criterion_id"] for q in stage["questions"]} <= criteria
    assert len(body["default_anchors"]) == 4
    assert body["plan"]["sla_hours"] == 24

    plan = body["plan"]
    plan["stages"][0]["interviewer_ids"] = [_uid(world, "interviewer")]
    plan["stages"][0]["questions"][0]["anchors"] = [
        "Could not describe the work.",
        "Described it with gaps.",
        "Clear, concrete example.",
        "Several strong examples.",
    ]
    # Interviewers and employees cannot edit; nobody saves without the version they read.
    assert (await _put(api, world, "interviewer", job["id"], plan, None)).status_code == 403
    assert (await _put(api, world, "employee", job["id"], plan, None)).status_code == 403
    saved = await _put(api, world, "manager", job["id"], plan, None)
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 1
    assert saved.json()["saved"] is True
    stale = await _put(api, world, "recruiter", job["id"], plan, None)
    assert stale.status_code == 409
    again = await _put(api, world, "recruiter", job["id"], plan, 1)
    assert again.status_code == 200
    assert again.json()["version"] == 2

    def changed(**edit: Any) -> dict[str, Any]:
        copy: dict[str, Any] = {"sla_hours": plan["sla_hours"], "stages": [dict(plan["stages"][0])]}
        copy["stages"][0].update(edit)
        return copy

    questions = plan["stages"][0]["questions"]
    bad_cases = [
        changed(questions=questions[:2]),  # fewer than 3
        changed(questions=[*questions[:3], questions[0]]),  # a criterion twice
        changed(questions=[{**questions[0], "criterion_id": "not_in_rubric"}, *questions[1:]]),
        changed(key="hired"),  # an outcome, not an interview stage
        changed(key="nowhere"),
        changed(interviewer_ids=[_uid(world, "employee")]),  # cannot sit on a panel
        changed(questions=[{**questions[0], "anchors": ["one", "two"]}, *questions[1:]]),
    ]
    for case in bad_cases:
        refused = await _put(api, world, "recruiter", job["id"], case, 2)
        assert refused.status_code in {400, 422}, (case, refused.text)
    assert (
        await api.call("GET", f"/v1/hire/jobs/{job['id']}/interview-plan", world, "recruiter")
    ).json()["version"] == 2

    # Another organisation never sees it.
    hidden = await api.call("GET", f"/v1/hire/jobs/{job['id']}/interview-plan", other, "recruiter")
    assert hidden.status_code == 404
    assert (await _put(api, other, "recruiter", job["id"], plan, 2)).status_code == 404

    events = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT payload FROM audit_events WHERE action = 'hire.interview_plan.saved' "
        "AND entity_id = :j ORDER BY seq",
        {"j": job["id"]},
    )
    assert [e.payload["version"] for e in events] == [1, 2]


async def test_plan_interviewers_are_the_default_panel(hire_api: HireApi, world: HireWorld) -> None:
    api = hire_api
    job = await open_job(api, world)
    plan = (
        await api.call("GET", f"/v1/hire/jobs/{job['id']}/interview-plan", world, "recruiter")
    ).json()["plan"]
    applied = dict(plan["stages"][0], key="applied", name="Phone screen")
    applied["interviewer_ids"] = [_uid(world, "interviewer"), _uid(world, "manager")]
    plan["stages"].append(applied)
    saved = await _put(api, world, "recruiter", job["id"], plan, None)
    assert saved.status_code == 200, saved.text

    reference = await api.apply(world, job["slug"], cv_pdf(f"plan-{uuid.uuid4().hex}"))
    listed = await api.call("GET", f"/v1/hire/applications?job_id={job['id']}", world, "recruiter")
    app_id = next(a["id"] for a in listed.json()["items"] if a["reference"] == reference)
    panel = await api.call("GET", f"/v1/hire/scheduling/applications/{app_id}", world, "recruiter")
    assert panel.status_code == 200, panel.text
    assert sorted(panel.json()["default_panel"]) == sorted(
        [_uid(world, "interviewer"), _uid(world, "manager")]
    )
