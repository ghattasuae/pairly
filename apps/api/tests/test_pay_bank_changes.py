# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The bank-detail change workflow (WP4.3, brief 4.1.4) on Postgres, through the API.

* A request opens a locked ticket and holds the vendor's payments at once.
* Out of band: the verifier sees and calls only the number on file; a number the
  request carried is never shown to anyone nor recorded as called.
* Segregation of duties: the requester never verifies; neither the requester nor the
  verifier approves; two different people approve; the final action re-checks it.
* Cooling-off, then activation by the worker job; zero hours activates at once.
* Rejection keeps the old details; a failed call keeps the hold until a finance
  controller releases it; cancelling during cooling-off can keep the hold.
* Every step audited and written as a decision record; tenant isolation; the second
  factor on verify and approve.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.approvals.executor import ExecutorRefusedError
from pairly_core.db import create_sessionmaker
from pairly_core.jobs.registry import JobContext
from pairly_core.workflow.spec import FinalActionCall
from pairly_pay.bank_changes import activate_due, approve_bank_change, get_ticket
from pairly_pay.jobs import activate_due_changes

from .pay_support import (
    PayApi,
    PayWorld,
    audit_actions,
    has_all,
    other,
    other_bank,
    owner_rows,
    pay_api,
    seed_pay_world,
    vendor_body,
    world,
)

__all__ = ["other", "pay_api", "world"]  # fixtures

REQUEST_PHONE = "+971 50 999 0000"


async def _vendor(api: PayApi, world: PayWorld, index: int = 0) -> dict[str, Any]:
    return dict(
        await api.ok("POST", "/v1/pay/vendors", world, "clerk", status=201, json=vendor_body(index))
    )


async def _open(
    api: PayApi, world: PayWorld, vendor: dict[str, Any], bank_index: int = 5, who: str = "clerk"
) -> dict[str, Any]:
    return dict(
        await api.ok(
            "POST",
            f"/v1/pay/vendors/{vendor['id']}/bank-changes",
            world,
            who,
            status=201,
            json={
                "bank": other_bank(bank_index),
                "source": "email",
                "note": "Email from accounts asking to pay a new account from next month.",
                "request_contact": {
                    "name": "A. Caller",
                    "phone": REQUEST_PHONE,
                    "email": "x@lookalike.example",
                },
            },
        )
    )


async def _call(
    api: PayApi,
    world: PayWorld,
    ticket: dict[str, Any],
    result: str,
    who: str = "clerk2",
    **kw: Any,
) -> Any:
    return await api.call(
        "POST",
        f"/v1/pay/bank-changes/{ticket['id']}/calls",
        world,
        who,
        json={
            "result": result,
            "notes": "Spoke to the finance manager.",
            "called_number_on_file": True,
        },
        **kw,
    )


async def _ticket(
    api: PayApi, world: PayWorld, ticket_id: str, who: str = "clerk"
) -> dict[str, Any]:
    return dict(await api.ok("GET", f"/v1/pay/bank-changes/{ticket_id}", world, who))


async def _verified(api: PayApi, world: PayWorld, ticket: dict[str, Any]) -> dict[str, Any]:
    response = await _call(api, world, ticket, "confirmed")
    assert response.status_code == 200, response.text
    body = dict(response.json())
    assert body["status"] == "awaiting_approval", body
    assert body["approval_id"] is not None, body
    return body


async def _settings(api: PayApi, world: PayWorld, hours: int) -> None:
    await api.ok("PUT", "/v1/pay/settings", world, "finance", json={"cooling_off_hours": hours})


