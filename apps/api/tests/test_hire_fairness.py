# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The counterfactual fairness runner (M3, brief 3.3.4) on Postgres, through the API.

Real screened applications (synthetic CVs screened in recorded mode) are copied with a
swapped name, gender markers and nationality; each copy goes through the guardrails and
the blind redaction. Recorded mode measures the redaction and reports the model delta as
``pending_live_run``. The latest run feeds the bias report. Another organisation sees
nothing; interviewers cannot start a run.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from .hire_support import HireWorld, owner_rows
from .test_hire_screening import ScreenFactory, screen_factory, screening_world, world

__all__ = ["screen_factory", "world"]  # fixtures


async def test_fairness_run_on_screened_applications(
    screen_factory: ScreenFactory, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = await screen_factory()
    job = await api.open_job(world)
    for doc_id in ("cv-0026", "cv-0029"):
        app_id = await api.apply(world, job, doc_id)
        assert (await api.screening(world, app_id))["overall_score"] is not None

    denied = await api.call("POST", "/v1/hire/fairness/runs", world, "interviewer", json={})
    assert denied.status_code == 403
    started = await api.call(
        "POST", "/v1/hire/fairness/runs", world, "recruiter", json={"sample_size": 5}
    )
    assert started.status_code == 202, started.text
    assert started.json()["mode"] == "recorded"
    run_id = started.json()["id"]

    listed = await api.call("GET", "/v1/hire/fairness/runs", world, "recruiter")
    assert listed.status_code == 200
    (run,) = listed.json()
    assert run["status"] == "completed", run
    assert run["applications"] == 2
    assert run["copies"] >= 4  # a name and a gender copy each, at least
    assert run["flagged"] == 0
    summary = run["summary"]
    assert summary["model_deltas"] == "pending_live_run"
    assert summary["redaction_complete"] == run["copies"]

    detail = await api.call("GET", f"/v1/hire/fairness/runs/{run_id}", world, "admin")
    assert detail.status_code == 200
    results = detail.json()["results"]
    assert {r["dimension"] for r in results} >= {"name", "gender"}
    for r in results:
        assert r["model_status"] == "pending_live_run"
        assert r["delta"] is None
        assert r["flagged"] is None
        assert r["redaction_complete"] is True
        assert r["leaked"] == []
        assert r["reference"]
    # Only synthetic values are stored, never the candidate's real name.
    stored = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT inserted::text AS inserted FROM hire_fairness_results WHERE run_id = :r",
        {"r": run_id},
    )
    assert stored
    assert all("example.test" in row.inserted for row in stored if "@" in row.inserted)

    # The bias report carries the latest run.
    report = await api.call("GET", "/v1/hire/bias", world, "admin")
    assert report.json()["fairness"]["run_id"] == run_id

    # Another organisation sees nothing.
    other = await screening_world(owner_engine)
    assert (
        await api.call("GET", f"/v1/hire/fairness/runs/{run_id}", other, "admin")
    ).status_code == 404
    assert (await api.call("GET", "/v1/hire/fairness/runs", other, "admin")).json() == []
    # Started and completed, both audited.
    events = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT action FROM audit_events WHERE action LIKE 'hire.fairness.%' ORDER BY seq",
    )
    assert [e.action for e in events] == ["hire.fairness.started", "hire.fairness.completed"]
