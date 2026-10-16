# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The pre-employment checklist (M3) on Postgres, through the API.

* Configurable documents per job (default ID, degree, visa) and retention after hire.
* Requested through the candidate portal's file requests; the candidate's file is stored
  encrypted (never as a shared document), opened only by HR and admins, audited.
* Status tracking: requested, received, verified, waived.
* Retention: set once the candidate is hired (the job's days after), the retention rule
  is registered, and files past it are removed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker, set_tenant
from pairly_hire.onboarding import service as checklist
from pairly_hire.plugin import plugin
from pairly_hire.retention import CHECKLIST_RULE_KEY

from .hire_support import HireApi, HireWorld, cv_pdf, hire_api, open_job, owner_rows
from .test_hire_pipeline import move, other, world
from .test_hire_portal import _apply, _signed_in

__all__ = ["hire_api", "other", "world"]  # fixtures


async def test_checklist_config_request_upload_review_and_retention(
    hire_api: HireApi, world: HireWorld, other: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id, email = await _apply(api, world, job, "Huda Karim")

    default = await api.call("GET", f"/v1/hire/jobs/{job['id']}/checklist", world, "recruiter")
    assert default.status_code == 200, default.text
    assert [i["key"] for i in default.json()["items"]] == ["id_document", "degree", "visa"]
    assert default.json()["retention_days"] == 90
    # HR and admins only: hiring managers, interviewers and auditors never.
    for who in ("manager", "interviewer", "auditor"):
        denied = await api.call("GET", f"/v1/hire/applications/{app_id}/checklist", world, who)
        assert denied.status_code == 403, who
    saved = await api.call(
        "PUT",
        f"/v1/hire/jobs/{job['id']}/checklist",
        world,
        "recruiter",
        json={
            "items": [
                {"key": "id_document", "label": "Passport", "label_ar": "جواز السفر"},
                {"key": "degree", "label": "Degree certificate"},
            ],
            "retention_days": 30,
        },
    )
    assert saved.status_code == 200, saved.text
    bad = await api.call(
        "PUT",
        f"/v1/hire/jobs/{job['id']}/checklist",
        world,
        "recruiter",
        json={"items": [{"key": "a", "label": "A"}, {"key": "a", "label": "B"}]},
    )
    assert bad.status_code in {400, 422}

    asked = await api.call("POST", f"/v1/hire/applications/{app_id}/checklist", world, "recruiter")
    assert asked.status_code == 200, asked.text
    items = {i["key"]: i for i in asked.json()["items"]}
    assert set(items) == {"id_document", "degree"}
    assert {i["status"] for i in items.values()} == {"requested"}
    again = await api.call("POST", f"/v1/hire/applications/{app_id}/checklist", world, "recruiter")
    assert len(again.json()["items"]) == 2  # only what is missing

    # The candidate sees two file requests in the portal and uploads the passport.
    portal = await _signed_in(api, world, email, tmp_path)
    detail = (await api.client.get(f"/v1/hire/portal/applications/{app_id}", headers=portal)).json()
    requests = {r["label"]: r for r in detail["document_requests"]}
    assert set(requests) >= {"Passport", "Degree certificate"}
    passport = cv_pdf("passport-scan")
    wrong = await api.client.post(
        f"/v1/hire/portal/document-requests/{requests['Passport']['id']}/file",
        files={"file": ("notes.txt", b"just text", "text/plain")},
        headers=portal,
    )
    assert wrong.status_code == 400
    sent = await api.client.post(
        f"/v1/hire/portal/document-requests/{requests['Passport']['id']}/file",
        files={"file": ("passport.pdf", passport, "application/pdf")},
        headers=portal,
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["request"]["status"] == "received"

    # Stored encrypted under its own key, never as a shared document.
    sealed = [k for k in api.store.objects if "/sealed/" in k]
    assert len(sealed) == 1
    assert b"%PDF" not in api.store.objects[sealed[0]].data
    docs = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT d.id FROM documents d WHERE d.sha256 = encode(sha256(:b), 'hex')",
        {"b": passport},
    )
    assert docs == []
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT file_name, status FROM hire_checklist_items WHERE id = :i",
        {"i": items["id_document"]["id"]},
    )
    assert row.status == "received"
    assert "passport" not in (row.file_name or "")

    # HR opens it (audited); others cannot.
    item_id = items["id_document"]["id"]
    opened = await api.call("GET", f"/v1/hire/checklist-items/{item_id}/file", world, "recruiter")
    assert opened.status_code == 200, opened.text
    assert opened.content == passport
    assert opened.headers["cache-control"] == "no-store"
    assert "passport.pdf" in opened.headers["content-disposition"]
    for who in ("manager", "interviewer"):
        assert (
            await api.call("GET", f"/v1/hire/checklist-items/{item_id}/file", world, who)
        ).status_code == 403
    assert (
        await api.call("GET", f"/v1/hire/checklist-items/{item_id}/file", other, "recruiter")
    ).status_code == 404

    verified = await api.call(
        "POST",
        f"/v1/hire/checklist-items/{item_id}/review",
        world,
        "recruiter",
        json={"outcome": "verified"},
    )
    assert verified.status_code == 200, verified.text
    early = await api.call(
        "POST",
        f"/v1/hire/checklist-items/{items['degree']['id']}/review",
        world,
        "recruiter",
        json={"outcome": "verified"},
    )
    assert early.status_code == 409  # nothing received yet
    waived = await api.call(
        "POST",
        f"/v1/hire/checklist-items/{items['degree']['id']}/review",
        world,
        "admin",
        json={"outcome": "waived"},
    )
    assert waived.status_code == 200
    statuses = {i["key"]: i["status"] for i in waived.json()["items"]}
    assert statuses == {"id_document": "verified", "degree": "waived"}

    actions = [
        r.action
        for r in await owner_rows(
            owner_engine,
            world.org_id,
            "SELECT action FROM audit_events WHERE entity_id = :e ORDER BY seq",
            {"e": item_id},
        )
    ]
    for action in (
        "hire.checklist.requested",
        "hire.checklist.received",
        "hire.checklist.file_opened",
        "hire.checklist.verified",
    ):
        assert action in actions, actions

    # Retention: registered, set once hired (30 days after), files removed past it.
    assert CHECKLIST_RULE_KEY in {r.key for r in plugin.retention_rules}
    for stage in ("screening", "shortlist", "interview", "offer", "hired"):
        moved = await move(api, world, app_id, stage)
        assert moved.status_code == 200, (stage, moved.text)
    session = create_sessionmaker(owner_engine)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            assert await checklist.set_retention(session) == 2
            assert await checklist.purge_due(session, api.store) == 0
    (kept,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT retention_until, purged_at FROM hire_checklist_items WHERE id = :i",
        {"i": item_id},
    )
    assert kept.purged_at is None
    assert timedelta(days=29) < kept.retention_until - datetime.now(UTC) <= timedelta(days=30)
    session = create_sessionmaker(owner_engine)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            later = datetime.now(UTC) + timedelta(days=31)
            assert await checklist.purge_due(session, api.store, later) == 2
    assert not [k for k in api.store.objects if "/sealed/" in k]
    gone = await api.call("GET", f"/v1/hire/checklist-items/{item_id}/file", world, "recruiter")
    assert gone.status_code == 404
