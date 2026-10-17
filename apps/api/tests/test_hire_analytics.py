# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Hiring analytics (M3, brief 3.10) on Postgres, through the API.

* HR and talent leads (recruiters) and admins read it; interviewers do not.
* Numbers come from the organisation's own rows, each with the records behind it
  (application references, never names).
* Another organisation's applications never show up (Row Level Security).
"""

from __future__ import annotations

from typing import Any

from .hire_support import HireApi, HireWorld, hire_api, open_job
from .test_hire_pipeline import move, other, world
from .test_hire_portal import _apply

__all__ = ["hire_api", "other", "world"]  # fixtures


def by_stage(metrics: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {m["detail"]["stage"]: m for m in metrics}


async def test_analytics_from_real_rows_with_records(
    hire_api: HireApi, world: HireWorld, other: HireWorld
) -> None:
    api = hire_api
    job = await open_job(api, world)
    first, _ = await _apply(api, world, job, "Nadia Saleh")
    second, _ = await _apply(api, world, job, "Karim Haddad")
    for stage in ("screening", "shortlist"):
        moved = await move(api, world, first, stage)
        assert moved.status_code == 200, moved.text

    denied = await api.call("GET", "/v1/hire/analytics", world, "interviewer")
    assert denied.status_code == 403
    got = await api.call("GET", f"/v1/hire/analytics?job_id={job['id']}", world, "recruiter")
    assert got.status_code == 200, got.text
    data = got.json()
    assert data["applications"] == 2
    conversion = by_stage(data["stage_conversion"])
    assert conversion["applied"]["detail"]["reached"] == 2
    assert conversion["applied"]["value"] == 0.5
    assert conversion["screening"]["value"] == 1.0
    assert {r["id"] for r in conversion["applied"]["records"]} == {first}
    assert conversion["applied"]["records"][0]["job_id"] == job["id"]
    stays = by_stage(data["time_in_stage"])
    assert stays["applied"]["n"] == 1
    sources = {m["detail"]["source"]: m for m in data["sources"]}
    assert sources["careers"]["detail"]["applications"] == 2
    assert sources["careers"]["detail"]["shortlisted"] == 1
    # Nothing was hired or messaged yet: no number is invented.
    assert data["time_to_hire"]["value"] is None
    assert data["time_to_first_response"]["detail"]["pending"] == 2
    assert data["scheduling_time_saved"]["detail"]["estimate"] is True
    assert data["survey"] is not None  # recruiters read survey totals
    assert data["settings"]["sla_days"] == 7
    # Records are references, never names.
    assert "Nadia" not in got.text
    assert "Karim" not in got.text
    assert second in {r["id"] for r in sources["careers"]["records"]}

    # Admins read it too; another organisation sees none of these rows.
    assert (await api.call("GET", "/v1/hire/analytics", world, "admin")).status_code == 200
    elsewhere = await api.call("GET", "/v1/hire/analytics", other, "recruiter")
    assert elsewhere.status_code == 200
    assert elsewhere.json()["applications"] == 0
    assert first not in elsewhere.text
    bad = await api.call("GET", "/v1/hire/analytics?agreement_threshold=101", world, "recruiter")
    assert bad.status_code == 422
