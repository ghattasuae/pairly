# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The screening queue for recruiter screening mode (WP2.6), on Postgres, offline.

The queue lists a job's applications oldest first with the latest screening's status,
overall score and flag codes, blind labels while blind, the stages, the blind mode
setting and whether the caller may change it, and whether scheduling is available. The
explainability panel carries the rubric anchors for "why not higher".
"""

from __future__ import annotations

import json

from synth.hire.screening import contact_for

from .hire_support import HireWorld
from .test_hire_screening import ScreenFactory, screen_factory, spec, world

__all__ = ["screen_factory", "world"]  # fixtures


async def test_screening_queue_is_stable_blind_and_scored(
    screen_factory: ScreenFactory, world: HireWorld
) -> None:
    api = await screen_factory()
    job = await api.open_job(world)
    first = await api.apply(world, job, "cv-0026")
    second = await api.apply(world, job, "cv-0029")
    view = await api.screening(world, first)
    assert view["scored"]
    queue = await api.call("GET", f"/v1/hire/jobs/{job['id']}/screening-queue", world, "recruiter")
    assert queue.status_code == 200, queue.text
    data = queue.json()
    assert [i["application_id"] for i in data["items"]] == [first, second]
    assert data["blind_mode"] is True
    assert data["can_change_blind_mode"] is False
    assert data["scheduling_available"] is False
    assert [s["key"] for s in data["stages"]][:3] == ["applied", "screening", "shortlist"]
    item = data["items"][0]
    assert item["identity_hidden"] is True
    assert item["label"].startswith("Candidate ")
    assert item["scored"] is True
    assert item["overall_score"] == view["overall_score"]
    assert item["screening_status"] == "awaiting_review"
    dumped = json.dumps(data, ensure_ascii=False)
    for doc in ("cv-0026", "cv-0029"):
        assert contact_for(spec(doc))["full_name"] not in dumped
    admin = await api.call("GET", f"/v1/hire/jobs/{job['id']}/screening-queue", world, "admin")
    assert admin.json()["can_change_blind_mode"] is True
    # Anchors for "why not higher": the next level's text, from the locked rubric.
    for criterion in view["criteria"]:
        assert criterion["anchor"]
        if criterion["score"] < 4:
            assert criterion["next_anchor"]
        else:
            assert criterion["next_anchor"] is None
    # Employees never see the queue.
    employee = await api.call(
        "GET", f"/v1/hire/jobs/{job['id']}/screening-queue", world, "employee"
    )
    assert employee.status_code == 403
