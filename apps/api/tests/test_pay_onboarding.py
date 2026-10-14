# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Vendor onboarding by invite link (WP4.2, brief 4.1.1) on Postgres, through the API.

* The link is signed and single use; it opens a short session that can only add
  documents (through the engine's intake) and submit the form.
* A submission becomes a pending vendor with duplicate hints for the AP review.
* Accepting it puts the vendor's own bank details through the change workflow (call
  back, dual approval) before anything is payable to them.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from .pay_support import (
    PayApi,
    PayWorld,
    audit_actions,
    other,
    pay_api,
    retire_documents,
    vendor_body,
    world,
)

__all__ = ["other", "pay_api", "world"]  # fixtures

SESSION = "X-Pairly-Vendor-Session"


def _form(index: int, **overrides: Any) -> dict[str, Any]:
    body = vendor_body(index)
    body.pop("acknowledge_similar", None)
    return {**body, "confirm": True, **overrides}


async def _open(api: PayApi, token: str) -> Any:
    return await api.client.post("/v1/pay/public/vendor-onboarding/open", json={"token": token})


async def test_invite_open_upload_submit_review_and_verify(
    pay_api: PayApi, world: PayWorld, other: PayWorld, owner_engine: AsyncEngine
) -> None:
    api = pay_api
    # An existing vendor with the IBAN the submission will reuse: a strong duplicate hint.
    existing = await api.ok(
        "POST", "/v1/pay/vendors", world, "clerk", status=201, json=vendor_body(10)
    )
    denied = await api.call("POST", "/v1/pay/vendor-invites", world, "approver", json={})
    assert denied.status_code == 403
    invite = await api.ok(
        "POST",
        "/v1/pay/vendor-invites",
        world,
        "clerk",
        status=201,
        json={"company_hint": "Seltus", "email": "Billing@Seltus.example", "locale": "ar"},
    )
    token = invite["token"]
    assert invite["path"] == f"/ar/vendor-onboarding#{token}"
    assert invite["state"] == "open"
    assert invite["email"] == "billing@seltus.example"
    listed = await api.ok("GET", "/v1/pay/vendor-invites", world, "clerk")
    assert "token" not in listed["items"][0]

    # A forged or garbled link reads as invalid; the real one works once.
    forged = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert (await _open(api, forged)).status_code == 404
    assert (await _open(api, "vi1.nonsense")).status_code == 404
    opened = await _open(api, token)
    assert opened.status_code == 200, opened.text
    session_token = opened.json()["session_token"]
    assert (await _open(api, token)).status_code == 404  # single use
    headers = {SESSION: session_token}
    page = await api.client.get("/v1/pay/vendor-onboarding/me", headers=headers)
    assert page.status_code == 200
    assert page.json()["company"] == world.name
    assert page.json()["locale"] == "ar"
    assert (await api.client.get("/v1/pay/vendor-onboarding/me")).status_code == 401
    # The session is not a staff credential.
    assert (await api.client.get("/v1/pay/vendors", headers=headers)).status_code == 401

    uploaded = await api.client.post(
        "/v1/pay/vendor-onboarding/me/documents",
        files={
            "file": (
                "trade-licence.pdf",
                b"%PDF-1.4\n% synthetic licence\n%%EOF\n",
                "application/pdf",
            )
        },
        headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    (doc,) = uploaded.json()["documents"]
    assert doc["filename"] == "trade-licence.pdf"

    # Invalid details are refused with the same codes as staff entry.
    bad = _form(0)
    bad["bank"] = {**bad["bank"], "iban": "AE000000000000000000000"}
    refused = await api.client.post(
        "/v1/pay/vendor-onboarding/me/submit", json=bad, headers=headers
    )
    assert refused.status_code == 400
    assert refused.json()["code"] == "bank_details_invalid"
    form = _form(0)
    form["bank"] = vendor_body(10)["bank"]  # the existing vendor's IBAN
    submitted = await api.client.post(
        "/v1/pay/vendor-onboarding/me/submit", json=form, headers=headers
    )
    assert submitted.status_code == 201, submitted.text
    reference = submitted.json()["reference"]
    # The session ends with the submission.
    assert (
        await api.client.get("/v1/pay/vendor-onboarding/me", headers=headers)
    ).status_code == 401

    pending = (await api.ok("GET", "/v1/pay/vendors?status=pending", world, "clerk"))["items"]
    (vendor,) = [v for v in pending if v["reference"] == reference]
    detail = await api.ok("GET", f"/v1/pay/vendors/{vendor['id']}", world, "clerk")
    assert detail["source"] == "onboarding"
    assert detail["status"] == "pending"
    assert detail["document_ids"] == [doc["id"]]
    assert detail["iban_masked"] is None  # nothing active yet
    assert detail["accounts"][0]["status"] == "proposed"
    assert any(
        r.startswith(f"possible_duplicate:{existing['reference']}")
        for r in detail["review_reasons"]
    )
    assert [(d["reference"], d["reasons"]) for d in detail["duplicates"]] == [
        (existing["reference"], ["iban"])
    ]
    (state,) = [
        i
        for i in (await api.ok("GET", "/v1/pay/vendor-invites", world, "clerk"))["items"]
        if i["id"] == invite["id"]
    ]
    assert state["state"] == "submitted"
    assert state["vendor_id"] == vendor["id"]
    # Another organisation sees none of it.
    assert (
        await api.call("GET", f"/v1/pay/vendors/{vendor['id']}", other, "admin")
    ).status_code == 404

    # Accepting it puts the vendor's own bank details through the change workflow.
    # (No cooling-off here: the ticket keeps the hours set when it opens.)
    await api.ok("PUT", "/v1/pay/settings", world, "finance", json={"cooling_off_hours": 0})
    accepted = await api.ok(
        "POST",
        f"/v1/pay/vendors/{vendor['id']}/accept",
        world,
        "clerk",
        json={"note": "Licence checked."},
    )
    assert accepted["status"] == "pending"
    assert accepted["payment_hold"] is True
    ticket_id = accepted["open_ticket_id"]
    assert ticket_id is not None
    ticket = await api.ok("GET", f"/v1/pay/bank-changes/{ticket_id}", world, "clerk2")
    assert ticket["source"] == "onboarding"
    assert ticket["previous_account"] is None
    assert ticket["proposed_account"]["id"] == detail["accounts"][0]["id"]
    assert ticket["verification"]["phone_on_file"] == "+97128611629"
    call = await api.call(
        "POST",
        f"/v1/pay/bank-changes/{ticket_id}/calls",
        world,
        "clerk2",
        json={"result": "confirmed", "called_number_on_file": True},
    )
    assert call.status_code == 200, call.text
    approval_id = call.json()["approval_id"]
    for who in ("approver", "approver2"):
        assert (await api.decide(world, who, approval_id)).status_code == 200
    live = await api.ok("GET", f"/v1/pay/vendors/{vendor['id']}", world, "clerk")
    assert live["status"] == "active"
    assert live["payment_hold"] is False
    assert live["accounts"][0]["status"] == "active"
    actions = await audit_actions(owner_engine, world.org_id, invite["id"])
    assert actions[:2] == ["pay.vendor_invite.created", "pay.vendor_invite.opened"]
    assert "pay.vendor_invite.submitted" in actions
    await retire_documents(owner_engine, world.org_id)


async def test_revoked_and_rejected(
    pay_api: PayApi, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    api = pay_api
    invite = await api.ok("POST", "/v1/pay/vendor-invites", world, "clerk", status=201, json={})
    revoked = await api.ok("POST", f"/v1/pay/vendor-invites/{invite['id']}/revoke", world, "clerk")
    assert revoked["state"] == "revoked"
    assert (await _open(api, invite["token"])).status_code == 404
    # A session dies with its invite too.
    second = await api.ok("POST", "/v1/pay/vendor-invites", world, "clerk", status=201, json={})
    session_token = (await _open(api, second["token"])).json()["session_token"]
    await api.ok("POST", f"/v1/pay/vendor-invites/{second['id']}/revoke", world, "clerk")
    me = await api.client.get("/v1/pay/vendor-onboarding/me", headers={SESSION: session_token})
    assert me.status_code == 401
    # A submission rejected by AP: its bank details never become active.
    third = await api.ok("POST", "/v1/pay/vendor-invites", world, "clerk", status=201, json={})
    token = (await _open(api, third["token"])).json()["session_token"]
    done = await api.client.post(
        "/v1/pay/vendor-onboarding/me/submit", json=_form(11), headers={SESSION: token}
    )
    assert done.status_code == 201, done.text
    pending = (await api.ok("GET", "/v1/pay/vendors?status=pending", world, "clerk"))["items"]
    (vendor,) = [v for v in pending if v["reference"] == done.json()["reference"]]
    rejected = await api.ok(
        "POST",
        f"/v1/pay/vendors/{vendor['id']}/reject",
        world,
        "clerk",
        json={"reason": "Unknown company."},
    )
    assert rejected["status"] == "rejected"
    assert rejected["accounts"][0]["status"] == "rejected"
    blocked = await api.call(
        "POST",
        f"/v1/pay/vendors/{vendor['id']}/bank-changes",
        world,
        "clerk",
        json={"bank": vendor_body(12)["bank"]},
    )
    assert blocked.status_code == 409
    assert uuid.UUID(vendor["id"])
    await retire_documents(owner_engine, world.org_id)
