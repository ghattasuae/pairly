# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Calibration mode (M3, brief 3.3.11) on Postgres, through the API.

People label scored applications per criterion without seeing the AI's scores; the
report gives agreement per criterion from labels and recorded overrides, with a
suggestion from the anchors for low-agreement criteria. A model suggestion fails closed
when no recorded answer exists. Another organisation sees nothing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from .hire_support import HireWorld, owner_rows
from .test_hire_screening import ScreenFactory, screen_factory, screening_world, world

__all__ = ["screen_factory", "world"]  # fixtures


async def test_labels_overrides_and_the_agreement_report(
    screen_factory: ScreenFactory, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = await screen_factory()
    job = await api.open_job(world)
    apps = [await api.apply(world, job, doc_id) for doc_id in ("cv-0026", "cv-0029")]
    ai: dict[str, dict[str, int]] = {}
    for app_id in apps:
        view = await api.screening(world, app_id)
        ai[app_id] = {c["criterion_id"]: c["score"] for c in view["criteria"]}

    assert (
        await api.call(
            "GET", f"/v1/hire/calibration/sample?job_id={job['id']}", world, "interviewer"
        )
    ).status_code == 403
    sample = await api.call(
        "GET", f"/v1/hire/calibration/sample?job_id={job['id']}", world, "recruiter"
    )
    assert sample.status_code == 200, sample.text
    body = sample.json()
    assert {i["application_id"] for i in body["items"]} == set(apps)
    criteria = [c["id"] for c in body["criteria"]]
    assert criteria
    assert all(len(c["anchors"]) == 5 for c in body["criteria"])
    target = criteria[0]

    # The item shows the CV (blind) and the person's own labels, never the AI's scores.
    item = await api.call("GET", f"/v1/hire/calibration/items/{apps[0]}", world, "recruiter")
    assert item.status_code == 200, item.text
    assert item.json()["blind"] is True
    assert item.json()["my_scores"] == {}
    assert "score" not in str(item.json()["criteria"])

    # Three people label both applications: the target criterion two points off the AI.
    def far(score: int) -> int:
        return score + 2 if score <= 2 else score - 2

    for who in ("recruiter", "manager", "admin"):
        for app_id in apps:
            labels: dict[str, Any] = {c: ai[app_id].get(c, 0) for c in criteria}
            labels[target] = far(ai[app_id].get(target, 0))
            saved = await api.call(
                "PUT", f"/v1/hire/calibration/items/{app_id}", world, who, json={"scores": labels}
            )
            assert saved.status_code == 200, saved.text
    again = await api.call(
        "PUT",
        f"/v1/hire/calibration/items/{apps[0]}",
        world,
        "recruiter",
        json={"scores": {target: far(ai[apps[0]].get(target, 0))}},
    )
    assert again.json()["my_scores"][target] == far(ai[apps[0]].get(target, 0))
    bad = await api.call(
        "PUT",
        f"/v1/hire/calibration/items/{apps[0]}",
        world,
        "recruiter",
        json={"scores": {"nope": 1}},
    )
    assert bad.status_code == 400
    # A recorded override feeds calibration too.
    override = await api.call(
        "POST",
        f"/v1/hire/applications/{apps[1]}/screening/overrides",
        world,
        "recruiter",
        json={
            "scores": {target: far(ai[apps[1]].get(target, 0))},
            "reason": "Checked the CV again.",
        },
    )
    assert override.status_code == 200, override.text

    report = await api.call(
        "GET", f"/v1/hire/calibration/report?job_id={job['id']}", world, "admin"
    )
    assert report.status_code == 200, report.text
    data = report.json()
    assert (data["labels"], data["overrides"]) == (6 * len(criteria), 1)
    by = {c["criterion_id"]: c for c in data["criteria"]}
    low = by[target]
    assert low["n"] == 7
    assert low["sources"] == {"label": 6, "override": 1}
    assert (low["exact"], low["within_one"]) == (0.0, 0.0)
    assert low["low_agreement"] is True
    assert low["suggestion"]
    assert low["kappa_note"] == "needs at least 10 pairs"
    other_criterion = criteria[1] if len(criteria) > 1 else None
    if other_criterion:
        assert by[other_criterion]["exact"] == 1.0
        assert by[other_criterion]["low_agreement"] is False
    # Recorded mode has no answer for this job: the model suggestion fails closed.
    suggested = await api.call(
        "POST",
        "/v1/hire/calibration/suggest",
        world,
        "admin",
        json={"job_id": job["id"], "criterion_id": target},
    )
    assert suggested.status_code == 409
    assert "use the suggestion from the anchors" in suggested.text
    events = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT count(*) AS n FROM audit_events WHERE action = 'hire.calibration.labelled'",
    )
    assert events[0].n == 7

    # Hiring analytics on real screenings: cost from model_calls (recorded mode: 0, and
    # it says so), the AI scores counted as screened.
    analytics = await api.call("GET", f"/v1/hire/analytics?job_id={job['id']}", world, "admin")
    assert analytics.status_code == 200, analytics.text
    cost = analytics.json()["cost_per_screened_application"]
    assert cost["n"] == 2
    assert cost["detail"]["model_calls"] > 0
    assert cost["detail"]["recorded_mode"] is True
    assert cost["value"] == 0.0
    assert analytics.json()["override_rate"]["value"] == 0.5

    other = await screening_world(owner_engine)
    elsewhere = await api.call(
        "GET", f"/v1/hire/calibration/report?job_id={job['id']}", other, "admin"
    )
    assert elsewhere.status_code == 404
    assert (
        await api.call("GET", f"/v1/hire/calibration/items/{apps[0]}", other, "recruiter")
    ).status_code == 404
