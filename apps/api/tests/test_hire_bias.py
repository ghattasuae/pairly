# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The bias and adverse-impact report and its audit pack (M3, brief 3.10), on Postgres.

* Counts per voluntary self-identification group at each pipeline step, read only
  through ``pairly_hire.selfid.reporting``; no reference, id or name in the report.
* Small samples read "too small to judge" (Fisher exact), groups under 5 are hidden.
* The export needs ``hire.bias.export`` and a two-factor sign-in, and is audited.
* Another organisation sees none of it.
"""

from __future__ import annotations

import io
import zipfile

from sqlalchemy.ext.asyncio import AsyncEngine

from .hire_support import HireApi, HireWorld, hire_api, open_job, owner_rows
from .test_hire_pipeline import move, other, world
from .test_hire_portal import _apply

__all__ = ["hire_api", "other", "world"]  # fixtures


async def test_bias_report_and_audit_pack(
    hire_api: HireApi, world: HireWorld, other: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    women = [
        await _apply(api, world, job, f"Woman {i}", self_id={"gender": "woman"}) for i in range(6)
    ]
    men = [await _apply(api, world, job, f"Man {i}", self_id={"gender": "man"}) for i in range(6)]
    await _apply(api, world, job, "Another One", self_id={"gender": "another"})
    for app_id, _ in [*men[:5], women[0]]:
        moved = await move(api, world, app_id, "screening")
        assert moved.status_code == 200, moved.text

    assert (await api.call("GET", "/v1/hire/bias", world, "interviewer")).status_code == 403
    got = await api.call("GET", f"/v1/hire/bias?job_id={job['id']}", world, "recruiter")
    assert got.status_code == 200, got.text
    report = got.json()
    assert report["applications"] == 13
    gender = next(c for c in report["categories"] if c["category"] == "gender")
    first = next(s for s in gender["steps"] if s["from_stage"] == "applied")
    by = {g["group"]: g for g in first["groups"]}
    assert first["reference_group"] == "man"
    assert (by["man"]["base"], by["man"]["selected"]) == (6, 5)
    assert (by["woman"]["base"], by["woman"]["selected"]) == (6, 1)
    assert by["woman"]["impact_ratio"] == 0.2
    assert by["woman"]["small_sample"] is True
    assert by["woman"]["judgement"] == "too_small_to_judge"
    assert by["another"]["suppressed"] is True
    assert by["another"]["base"] is None
    for app_id, _ in [*women, *men]:
        assert app_id not in got.text
    assert "Woman 0" not in got.text

    # The export: its own permission, a second factor, an audit event.
    path = f"/v1/hire/bias/export?job_id={job['id']}"
    assert (await api.call("GET", path, world, "recruiter")).status_code == 403
    no_otp = await api.call("GET", path, world, "admin", otp=False)
    assert no_otp.status_code == 403
    exported = await api.call("GET", path, world, "admin")
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"] == "application/zip"
    assert exported.headers["cache-control"] == "no-store"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert sorted(archive.namelist()) == ["METHOD.md", "data.csv", "report.json", "results.csv"]
        assert "too_small_to_judge" in archive.read("results.csv").decode()
    events = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT action, payload FROM audit_events WHERE action LIKE 'hire.bias.%' ORDER BY seq",
    )
    actions = [e.action for e in events]
    assert "hire.bias.report_viewed" in actions
    assert actions.count("hire.bias.exported") == 1
    exported_event = next(e for e in events if e.action == "hire.bias.exported")
    assert exported_event.payload["sha256"] == exported.headers["x-pairly-sha256"]
    # Auditors export too.
    assert (await api.call("GET", path, world, "auditor")).status_code == 200

    elsewhere = await api.call("GET", "/v1/hire/bias", other, "admin")
    assert elsewhere.status_code == 200
    assert elsewhere.json()["applications"] == 0
