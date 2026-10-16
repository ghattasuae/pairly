# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Vendor onboarding by invite link (WP4.2, brief 4.1.1) on Postgres, through the API.

* The link is signed and single use; it opens a short session that can only add
  documents (through the engine's intake) and submit the form.
* A submission becomes a pending vendor with duplicate hints for the AP review.
* Accepting it puts the vendor's own bank details through the change workflow (call
  back, dual approval) before anything is payable to them, on a number AP records from
  an independent source; the form's own number is never called (security review
  2026-10, M-05).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.approvals.executor import ExecutorRefusedError
from pairly_core.workflow.spec import FinalActionCall
from pairly_pay.bank_changes import approve_bank_change, get_ticket

from .pay_support import (
    PayApi,
    PayWorld,
    audit_actions,
    other,
    owner_rows,
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
    # Security review 2026-10, M-05: the number on the form is never the one called.
    assert detail["phone_on_file"] is None
    without = await api.call(
        "POST",
        f"/v1/pay/vendors/{vendor['id']}/accept",
        world,
        "clerk",
        json={"note": "Licence checked."},
    )
    assert without.status_code == 400
    assert without.json()["code"] == "callback_source_required"
    for incomplete in (
        {"phone": INDEPENDENT_PHONE, "source": "trade_licence"},
        {"phone": INDEPENDENT_PHONE, "reference": "Licence 778899"},
        {"phone": INDEPENDENT_PHONE, "source": "the vendor said so", "reference": "x" * 10},
    ):
        bad_source = await api.call(
            "POST",
            f"/v1/pay/vendors/{vendor['id']}/accept",
            world,
            "clerk",
            json={"callback": incomplete},
        )
        assert bad_source.status_code == 422, incomplete
    accepted = await api.ok(
        "POST",
        f"/v1/pay/vendors/{vendor['id']}/accept",
        world,
        "clerk",
        json={"note": "Licence checked.", "callback": CALLBACK},
    )
    assert accepted["status"] == "pending"
    assert accepted["payment_hold"] is True
    assert accepted["phone_on_file"] == "+97128611630"
    ticket_id = accepted["open_ticket_id"]
    assert ticket_id is not None
    ticket = await api.ok("GET", f"/v1/pay/bank-changes/{ticket_id}", world, "clerk2")
    assert ticket["source"] == "onboarding"
    assert ticket["previous_account"] is None
    assert ticket["proposed_account"]["id"] == detail["accounts"][0]["id"]
    assert ticket["verification"]["phone_on_file"] == "+97128611630"
    assert ticket["verification"]["callback"] == {
        "source": "trade_licence",
        "reference": CALLBACK["reference"],
        "recorded_by": str(world.id("clerk")),
        "matches_claim": False,
    }
    assert ticket["verification"]["callback_required"] is False
    assert ticket["request_had_contact_details"] is True
    assert "8611629" not in json.dumps(ticket)
    # The clerk who recorded the number does not call it (M-04).
    mine = await api.call(
        "POST",
        f"/v1/pay/bank-changes/{ticket_id}/calls",
        world,
        "clerk",
        json={"result": "confirmed", "called_number_on_file": True},
    )
    assert mine.status_code == 403
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


INDEPENDENT_PHONE = "+971 2 861 1630"
CALLBACK = {
    "phone": INDEPENDENT_PHONE,
    "source": "trade_licence",
    "reference": "Trade licence 778899, Abu Dhabi DED register, checked 16 Oct",
}


async def _onboarded(api: PayApi, world: PayWorld, index: int) -> dict[str, Any]:
    """A submitted onboarding with bank details, pending review."""
    invite = await api.ok("POST", "/v1/pay/vendor-invites", world, "clerk", status=201, json={})
    opened = await _open(api, invite["token"])
    assert opened.status_code == 200, opened.text
    headers = {SESSION: opened.json()["session_token"]}
    submitted = await api.client.post(
        "/v1/pay/vendor-onboarding/me/submit", json=_form(index), headers=headers
    )
    assert submitted.status_code == 201, submitted.text
    pending = (await api.ok("GET", "/v1/pay/vendors?status=pending", world, "clerk"))["items"]
    (vendor,) = [v for v in pending if v["reference"] == submitted.json()["reference"]]
    return dict(vendor)


async def test_an_onboarding_ticket_without_an_independent_number_is_never_verified(
    pay_api: PayApi, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    """M-05 for a ticket opened before the fix (the form's number was the number on file,
    no source): it cannot be verified, approved or activated until AP records the number
    from an independent source on the ticket."""
    api = pay_api
    vendor = await _onboarded(api, world, 1)
    accepted = await api.ok(
        "POST",
        f"/v1/pay/vendors/{vendor['id']}/accept",
        world,
        "clerk",
        json={"callback": CALLBACK},
    )
    ticket_id = accepted["open_ticket_id"]
    # The state such a ticket had before: no source, the form's number to call.
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE pay_bank_change_tickets SET callback_source = NULL, callback_reference = NULL, "
        "callback_recorded_by = NULL, callback_matches_claim = NULL, phone_set_by = NULL "
        "WHERE id = :t",
        {"t": ticket_id},
    )
    shown = await api.ok("GET", f"/v1/pay/bank-changes/{ticket_id}", world, "clerk2")
    assert shown["verification"]["callback_required"] is True
    assert shown["can_verify"] is False
    refused = await api.call(
        "POST",
        f"/v1/pay/bank-changes/{ticket_id}/calls",
        world,
        "clerk2",
        json={"result": "confirmed", "called_number_on_file": True},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "callback_source_missing"
    # A verifier without create permission cannot record it; AP can.
    denied = await api.call(
        "POST",
        f"/v1/pay/bank-changes/{ticket_id}/callback-number",
        world,
        "approver",
        json=CALLBACK,
    )
    assert denied.status_code == 403
    recorded = await api.ok(
        "POST",
        f"/v1/pay/bank-changes/{ticket_id}/callback-number",
        world,
        "clerk2",
        json={**CALLBACK, "phone": "+971 2 861 1631", "source": "signed_contract"},
    )
    assert recorded["verification"]["callback"]["source"] == "signed_contract"
    assert recorded["verification"]["phone_on_file"] == "+97128611631"
    assert recorded["can_verify"] is False  # clerk2 recorded it (M-04)
    again = await api.call(
        "POST", f"/v1/pay/bank-changes/{ticket_id}/callback-number", world, "clerk2", json=CALLBACK
    )
    assert again.status_code == 409
    vendor_now = await api.ok("GET", f"/v1/pay/vendors/{vendor['id']}", world, "clerk")
    assert vendor_now["phone_on_file"] == "+97128611631"
    by_recorder = await api.call(
        "POST",
        f"/v1/pay/bank-changes/{ticket_id}/calls",
        world,
        "clerk2",
        json={"result": "confirmed", "called_number_on_file": True},
    )
    assert by_recorder.status_code == 403
    verified = await api.call(
        "POST",
        f"/v1/pay/bank-changes/{ticket_id}/calls",
        world,
        "finance",
        json={"result": "confirmed", "called_number_on_file": True},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["verification"]["calls"][-1]["number_last4"] == "1631"
    actions = await audit_actions(owner_engine, world.org_id, ticket_id)
    assert "pay.bank_change.callback_recorded" in actions

    # The final action refuses an onboarding ticket without a source, whatever happened.
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE pay_bank_change_tickets SET callback_source = NULL, callback_reference = NULL, "
        "callback_recorded_by = NULL WHERE id = :t",
        {"t": ticket_id},
    )
    async with await api.in_org(world) as session, session.begin():
        row = await get_ticket(session, uuid.UUID(ticket_id))
        assert row.run_id is not None
        assert row.approval_id is not None
        call = FinalActionCall(
            org_id=world.org_id,
            run_id=row.run_id,
            approval_id=row.approval_id,
            step_key="dual_approval",
            payload={"ticket_id": ticket_id, "details_digest": row.details_digest},
            approver_ids=(world.id("approver"), world.id("approver2")),
        )
        with pytest.raises(ExecutorRefusedError) as caught:
            async with session.begin_nested():
                await approve_bank_change(session, call)
        assert caught.value.reason == "callback_source_missing"
        await session.rollback()


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
