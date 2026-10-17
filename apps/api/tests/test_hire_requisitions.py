# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire requisitions (M3) on Postgres, through the API.

* A hiring manager requests a role; HR approves it (a second factor, never the
  requester); only then can a job be opened from it, and the job links to it.
* Above the budget threshold the chain adds finance after HR, from the YAML rules.
* Segregation of duties: the requester can never approve, even as an admin.
* Rejected and withdrawn requisitions open no job; permissions; audit; tenant isolation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

import pairly_hire.requisitions.service as requisition_service

from .hire_support import HireApi, HireWorld, hire_api, owner_rows, seed_hire_world

__all__ = ["hire_api"]  # fixture


@pytest.fixture
async def world(owner_engine: AsyncEngine) -> HireWorld:
    return await seed_hire_world(owner_engine, recordings=False)


@pytest.fixture
async def other(owner_engine: AsyncEngine) -> HireWorld:
    return await seed_hire_world(owner_engine, recordings=False)


def _key() -> dict[str, str]:
    return {"Idempotency-Key": f"k-{uuid.uuid4().hex}"}


def _body(budget: str = "300000", **extra: Any) -> dict[str, Any]:
    return {
        "title": "Warehouse Operations Lead",
        "title_ar": "قائد عمليات المستودع",
        "team": "Operations",
        "budget": budget,
        "currency": "AED",
        "headcount": 2,
        "location": "Dubai",
        "work_model": "onsite",
        "start_date": (date.today() + timedelta(days=45)).isoformat(),
        "justification": "Two new shifts start in the spring.",
    } | extra


async def _request(api: HireApi, world: HireWorld, who: str, **extra: Any) -> dict[str, Any]:
    created = await api.client.post(
        "/v1/hire/requisitions", json=_body(**extra), headers=api.headers(world, who, extra=_key())
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "pending_approval"
    # The approval run starts after the response (a background task).
    got = await api.call("GET", f"/v1/hire/requisitions/{created.json()['id']}", world, who)
    assert got.status_code == 200, got.text
    assert got.json()["approval_id"] is not None, got.json()
    return dict(got.json())


async def _decide(
    api: HireApi, world: HireWorld, who: str, approval_id: str, decision: str = "approve", **kw: Any
) -> httpx.Response:
    return await api.client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"decision": decision, "reason": "Checked against the plan."},
        headers=api.headers(world, who, extra=_key(), **kw),
    )


