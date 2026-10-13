# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire offers (M3) on Postgres, through the API. No e-signature: a PDF plus the
candidate's answer in the portal.

* Built from a template; compensation is encrypted and visible only with
  ``hire.offer.compensation`` (auditors read offers without it, interviewers not at all).
* Approval chain from the YAML rules: the hiring manager, then HR, and finance above
  the threshold; the submitter never approves (segregation of duties).
* One approval per offer: the chain's approval binds the exact offer email (its
  rendered hash), so after the chain the email is sent once with no second approval;
  any change to the offer after the approval blocks the send. The candidate sees it in
  the portal only then, accepts or declines once (a decision record by the candidate,
  audited), and the board shows the status.
* The PDF letter in English and Arabic.
"""

from __future__ import annotations

import io
import unicodedata
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pdfplumber
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

import pairly_hire.offers.service as offer_service

from .hire_support import HireApi, HireWorld, hire_api, open_job, owner_rows
from .test_hire_pipeline import _key, other, world
from .test_hire_portal import _apply, _signed_in

__all__ = ["hire_api", "other", "world"]  # fixtures

PORTAL = "X-Pairly-Portal-Session"


def body(base: str = "20000", housing: str = "5000", **extra: Any) -> dict[str, Any]:
    return {
        "template": "standard",
        "start_date": (date.today() + timedelta(days=30)).isoformat(),
        "compensation": {
            "currency": "AED",
            "base_monthly": base,
            "housing_monthly": housing,
            "transport_monthly": "1000",
            "bonus_target_pct": "10",
        },
    } | extra


async def decide(
    api: HireApi, world: HireWorld, who: str, approval_id: str, decision: str = "approve"
) -> httpx.Response:
    return await api.client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"decision": decision, "reason": "Checked against the budget."},
        headers=api.headers(world, who, extra=_key()),
    )


async def offers(api: HireApi, world: HireWorld, app_id: str, who: str = "recruiter") -> Any:
    got = await api.call("GET", f"/v1/hire/applications/{app_id}/offers", world, who)
    assert got.status_code == 200, got.text
    return got.json()


async def submitted(api: HireApi, world: HireWorld, app_id: str, who: str, **kw: Any) -> Any:
    created = await api.call(
        "POST", f"/v1/hire/applications/{app_id}/offers", world, who, json=body(**kw)
    )
    assert created.status_code == 201, created.text
    offer = created.json()
    sub = await api.call("POST", f"/v1/hire/offers/{offer['id']}/submit", world, who)
    assert sub.status_code == 200, sub.text
    (got,) = (await offers(api, world, app_id, who))["items"]
    assert got["approval_id"] is not None, got
    return got


async def test_offer_chain_send_and_the_candidates_answer(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id, email = await _apply(api, world, job, "Layla Haddad")

    templates = (await api.call("GET", "/v1/hire/offer-templates", world, "recruiter")).json()
    assert {t["key"] for t in templates} == {"standard", "fixed_term"}
    # Compensation needs its own permission: finance reads but never builds offers.
    denied = await api.call(
        "POST", f"/v1/hire/applications/{app_id}/offers", world, "finance", json=body()
    )
    assert denied.status_code == 403
    created = await api.call(
        "POST", f"/v1/hire/applications/{app_id}/offers", world, "recruiter", json=body()
    )
    assert created.status_code == 201, created.text
    offer = created.json()
    assert offer["reference"].startswith("OFR-")
    assert offer["compensation"]["annual_total"] == "312000.00"
    assert len(offer["conditions"]) == 4
    assert offer["status"] == "draft"
    # Encrypted at rest.
    (raw,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT compensation FROM hire_offers WHERE id = :i",
        {"i": offer["id"]},
    )
    assert "20000" not in raw.compensation
    # Visibility: auditors without compensation, interviewers not at all.
    audited = (await offers(api, world, app_id, "auditor"))["items"][0]
    assert audited["compensation"] is None
    assert audited["reference"] == offer["reference"]
    assert (await offers(api, world, app_id, "finance"))["sees_compensation"] is True
    blocked = await api.call("GET", f"/v1/hire/applications/{app_id}/offers", world, "interviewer")
    assert blocked.status_code == 403
    for who in ("auditor", "interviewer"):
        pdf_denied = await api.call("GET", f"/v1/hire/offers/{offer['id']}/pdf", world, who)
        assert pdf_denied.status_code == 403, who

    # Editing a draft names the version it edited.
    stale = await api.call(
        "PUT",
        f"/v1/hire/offers/{offer['id']}",
        world,
        "recruiter",
        json={"expected_version": 0, "conditions": ["Probation of 3 months."]},
    )
    assert stale.status_code == 409
    edited = await api.call(
        "PUT",
        f"/v1/hire/offers/{offer['id']}",
        world,
        "recruiter",
        json={"expected_version": 1, "conditions": ["Probation of 3 months."]},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 2
    early = await api.call("POST", f"/v1/hire/offers/{offer['id']}/send", world, "recruiter")
    assert early.status_code == 409  # never before the approval

    sub = await api.call("POST", f"/v1/hire/offers/{offer['id']}/submit", world, "recruiter")
    assert sub.status_code == 200, sub.text
    (pending,) = (await offers(api, world, app_id))["items"]
    assert pending["status"] == "pending_approval"
    assert pending["chain"] == "standard"
    assert [s["name"] for s in pending["steps"]] == ["hiring_manager", "hr"]
    approval = pending["approval_id"]
    detail = (await api.call("GET", f"/v1/approvals/{approval}", world, "manager")).json()
    assert detail["payload"]["annual_total"] == "312000.00"
    assert "base_monthly" not in detail["payload"]  # the breakdown stays encrypted
    # The same approval binds the exact offer email; approvers read it in the context.
    assert len(detail["payload"]["message_sha256"]) == 64
    assert detail["context"]["email_text"]
    frozen_id = detail["payload"]["message_id"]
    frozen = (await api.call("GET", f"/v1/hire/messages/{frozen_id}", world, "recruiter")).json()
    assert frozen["template_key"] == "hire.candidate.offer"
    assert frozen["status"] == "awaiting_approval"
    assert frozen["approval_id"] == approval
    # HR cannot jump the hiring manager; the submitter never approves.
    assert (await decide(api, world, "recruiter", approval)).status_code == 403
    assert (await decide(api, world, "manager", approval)).status_code == 200
    assert (await decide(api, world, "recruiter", approval)).status_code == 403
    assert (await decide(api, world, "admin", approval)).status_code == 200
    (approved,) = (await offers(api, world, app_id))["items"]
    assert approved["status"] == "approved", approved
    assert approved["sent"] is False

    # The board shows the offer's status.
    board = (
        await api.call("GET", f"/v1/hire/jobs/{job['id']}/pipeline", world, "recruiter")
    ).json()
    (card,) = [c for c in board["cards"] if c["application_id"] == app_id]
    assert card["offer_status"] == "approved"

    # The candidate sees nothing until the offer email is sent.
    portal = await _signed_in(api, world, email, tmp_path / "a")
    path = f"/v1/hire/portal/applications/{app_id}/offer"
    assert (await api.client.get(path, headers=portal)).status_code == 404
    sent = await api.call("POST", f"/v1/hire/offers/{offer['id']}/send", world, "recruiter")
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "sending"
    message_id = sent.json()["message_id"]
    assert message_id == frozen_id
    message = (await api.call("GET", f"/v1/hire/messages/{message_id}", world, "recruiter")).json()
    assert message["template_key"] == "hire.candidate.offer"
    # No second approval: the offer chain's approval sent it.
    assert message["approval_id"] == approval
    assert message["status"] in {"queued", "sent"}
    (note,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT approval_id FROM notifications WHERE id = "
        "(SELECT notification_id FROM hire_messages WHERE id = :m)",
        {"m": message_id},
    )
    assert str(note.approval_id) == approval
    # Sent once: the offer is no longer approved, and the approval is spent.
    again_send = await api.call("POST", f"/v1/hire/offers/{offer['id']}/send", world, "recruiter")
    assert again_send.status_code == 409
    seen = await api.client.get(path, headers=portal)
    assert seen.status_code == 200, seen.text
    got = seen.json()
    assert got["status"] == "sent"
    assert got["compensation"]["annual_total"] == "312000.00"
    assert got["conditions"] == ["Probation of 3 months."]
    letter = await api.client.get(f"/v1/hire/portal/offers/{got['id']}/pdf", headers=portal)
    assert letter.status_code == 200
    assert letter.headers["content-type"] == "application/pdf"

    # Staff credentials never answer for the candidate; the candidate answers once.
    staff = await api.client.post(
        f"/v1/hire/portal/offers/{got['id']}/accept",
        json={"confirm": True},
        headers=api.headers(world, "recruiter"),
    )
    assert staff.status_code == 401
    no_confirm = await api.client.post(
        f"/v1/hire/portal/offers/{got['id']}/accept", json={"confirm": False}, headers=portal
    )
    assert no_confirm.status_code == 422
    accepted = await api.client.post(
        f"/v1/hire/portal/offers/{got['id']}/accept",
        json={"confirm": True, "note": "Looking forward to it."},
        headers=portal,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted"
    again = await api.client.post(
        f"/v1/hire/portal/offers/{got['id']}/decline", json={"confirm": True}, headers=portal
    )
    assert again.status_code == 409
    (final,) = (await offers(api, world, app_id))["items"]
    assert final["status"] == "accepted"
    record_id = final["response_record_id"]
    (record,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT actor_type, actor_id, output FROM decision_records WHERE id = :i",
        {"i": record_id},
    )
    assert record.actor_type == "candidate"
    assert record.output == {"decision": "accepted"}
    actions = [
        r.action
        for r in await owner_rows(
            owner_engine,
            world.org_id,
            "SELECT action, actor_type FROM audit_events WHERE entity_id = :e ORDER BY seq",
            {"e": offer["id"]},
        )
    ]
    for action in (
        "hire.offer.drafted",
        "hire.offer.edited",
        "hire.offer.submitted",
        "hire.offer.awaiting_approval",
        "hire.offer.approved",
        "hire.offer.send_requested",
        "hire.offer.accepted",
    ):
        assert action in actions, actions


async def test_finance_above_the_threshold_and_admin_submitter_is_excluded(
    hire_api: HireApi, world: HireWorld, other: HireWorld
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id, _ = await _apply(api, world, job, "Omar Saleh")
    offer = await submitted(api, world, app_id, "admin", base="45000", housing="10000")
    assert offer["chain"] == "above_threshold"
    assert [s["name"] for s in offer["steps"]] == ["hiring_manager", "hr", "finance"]
    approval = offer["approval_id"]
    # Admin is in every step's roles, but submitted it: excluded from all of them.
    assert (await decide(api, world, "admin", approval)).status_code == 403
    assert (await decide(api, world, "finance", approval)).status_code == 403
    assert (await decide(api, world, "manager", approval)).status_code == 200
    assert (await decide(api, world, "recruiter", approval)).status_code == 200
    mid = (await offers(api, world, app_id, "admin"))["items"][0]
    assert mid["status"] == "pending_approval"
    assert (await decide(api, world, "finance", approval)).status_code == 200
    done = (await offers(api, world, app_id, "admin"))["items"][0]
    assert done["status"] == "approved"
    # Another organisation sees nothing.
    elsewhere = await api.call("GET", f"/v1/hire/applications/{app_id}/offers", other, "recruiter")
    assert elsewhere.status_code == 404
    # One open offer per application.
    second = await api.call(
        "POST", f"/v1/hire/applications/{app_id}/offers", world, "recruiter", json=body()
    )
    assert second.status_code == 409


async def test_changes_requested_then_withdrawn_and_declined(
    hire_api: HireApi, world: HireWorld, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id, _ = await _apply(api, world, job, "Sara Nasser")
    offer = await submitted(api, world, app_id, "recruiter")
    sent_back = await decide(api, world, "manager", offer["approval_id"], "request_changes")
    assert sent_back.status_code == 200, sent_back.text
    (back,) = (await offers(api, world, app_id))["items"]
    assert back["status"] == "changes_requested"
    assert back["can_edit"] is True
    withdrawn = await api.call(
        "POST", f"/v1/hire/offers/{offer['id']}/withdraw", world, "recruiter"
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "withdrawn"
    # A new offer can follow a withdrawn one.
    fresh = await api.call(
        "POST", f"/v1/hire/applications/{app_id}/offers", world, "recruiter", json=body()
    )
    assert fresh.status_code == 201, fresh.text
    assert uuid.UUID(fresh.json()["id"]) != uuid.UUID(offer["id"])


async def test_start_date_is_checked_in_the_organisation_time_zone(
    hire_api: HireApi, world: HireWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id, _ = await _apply(api, world, job, "Omar Haddad")
    # 00:03 on 2 March in Dubai (the default organisation zone) is still 1 March in UTC:
    # 1 March is yesterday for the organisation, so it is refused; 2 March is today.
    frozen = datetime(2026, 3, 1, 20, 3, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(offer_service, "datetime", _Clock)
    url = f"/v1/hire/applications/{app_id}/offers"
    yesterday = await api.call("POST", url, world, "recruiter", json=body(start_date="2026-03-01"))
    assert yesterday.status_code == 409, yesterday.text
    today = await api.call("POST", url, world, "recruiter", json=body(start_date="2026-03-02"))
    assert today.status_code == 201, today.text
    offer = today.json()
    # Editing applies the same rule.
    edited = await api.call(
        "PUT",
        f"/v1/hire/offers/{offer['id']}",
        world,
        "recruiter",
        json={"expected_version": offer["version"], "start_date": "2026-03-01"},
    )
    assert edited.status_code == 409, edited.text


async def test_a_change_after_the_approval_blocks_the_send(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    """The chain approved one offer and one email: an offer changed afterwards (here
    behind the API's back) is never sent, and the frozen email of a withdrawn or
    sent-back offer is cancelled."""
    api = hire_api
    job = await open_job(api, world)
    app_id, _ = await _apply(api, world, job, "Huda Karim")
    offer = await submitted(api, world, app_id, "recruiter")
    approval = offer["approval_id"]
    assert (await decide(api, world, "manager", approval)).status_code == 200
    assert (await decide(api, world, "admin", approval)).status_code == 200
    (approved,) = (await offers(api, world, app_id))["items"]
    assert approved["status"] == "approved"
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE hire_offers SET start_date = start_date + 1 WHERE id = :i",
        {"i": offer["id"]},
    )
    refused = await api.call("POST", f"/v1/hire/offers/{offer['id']}/send", world, "recruiter")
    assert refused.status_code == 409, refused.text
    assert "offer_or_email_changed" in refused.text
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT status, notification_id FROM hire_messages WHERE id = :m",
        {"m": approved["message_id"]},
    )
    assert row.status == "awaiting_approval"
    assert row.notification_id is None
    used = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT id FROM notifications WHERE approval_id = :a",
        {"a": approval},
    )
    assert used == []
    # Withdrawn: its frozen email is cancelled and can never go out.
    withdrawn = await api.call(
        "POST", f"/v1/hire/offers/{offer['id']}/withdraw", world, "recruiter"
    )
    assert withdrawn.status_code == 200
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT status FROM hire_messages WHERE id = :m",
        {"m": approved["message_id"]},
    )
    assert row.status == "cancelled"


async def test_sent_back_offer_freezes_a_new_email_on_resubmit(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id, _ = await _apply(api, world, job, "Rami Aziz")
    offer = await submitted(api, world, app_id, "recruiter")
    first_message = offer["message_id"]
    back = await decide(api, world, "manager", offer["approval_id"], "request_changes")
    assert back.status_code == 200, back.text
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT status FROM hire_messages WHERE id = :m",
        {"m": first_message},
    )
    assert row.status == "cancelled"
    edited = await api.call(
        "PUT",
        f"/v1/hire/offers/{offer['id']}",
        world,
        "recruiter",
        json={"expected_version": offer["version"], "job_title": "Senior Analyst"},
    )
    assert edited.status_code == 200, edited.text
    again = await api.call("POST", f"/v1/hire/offers/{offer['id']}/submit", world, "recruiter")
    assert again.status_code == 200, again.text
    (fresh,) = (await offers(api, world, app_id))["items"]
    assert fresh["message_id"] not in (None, first_message)
    assert fresh["approval_id"] not in (None, offer["approval_id"])
    detail = await api.call("GET", f"/v1/approvals/{fresh['approval_id']}", world, "manager")
    assert detail.json()["payload"]["job_title"] == "Senior Analyst"
    assert detail.json()["payload"]["message_id"] == fresh["message_id"]


def _text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as doc:
        return unicodedata.normalize("NFKC", "\n".join(p.extract_text() or "" for p in doc.pages))


async def test_the_offer_letter_pdf_in_english_and_arabic(
    hire_api: HireApi, world: HireWorld
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id, _ = await _apply(api, world, job, "Mona Fares")
    created = await api.call(
        "POST", f"/v1/hire/applications/{app_id}/offers", world, "recruiter", json=body()
    )
    offer = created.json()
    en = await api.call("GET", f"/v1/hire/offers/{offer['id']}/pdf", world, "recruiter")
    assert en.status_code == 200, en.text
    assert en.headers["cache-control"] == "no-store"
    text = _text(en.content)
    assert "Offer of employment" in text
    assert "Mona Fares" in text
    assert "AED 20,000.00" in text
    assert "AED 312,000.00" in text
    assert "Probation period of 6 months." in text
    ar = await api.call("GET", f"/v1/hire/offers/{offer['id']}/pdf?locale=ar", world, "manager")
    assert ar.status_code == 200
    arabic = _text(ar.content)
    assert "عرض عمل"[::-1] in arabic
    assert "السنوي"[::-1] in arabic  # annual (total)
    assert "الشروط"[::-1] in arabic  # conditions
    assert "312,000.00" in arabic