async def test_lifecycle_with_cooling_off(
    pay_api: PayApi, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    api = pay_api
    vendor = await _vendor(api, world)
    old_account = vendor["accounts"][0]["id"]
    ticket = await _open(api, world, vendor)
    assert ticket["status"] == "awaiting_verification"
    assert ticket["reference"].startswith("BCT-")
    assert ticket["cooling_off_hours"] == 24
    # The hold is placed at once.
    assert ticket["vendor_payment_hold"] is True
    assert ticket["vendor_hold_reason"] == "bank_change"
    held = await api.ok("GET", f"/v1/pay/vendors/{vendor['id']}", world, "auditor")
    assert held["payment_hold"] is True
    assert held["open_ticket_id"] == ticket["id"]
    # A locked ticket: no second change, no new phone number while it is open.
    again = await api.call(
        "POST",
        f"/v1/pay/vendors/{vendor['id']}/bank-changes",
        world,
        "clerk",
        json={"bank": other_bank(6)},
    )
    assert again.status_code == 409
    assert again.json()["code"] == "bank_change_already_open"
    phone = await api.call(
        "PATCH",
        f"/v1/pay/vendors/{vendor['id']}",
        world,
        "clerk",
        json={"phone_on_file": REQUEST_PHONE},
    )
    assert phone.status_code == 409
    # Out of band: only the number on file, and only to people who verify.
    on_file = vendor["phone_on_file"]
    for who in ("clerk", "clerk2", "approver", "auditor", "finance"):
        seen = await api.call("GET", f"/v1/pay/bank-changes/{ticket['id']}", world, who)
        assert seen.status_code == 200
        text = seen.text
        for fragment in ("999 0000", "9990000", "lookalike.example", "A. Caller"):
            assert fragment not in text, (who, fragment)
        body = seen.json()
        assert body["request_had_contact_details"] is True
        assert body["verification"]["phone_last4"] == on_file[-4:]
        expected_phone = on_file if who in ("clerk", "clerk2", "finance") else None
        assert body["verification"]["phone_on_file"] == expected_phone, who
        assert (body["proposed_iban"] is not None) is (who != "auditor"), who
    # Stored for investigation, encrypted.
    (raw,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT request_claims FROM pay_bank_change_tickets WHERE id = :i",
        {"i": ticket["id"]},
    )
    assert raw.request_claims
    assert "999" not in raw.request_claims

    # Segregation of duties: the requester never verifies; the step needs a second factor.
    mine = await _call(api, world, ticket, "confirmed", who="clerk")
    assert mine.status_code == 403
    assert mine.json()["code"] == "segregation_of_duties"
    no_otp = await _call(api, world, ticket, "confirmed", otp=False)
    assert no_otp.status_code == 403
    assert no_otp.json()["code"] == "second_factor_required"
    unattested = await api.call(
        "POST",
        f"/v1/pay/bank-changes/{ticket['id']}/calls",
        world,
        "clerk2",
        json={"result": "confirmed", "called_number_on_file": False},
    )
    assert unattested.status_code == 400
    # The body cannot carry a number to call.
    sneaky = await api.call(
        "POST",
        f"/v1/pay/bank-changes/{ticket['id']}/calls",
        world,
        "clerk2",
        json={"result": "confirmed", "called_number_on_file": True, "phone": REQUEST_PHONE},
    )
    assert sneaky.status_code == 422
    no_answer = await _call(api, world, ticket, "no_answer")
    assert no_answer.status_code == 200
    assert no_answer.json()["status"] == "awaiting_verification"
    verified = await _verified(api, world, ticket)
    calls = verified["verification"]["calls"]
    assert [c["result"] for c in calls] == ["no_answer", "confirmed"]
    assert {c["number_last4"] for c in calls} == {on_file[-4:]}
    assert verified["verification"]["verified_by"] == str(world.id("clerk2"))

    # The dual approval, in the inbox. Requester and verifier are not approvers.
    approval_id = verified["approval_id"]
    detail = await api.ok("GET", f"/v1/approvals/{approval_id}", world, "approver")
    assert detail["approval_type"] == "pay.bank_change"
    assert detail["payload"]["details_digest"]
    assert len(detail["payload"]["details_digest"]) == 64
    assert other_bank(5)["iban"] not in json.dumps(detail)
    for who in ("clerk", "clerk2"):
        denied = await api.decide(world, who, approval_id)
        assert denied.status_code == 403, who
    no_otp_decision = await api.decide(world, "approver", approval_id, otp=False)
    assert no_otp_decision.status_code == 403
    first = await api.decide(world, "approver", approval_id)
    assert first.status_code == 200, first.text
    twice = await api.decide(world, "approver", approval_id)
    assert twice.status_code in (403, 409)
    assert (await _ticket(api, world, ticket["id"]))["status"] == "awaiting_approval"
    second = await api.decide(world, "approver2", approval_id)
    assert second.status_code == 200, second.text

    cooling = await _ticket(api, world, ticket["id"])
    assert cooling["status"] == "cooling_off", cooling
    assert sorted(cooling["approver_ids"]) == sorted(
        [str(world.id("approver")), str(world.id("approver2"))]
    )
    activates = datetime.fromisoformat(cooling["activates_at"])
    assert timedelta(hours=23) < activates - datetime.now(UTC) <= timedelta(hours=24)
    # Still on hold, still the old details.
    vendor_now = await api.ok("GET", f"/v1/pay/vendors/{vendor['id']}", world, "clerk")
    assert vendor_now["payment_hold"] is True
    assert vendor_now["iban_masked"] == vendor["iban_masked"]
    # Not due yet: nothing happens.
    async with await api.in_org(world) as session, session.begin():
        assert await activate_due(session) == {"activated": 0, "failed": 0}

    # The cooling-off period ends; the worker job activates the new details.
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE pay_bank_change_tickets SET activates_at = now() - interval '1 minute' "
        "WHERE id = :i",
        {"i": ticket["id"]},
    )
    job = JobContext(
        sessionmaker=create_sessionmaker(api.engine),
        runtime=api.app.state.workflow_runtime,
        registry=api.app.state.pairly.registry,
    )
    summary = await activate_due_changes(job)
    assert summary["activated"] >= 1
    assert summary["errors"] == 0
    done = await _ticket(api, world, ticket["id"])
    assert done["status"] == "completed"
    assert done["outcome"] == "activated"
    assert done["hold_kept"] is False
    vendor_done = await api.ok("GET", f"/v1/pay/vendors/{vendor['id']}", world, "clerk")
    assert vendor_done["payment_hold"] is False
    assert vendor_done["hold_reason"] is None
    assert vendor_done["iban_masked"].endswith(other_bank(5)["iban"][-4:])
    statuses = {a["id"]: a["status"] for a in vendor_done["accounts"]}
    assert statuses[old_account] == "retired"
    assert statuses[done["proposed_account"]["id"]] == "active"

    # Audited, and recorded as decision records (ours plus the approvals engine's).
    actions = await audit_actions(owner_engine, world.org_id, ticket["id"])
    assert has_all(
        actions,
        "pay.bank_change.opened",
        "pay.bank_change.call_recorded",
        "pay.bank_change.awaiting_approval",
        "pay.bank_change.approved",
        "pay.bank_change.activated",
    )
    vendor_actions = await audit_actions(owner_engine, world.org_id, vendor["id"])
    assert has_all(
        vendor_actions,
        "pay.vendor.hold_placed",
        "pay.vendor.hold_lifted",
        "pay.vendor.bank_details_changed",
    )
    records = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT id, kind, final_action, actor_id FROM decision_records "
        "WHERE entity_type = 'pay.bank_change' AND entity_id = :e ORDER BY decided_at, id",
        {"e": ticket["id"]},
    )
    kinds = [(r.kind, r.final_action) for r in records]
    for expected in (
        ("final_action", "bank_change_requested"),
        ("final_action", "call_no_answer"),
        ("final_action", "call_confirmed"),
        ("approval", "approve"),
        ("final_action", "bank_details_activated"),
    ):
        assert expected in kinds, (expected, kinds)
    assert sum(1 for k in kinds if k[0] == "approval") == 2
    assert len(done["decision_ids"]) >= 6
    assert set(done["decision_ids"]) == {r.id for r in records}