async def test_request_approve_then_open_a_linked_job(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    req = await _request(api, world, "manager")
    assert req["reference"].startswith("REQ-")
    assert req["chain"] == "standard"
    assert [s["name"] for s in req["steps"]] == ["hr"]
    assert req["mine"] is True
    assert req["can_cancel"] is True

    # No job before the approval.
    early = await api.client.post(
        f"/v1/hire/requisitions/{req['id']}/job",
        headers=api.headers(world, "manager", extra=_key()),
    )
    assert early.status_code == 409

    # The approver sees the request's details before approving.
    detail = (
        await api.call("GET", f"/v1/approvals/{req['approval_id']}", world, "recruiter")
    ).json()
    assert detail["approval_type"] == "hire.requisition"
    assert detail["payload"]["budget"] == "300000.00"
    assert detail["payload"]["headcount"] == 2

    # The requester can never approve; an approver needs a second factor.
    assert (await _decide(api, world, "manager", req["approval_id"])).status_code == 403
    assert (
        await _decide(api, world, "recruiter", req["approval_id"], otp=False)
    ).status_code == 403
    assert (await _decide(api, world, "interviewer", req["approval_id"])).status_code == 403
    ok = await _decide(api, world, "recruiter", req["approval_id"])
    assert ok.status_code == 200, ok.text

    approved = (
        await api.call("GET", f"/v1/hire/requisitions/{req['id']}", world, "manager")
    ).json()
    assert approved["status"] == "approved", approved
    assert approved["can_open_job"] is True

    opened = await api.client.post(
        f"/v1/hire/requisitions/{req['id']}/job",
        headers=api.headers(world, "manager", extra=_key()),
    )
    assert opened.status_code == 201, opened.text
    job = opened.json()
    assert job["requisition_id"] == req["id"]
    assert job["status"] == "draft"
    assert job["title"] == "Warehouse Operations Lead"
    assert job["work_model"] == "onsite"
    again = await api.client.post(
        f"/v1/hire/requisitions/{req['id']}/job",
        headers=api.headers(world, "manager", extra=_key()),
    )
    assert again.status_code == 409
    after = (await api.call("GET", f"/v1/hire/requisitions/{req['id']}", world, "manager")).json()
    assert after["job_id"] == job["id"]
    assert after["can_open_job"] is False

    actions = [
        r.action
        for r in await owner_rows(
            owner_engine,
            world.org_id,
            "SELECT action FROM audit_events WHERE entity_id = :e ORDER BY seq",
            {"e": req["id"]},
        )
    ]
    for expected in (
        "hire.requisition.requested",
        "hire.requisition.awaiting_approval",
        "hire.requisition.approved",
        "hire.requisition.job_opened",
    ):
        assert expected in actions, actions


async def test_large_budget_needs_finance_after_hr_and_admin_requester_is_excluded(
    hire_api: HireApi, world: HireWorld
) -> None:
    api = hire_api
    req = await _request(api, world, "admin", budget="750000")
    assert req["chain"] == "above_budget_threshold"
    assert [s["name"] for s in req["steps"]] == ["hr", "finance"]
    # Admin is in both steps' roles, but requested it: excluded from every step.
    assert (await _decide(api, world, "admin", req["approval_id"])).status_code == 403
    # Finance cannot jump the HR step.
    assert (await _decide(api, world, "finance", req["approval_id"])).status_code == 403
    assert (await _decide(api, world, "recruiter", req["approval_id"])).status_code == 200
    mid = (await api.call("GET", f"/v1/hire/requisitions/{req['id']}", world, "admin")).json()
    assert mid["status"] == "pending_approval"
    assert [s["status"] for s in mid["steps"]] == ["approved", "active"]
    # HR may not approve the finance step; the requester still may not.
    assert (await _decide(api, world, "recruiter", req["approval_id"])).status_code == 403
    assert (await _decide(api, world, "admin", req["approval_id"])).status_code == 403
    assert (await _decide(api, world, "finance", req["approval_id"])).status_code == 200
    done = (await api.call("GET", f"/v1/hire/requisitions/{req['id']}", world, "finance")).json()
    assert done["status"] == "approved"
    # Finance reads requisitions but cannot open jobs.
    assert done["can_open_job"] is False
    blocked = await api.client.post(
        f"/v1/hire/requisitions/{req['id']}/job",
        headers=api.headers(world, "finance", extra=_key()),
    )
    assert blocked.status_code == 403


async def test_rejected_and_withdrawn_open_no_job(hire_api: HireApi, world: HireWorld) -> None:
    api = hire_api
    rejected = await _request(api, world, "manager")
    no = await _decide(api, world, "recruiter", rejected["approval_id"], "reject")
    assert no.status_code == 200, no.text
    row = (
        await api.call("GET", f"/v1/hire/requisitions/{rejected['id']}", world, "manager")
    ).json()
    assert row["status"] == "rejected"
    job = await api.client.post(
        f"/v1/hire/requisitions/{rejected['id']}/job",
        headers=api.headers(world, "manager", extra=_key()),
    )
    assert job.status_code == 409

    pending = await _request(api, world, "manager")
    # Only the requester withdraws it.
    other_person = await api.call(
        "POST", f"/v1/hire/requisitions/{pending['id']}/cancel", world, "recruiter"
    )
    assert other_person.status_code == 403
    cancelled = await api.call(
        "POST", f"/v1/hire/requisitions/{pending['id']}/cancel", world, "manager"
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    # The approval was cancelled with the run: nothing left to approve.
    late = await _decide(api, world, "recruiter", pending["approval_id"])
    assert late.status_code in {404, 409}
    row = (await api.call("GET", f"/v1/hire/requisitions/{pending['id']}", world, "manager")).json()
    assert row["status"] == "cancelled"


async def test_start_date_is_checked_in_the_organisation_time_zone(
    hire_api: HireApi, world: HireWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 00:03 on 2 March in Dubai (the default organisation zone) is still 1 March in UTC:
    # 1 March is yesterday for the organisation, so it is refused; 2 March is today.
    frozen = datetime(2026, 3, 1, 20, 3, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(requisition_service, "datetime", _Clock)
    yesterday = await hire_api.client.post(
        "/v1/hire/requisitions",
        json=_body(start_date="2026-03-01"),
        headers=hire_api.headers(world, "manager", extra=_key()),
    )
    assert yesterday.status_code == 409, yesterday.text
    today = await hire_api.client.post(
        "/v1/hire/requisitions",
        json=_body(start_date="2026-03-02"),
        headers=hire_api.headers(world, "manager", extra=_key()),
    )
    assert today.status_code == 201, today.text


async def test_permissions_validation_and_tenant_isolation(
    hire_api: HireApi, world: HireWorld, other: HireWorld
) -> None:
    api = hire_api
    for who in ("interviewer", "employee", "auditor", "finance"):
        denied = await api.client.post(
            "/v1/hire/requisitions", json=_body(), headers=api.headers(world, who, extra=_key())
        )
        assert denied.status_code == 403, who
    assert (await api.call("GET", "/v1/hire/requisitions", world, "employee")).status_code == 403
    assert (await api.call("GET", "/v1/hire/requisitions", world, "auditor")).status_code == 200
    past = await api.client.post(
        "/v1/hire/requisitions",
        # Clearly in the past in every zone (the midnight boundary has its own test).
        json=_body(start_date=(date.today() - timedelta(days=30)).isoformat()),
        headers=api.headers(world, "manager", extra=_key()),
    )
    assert past.status_code == 409
    for bad in ({"headcount": 0}, {"budget": "-5"}, {"work_model": "space"}, {"currency": "aed"}):
        invalid = await api.client.post(
            "/v1/hire/requisitions",
            json=_body(**bad),
            headers=api.headers(world, "manager", extra=_key()),
        )
        assert invalid.status_code == 422, bad

    req = await _request(api, world, "manager")
    assert (
        await api.call("GET", f"/v1/hire/requisitions/{req['id']}", other, "recruiter")
    ).status_code == 404
    listed = (await api.call("GET", "/v1/hire/requisitions", other, "recruiter")).json()
    assert listed["items"] == []
    mine = (await api.call("GET", "/v1/hire/requisitions?mine=true", world, "recruiter")).json()
    assert mine["items"] == []
    ours = (await api.call("GET", "/v1/hire/requisitions", world, "recruiter")).json()
    assert [i["id"] for i in ours["items"]] == [req["id"]]
