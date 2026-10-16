# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire interview kits (M3) on Postgres, through the API.

* A kit per candidate and stage: questions mapped to the plan's rubric criteria, with
  probes for weak or unclear screening evidence, from recorded model responses
  (offline), kept and reused; Arabic too.
* Blind safe: while the candidate is blind the kit carries no name and no CV quote; the
  quotes appear once the identity is revealed.
* The interviewer briefing email carries the kit (the M2 placeholder is gone).
* Permissions and tenant isolation.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.llm.bundle import import_recordings, parse_bundle
from pairly_hire.interviewing.recordings import recordings_text
from pairly_hire.pipeline import hooks
from pairly_hire.screening import reviewer as screening_reviewer

from .hire_support import (
    HireApi,
    HireWorld,
    cv_pdf,
    hire_api,
    open_job,
    owner_rows,
    seed_hire_world,
)
from .test_hire_scheduling import book, deliver, slots

__all__ = ["hire_api"]  # fixture

NAME = "Layla Haddad"


async def interview_world(owner: AsyncEngine) -> HireWorld:
    world = await seed_hire_world(owner)
    session = create_sessionmaker(owner)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            await import_recordings(session, world.org_id, parse_bundle(recordings_text()))
    return world


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> HireWorld:
    return await interview_world(owner_engine)


@pytest.fixture
async def other(owner_engine: AsyncEngine) -> HireWorld:
    return await interview_world(owner_engine)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    screening_reviewer.register()


class _Screened:
    """Screening results with quotes that name the candidate (blind safety)."""

    def __init__(self, criteria: Sequence[tuple[str, str]]) -> None:
        self.criteria = criteria

    async def summary(
        self, session: AsyncSession, application_id: uuid.UUID
    ) -> hooks.ScreeningSummary | None:
        return hooks.ScreeningSummary(
            status="scored",
            overall="62 / 100",
            criteria=tuple(
                hooks.CriterionEvidence(name, result, (f"{NAME} led the move to Postgres.",))
                for name, result in self.criteria
            ),
        )


async def applicant(api: HireApi, world: HireWorld, job: dict[str, Any]) -> str:
    reference = await api.apply(
        world,
        job["slug"],
        cv_pdf(f"kit-{uuid.uuid4().hex}"),
        email=f"layla.{uuid.uuid4().hex[:6]}@example.test",
        full_name=NAME,
    )
    listed = await api.call("GET", f"/v1/hire/applications?job_id={job['id']}", world, "recruiter")
    return str(next(a["id"] for a in listed.json()["items"] if a["reference"] == reference))


async def test_kit_from_recordings_is_kept_blind_safe_and_in_arabic(
    hire_api: HireApi, world: HireWorld, other: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    plan = (
        await api.call("GET", f"/v1/hire/jobs/{job['id']}/interview-plan", world, "recruiter")
    ).json()
    criteria = {c["id"]: c["name"] for c in plan["criteria"]}
    stage = plan["plan"]["stages"][0]
    first, second = stage["questions"][0]["criterion_id"], stage["questions"][1]["criterion_id"]
    hooks.set_screening_view(
        _Screened([(criteria[first], "partly_met"), (criteria[second], "unknown")])
    )
    app_id = await applicant(api, world, job)
    path = f"/v1/hire/applications/{app_id}/interview-kit?stage=interview"

    got = await api.call("GET", path, world, "interviewer")
    assert got.status_code == 200, got.text
    kit = got.json()
    assert kit["source"] == "model"
    assert kit["from_recording"] is True
    assert kit["prompt_version"] == "hire_interview_kit/v1"
    assert kit["identity_hidden"] is True
    assert kit["candidate_label"].startswith("Candidate ")
    assert [i["criterion_id"] for i in kit["items"]] == [
        q["criterion_id"] for q in stage["questions"]
    ]
    by_id = {i["criterion_id"]: i for i in kit["items"]}
    assert by_id[first]["screening_result"] == "partly_met"
    assert by_id[first]["probes"]  # weak evidence gets probes
    assert by_id[second]["screening_result"] == "unknown"
    assert by_id[second]["probes"]
    for item in kit["items"]:
        assert item["questions"][0] == next(
            q["question"] for q in stage["questions"] if q["criterion_id"] == item["criterion_id"]
        )
        assert len(item["anchors"]) == 4
        # Blind: no quote from the CV.
        assert item["quotes"] == []
    text = json.dumps(kit)
    assert NAME not in text
    assert "Layla" not in text

    again = await api.call("GET", path, world, "manager")
    assert again.json()["items"] == kit["items"]
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT count(*) AS n FROM hire_interview_kits WHERE application_id = :a",
        {"a": app_id},
    )
    assert rows[0].n == 1

    arabic = await api.call("GET", f"{path}&locale=ar", world, "interviewer")
    assert arabic.status_code == 200, arabic.text
    assert arabic.json()["from_recording"] is True
    assert arabic.json()["stage_name"] == "المقابلة"

    # Revealed: the recruiter turns blind mode off; the quotes appear.
    from .hire_support import set_blind_mode

    await set_blind_mode(api, world, False)
    shown = (await api.call("GET", path, world, "interviewer")).json()
    assert shown["identity_hidden"] is False
    assert any(NAME in q for i in shown["items"] for q in i["quotes"])

    # No plan stage: no kit. Permissions and tenant isolation.
    assert (
        await api.call("GET", path.replace("interview", "offer"), world, "recruiter")
    ).status_code == 404
    assert (await api.call("GET", path, world, "employee")).status_code == 403
    assert (await api.call("GET", path, other, "recruiter")).status_code == 404


async def test_briefing_email_carries_the_kit(
    hire_api: HireApi, world: HireWorld, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id = await applicant(api, world, job)
    interviewer = str(world.people["interviewer"][0])
    found = await slots(api, world, app_id, [interviewer])
    booked = await book(api, world, app_id, [interviewer], found["slots"][0]["start"])
    assert booked.status_code == 201, booked.text
    mails = await deliver(api, world, tmp_path)
    briefing = next(m for m in mails if "You are on the interview panel" in m["text"])
    assert "Interview kit:" in briefing["text"], briefing["text"]
    assert "will be here once interview kits" not in briefing["text"]
    assert "Ask: " in briefing["text"]
    assert NAME not in briefing["text"]