async def test_zero_cooling_off_and_rejection_by_an_approver(
    pay_api: PayApi, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    api = pay_api
    await _settings(api, world, 0)
    vendor = await _vendor(api, world, 1)
    ticket = await _open(api, world, vendor, bank_index=7)
    verified = await _verified(api, world, ticket)
    assert (await api.decide(world, "finance", verified["approval_id"])).status_code == 200
    assert (await api.decide(world, "approver", verified["approval_id"])).status_code == 200
    done = await _ticket(api, world, ticket["id"])
    assert done["status"] == "completed"
    assert done["activates_at"] is None
    assert done["vendor_payment_hold"] is False

    # A second change, rejected by an approver: old details stay, the hold lifts.
    before = await api.ok("GET", f"/v1/pay/vendors/{vendor['id']}", world, "clerk")
    ticket2 = await _open(api, world, vendor, bank_index=8, who="clerk2")
    # This time clerk2 asked, so clerk verifies.
    verified2 = await _call(api, world, ticket2, "confirmed", who="clerk")
    assert verified2.status_code == 200
    assert verified2.json()["status"] == "awaiting_approval"
    approval = verified2.json()["approval_id"]
    rejected = await api.decide(
        world, "approver2", approval, "reject", "The vendor's letter is unsigned."
    )
    assert rejected.status_code == 200, rejected.text
    closed = await _ticket(api, world, ticket2["id"])
    assert closed["status"] == "rejected"
    assert closed["outcome"] == "rejected_by_approver"
    assert closed["proposed_account"]["status"] == "rejected"
    assert closed["vendor_payment_hold"] is False
    assert closed["hold_kept"] is False
    after = await api.ok("GET", f"/v1/pay/vendors/{vendor['id']}", world, "clerk")
    assert after["iban_masked"] == before["iban_masked"]
    assert "pay.bank_change.rejected" in await audit_actions(
        owner_engine, world.org_id, ticket2["id"]
    )
    assert "recent_rejected_bank_change" in after["risk"]["signals"]


async def test_failed_call_keeps_the_hold_until_released(pay_api: PayApi, world: PayWorld) -> None:
    api = pay_api
    vendor = await _vendor(api, world, 2)
    ticket = await _open(api, world, vendor)
    failed = await _call(api, world, ticket, "not_confirmed")
    assert failed.status_code == 200
    body = failed.json()
    assert body["status"] == "rejected"
    assert body["outcome"] == "verification_failed"
    assert body["vendor_payment_hold"] is True
    assert body["vendor_hold_reason"] == "verification_failed"
    assert body["hold_kept"] is True
    # A later change that completes does not lift a sticky hold.
    await _settings(api, world, 0)
    ticket2 = await _open(api, world, vendor, bank_index=6)
    verified = await _verified(api, world, ticket2)
    for who in ("approver", "approver2"):
        assert (await api.decide(world, who, verified["approval_id"])).status_code == 200
    done = await _ticket(api, world, ticket2["id"])
    assert done["status"] == "completed"
    assert done["vendor_payment_hold"] is True
    assert done["vendor_hold_reason"] == "verification_failed"
    # Only a finance controller (with a second factor) releases it, with a reason.
    path = f"/v1/pay/vendors/{vendor['id']}/hold/release"
    reason = {"reason": "Called the vendor's director on the number on file: confirmed."}
    assert (await api.call("POST", path, world, "approver", json=reason)).status_code == 403
    assert (
        await api.call("POST", path, world, "finance", json=reason, otp=False)
    ).status_code == 403
    released = await api.ok("POST", path, world, "finance", json=reason)
    assert released["payment_hold"] is False
    assert released["hold_reason"] is None


async def test_manual_hold_and_release_refused_while_a_ticket_is_open(
    pay_api: PayApi, world: PayWorld
) -> None:
    api = pay_api
    vendor = await _vendor(api, world, 3)
    held = await api.ok(
        "POST",
        f"/v1/pay/vendors/{vendor['id']}/hold",
        world,
        "finance",
        json={"reason": "Audit query."},
    )
    assert held["payment_hold"] is True
    assert held["hold_reason"] == "manual"
    assert held["hold_sticky"]
    ticket = await _open(api, world, vendor)
    still = await api.ok("GET", f"/v1/pay/vendors/{vendor['id']}", world, "clerk")
    assert still["hold_reason"] == "manual"  # a sticky reason outranks the ticket's
    refused = await api.call(
        "POST",
        f"/v1/pay/vendors/{vendor['id']}/hold/release",
        world,
        "finance",
        json={"reason": "x"},
    )
    assert refused.status_code == 409
    cancelled = await api.ok(
        "POST",
        f"/v1/pay/bank-changes/{ticket['id']}/cancel",
        world,
        "clerk2",
        json={"reason": "Raised by mistake.", "keep_hold": False},
    )
    assert cancelled["status"] == "cancelled"
    assert cancelled["vendor_payment_hold"] is True  # the manual hold stays
    released = await api.ok(
        "POST",
        f"/v1/pay/vendors/{vendor['id']}/hold/release",
        world,
        "finance",
        json={"reason": "Query answered."},
    )
    assert released["payment_hold"] is False


async def test_cancel_paths(pay_api: PayApi, world: PayWorld) -> None:
    api = pay_api
    vendor = await _vendor(api, world, 4)
    ticket = await _open(api, world, vendor)
    # Waiting for the call: people who verify cancel; approvers do not.
    body = {"reason": "Duplicate request.", "keep_hold": False}
    assert (
        await api.call(
            "POST", f"/v1/pay/bank-changes/{ticket['id']}/cancel", world, "approver", json=body
        )
    ).status_code == 403
    cancelled = await api.ok(
        "POST", f"/v1/pay/bank-changes/{ticket['id']}/cancel", world, "clerk2", json=body
    )
    assert cancelled["status"] == "cancelled"
    assert cancelled["vendor_payment_hold"] is False
    assert cancelled["proposed_account"]["status"] == "rejected"
    # During cooling-off an approver may stop it, keeping the hold.
    ticket2 = await _open(api, world, vendor, bank_index=6)
    verified = await _verified(api, world, ticket2)
    for who in ("approver", "finance"):
        assert (await api.decide(world, who, verified["approval_id"])).status_code == 200
    assert (await _ticket(api, world, ticket2["id"]))["status"] == "cooling_off"
    keep = {"reason": "The vendor called back: it never asked for this.", "keep_hold": True}
    assert (
        await api.call(
            "POST", f"/v1/pay/bank-changes/{ticket2['id']}/cancel", world, "clerk2", json=keep
        )
    ).status_code == 403
    stopped = await api.ok(
        "POST", f"/v1/pay/bank-changes/{ticket2['id']}/cancel", world, "approver2", json=keep
    )
    assert stopped["status"] == "cancelled"
    assert stopped["outcome"] == "cancelled_from_cooling_off"
    assert stopped["vendor_payment_hold"] is True
    assert stopped["vendor_hold_reason"] == "cancelled_keep_hold"
    # Cancelled during cooling-off: the new details never became active.
    vendor_now = await api.ok("GET", f"/v1/pay/vendors/{vendor['id']}", world, "clerk")
    assert vendor_now["iban_masked"] == vendor["iban_masked"]
    async with await api.in_org(world) as session, session.begin():
        assert (await activate_due(session, now=datetime.now(UTC) + timedelta(days=3)))[
            "activated"
        ] == 0


async def test_fewer_than_two_possible_approvers_fails_closed(
    pay_api: PayApi, owner_engine: AsyncEngine
) -> None:
    api = pay_api
    small = await seed_pay_world(
        owner_engine,
        {
            "clerk": (("ap_clerk", "pay"),),
            "clerk2": (("ap_clerk", "pay"),),
            "approver": (("approver", "pay"),),
        },
    )
    vendor = await _vendor(api, small)
    ticket = await _open(api, small, vendor)
    response = await _call(api, small, ticket, "confirmed")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed", body
    assert body["outcome"] == "approval_not_possible:fewer_than_two_approvers"
    assert body["vendor_payment_hold"] is True
    assert body["vendor_hold_reason"] == "approval_not_possible"
    assert body["approval_status"] == "cancelled"


async def test_final_action_rechecks_dual_control(pay_api: PayApi, world: PayWorld) -> None:
    """Whatever the approvals engine or the rules say, the final action itself refuses
    one approver, an approver who requested or verified, and changed details."""
    api = pay_api
    vendor = await _vendor(api, world, 5)
    ticket = await _open(api, world, vendor, bank_index=9)
    verified = await _verified(api, world, ticket)
    async with await api.in_org(world) as session, session.begin():
        row = await get_ticket(session, uuid.UUID(ticket["id"]))
        assert row.run_id is not None
        assert row.approval_id is not None
        payload = {"ticket_id": ticket["id"], "details_digest": row.details_digest}

        def call(approvers: tuple[uuid.UUID, ...], **extra: Any) -> FinalActionCall:
            return FinalActionCall(
                org_id=world.org_id,
                run_id=row.run_id,  # type: ignore[arg-type]
                approval_id=row.approval_id,  # type: ignore[arg-type]
                step_key="dual_approval",
                payload={**payload, **extra},
                approver_ids=approvers,
            )

        cases = [
            (call((world.id("approver"),)), "dual_approval_missing"),
            (call((world.id("approver"), world.id("clerk2"))), "requester_or_verifier_approved"),
            (call((world.id("clerk"), world.id("approver"))), "requester_or_verifier_approved"),
            (
                call((world.id("approver"), world.id("approver2")), details_digest="0" * 64),
                "details_changed",
            ),
        ]
        for case, reason in cases:
            with pytest.raises(ExecutorRefusedError) as caught:
                async with session.begin_nested():
                    await approve_bank_change(session, case)
            assert caught.value.reason == reason
    assert verified["status"] == "awaiting_approval"


async def test_tenant_isolation_of_tickets(
    pay_api: PayApi, world: PayWorld, other: PayWorld
) -> None:
    api = pay_api
    vendor = await _vendor(api, world, 6)
    ticket = await _open(api, world, vendor)
    path = f"/v1/pay/bank-changes/{ticket['id']}"
    assert (await api.call("GET", path, other, "admin")).status_code == 404
    assert (await _call(api, other, ticket, "confirmed")).status_code == 404
    cancel = await api.call("POST", f"{path}/cancel", other, "clerk2", json={"reason": "x"})
    assert cancel.status_code == 404
    opened = await api.call(
        "POST",
        f"/v1/pay/vendors/{vendor['id']}/bank-changes",
        other,
        "clerk",
        json={"bank": other_bank(7)},
    )
    assert opened.status_code == 404
    assert (await api.ok("GET", "/v1/pay/bank-changes", other, "admin"))["items"] == []
    assert (await api.call("GET", path, world, "recruiter")).status_code == 403
    # Auditors read tickets, never act on them.
    assert (await api.call("GET", path, world, "auditor")).status_code == 200
    assert (await _call(api, world, ticket, "confirmed", who="auditor")).status_code == 403


async def test_activation_rechecks_and_fails_closed(
    pay_api: PayApi, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    """A tampered ticket (the verifier listed as an approver) never activates: it
    fails, the old details stay and the hold becomes sticky."""
    api = pay_api
    vendor = await _vendor(api, world, 7)
    ticket = await _open(api, world, vendor, bank_index=11)
    verified = await _verified(api, world, ticket)
    for who in ("approver", "approver2"):
        assert (await api.decide(world, who, verified["approval_id"])).status_code == 200
    assert (await _ticket(api, world, ticket["id"]))["status"] == "cooling_off"
    await owner_rows(
        owner_engine,
        world.org_id,
        "UPDATE pay_bank_change_tickets SET activates_at = now() - interval '1 minute', "
        "approver_ids = CAST(:a AS jsonb) WHERE id = :i",
        {"i": ticket["id"], "a": json.dumps([str(world.id("clerk2")), str(world.id("approver"))])},
    )
    async with await api.in_org(world) as session, session.begin():
        assert await activate_due(session) == {"activated": 0, "failed": 1}
    failed = await _ticket(api, world, ticket["id"])
    assert failed["status"] == "failed"
    assert failed["outcome"] == "activation_refused:requester_or_verifier_approved"
    assert failed["vendor_payment_hold"] is True
    assert failed["vendor_hold_reason"] == "approval_not_possible"
    after = await api.ok("GET", f"/v1/pay/vendors/{vendor['id']}", world, "clerk")
    assert after["iban_masked"] == vendor["iban_masked"]
