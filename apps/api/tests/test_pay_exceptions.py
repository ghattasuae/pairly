# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The exceptions queue and drafted messages (brief 4.5.4) on Postgres, offline.

* The queue lists invoices waiting for a person, oldest first, by category (price
  variance, missing receipt, no PO), with the checks' own sentences, a resolution, the
  message templates that fit, the SLA age and counts; it filters by category and text;
  another organisation sees nothing.
* A drafted message to the vendor uses a template plus the recorded personal paragraph;
  it goes only to a contact on file, waits for another person's approval of the exact
  text, and is queued through the engine's sensitive template only after it.
* A message to the requester goes only to a member; a declined message is never sent.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker, set_tenant
from pairly_pay.ai_recordings import import_ai_recordings

from .pay_flow_support import Flow, flow, fother, fworld, make_vendor, seed_po, upload
from .pay_support import PayWorld, audit_actions, owner_rows
from .test_pay_invoices import demo

__all__ = ["flow", "fother", "fworld"]  # fixtures

CLEAN = demo("clean")


def _categories(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["category"]: c for c in item["categories"]}


async def _recordings(owner: AsyncEngine, world: PayWorld) -> None:
    session = create_sessionmaker(owner)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            await import_ai_recordings(session, world.org_id)


async def _missing_receipt(flow: Flow, world: PayWorld) -> str:
    vendor = await make_vendor(flow, world, "inv-0001")
    await seed_po(flow, world, "inv-0001", vendor, receipts=False)
    invoice_id = await upload(flow, world, "inv-0001")
    detail = await flow.invoice(world, invoice_id)
    assert "check:missing_receipt" in detail["review_reasons"]
    return invoice_id


async def _email(owner: AsyncEngine, world: PayWorld, name: str) -> str:
    (row,) = await owner_rows(
        owner, world.org_id, "SELECT email FROM users WHERE id = :u", {"u": world.id(name)}
    )
    return str(row.email)


async def test_the_queue_categorises_explains_and_counts(
    flow: Flow, fworld: PayWorld, fother: PayWorld, owner_engine: AsyncEngine
) -> None:
    clean_vendor = await make_vendor(flow, fworld, CLEAN)
    await seed_po(flow, fworld, CLEAN, clean_vendor)
    vendor = await make_vendor(flow, fworld, "inv-0037")
    await seed_po(flow, fworld, "inv-0037", vendor)
    price = await upload(flow, fworld, "inv-0037")
    missing = await _missing_receipt(flow, fworld)
    await make_vendor(flow, fworld, "inv-0030")
    unknown = await upload(flow, fworld, "inv-0030")
    await owner_rows(
        owner_engine, fworld.org_id,
        "UPDATE pay_invoices SET created_at = now() - interval '3 days' WHERE id = :i",
        {"i": missing},
    )  # fmt: skip

    queue = await flow.ok("GET", "/v1/pay/exceptions", fworld, "clerk")
    assert queue["can_message"] is True
    assert queue["sla_hours"] == 48
    by_id = {item["id"]: item for item in queue["items"]}
    assert queue["items"][0]["id"] == missing  # oldest first
    assert by_id[missing]["overdue"] is True
    assert by_id[missing]["age_hours"] >= 72
    assert by_id[price]["overdue"] is False

    receipt = _categories(by_id[missing])["missing_receipt"]
    assert receipt["explanations"][0]["en"]
    assert receipt["explanations"][0]["ar"]
    assert "goods receipt" in receipt["resolution"]["en"]
    assert {t["id"] for t in receipt["templates"]} == {"delivery_note", "record_receipt"}
    assert "price_variance" in _categories(by_id[price])
    assert "no_po" in _categories(by_id[unknown])
    assert queue["counts"]["missing_receipt"] >= 1
    assert queue["counts"]["price_variance"] >= 1
    assert queue["counts"]["no_po"] >= 1
    assert set(queue["counts"]) >= {"bank_mismatch", "low_confidence", "other"}

    only = await flow.ok("GET", "/v1/pay/exceptions?category=no_po", fworld, "clerk")
    assert [i["id"] for i in only["items"]] == [unknown]
    reference = by_id[price]["reference"]
    found = await flow.ok("GET", f"/v1/pay/exceptions?q={reference}", fworld, "clerk")
    assert [i["id"] for i in found["items"]] == [price]

    auditor = await flow.ok("GET", "/v1/pay/exceptions", fworld, "auditor")
    assert auditor["can_message"] is False
    assert (await flow.ok("GET", "/v1/pay/exceptions", fother, "clerk"))["items"] == []


async def test_a_vendor_message_is_sent_only_after_another_person_approves(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    await _recordings(owner_engine, fworld)
    invoice_id = await _missing_receipt(flow, fworld)
    draft = await flow.ok(
        "POST", f"/v1/pay/invoices/{invoice_id}/messages/draft", fworld, "clerk",
        json={"category": "missing_receipt", "template_id": "delivery_note", "personalise": True},
    )  # fmt: skip
    assert draft["recipient"] == "vendor"
    assert draft["ai_note"], draft["ai_problems"]
    assert draft["from_recording"] is True
    assert "PO-2026-0001" in draft["message"]
    assert draft["message"].endswith(draft["ai_note"])
    assert draft["contacts"]
    contact = draft["contacts"][0]["email"]
    body = {
        "category": "missing_receipt",
        "template_id": "delivery_note",
        "message": draft["message"],
        "recipient_email": contact,
        "ai_note": True,
        "model_call_id": draft["model_call_id"],
    }
    path = f"/v1/pay/invoices/{invoice_id}/messages"

    # Only a contact on file, and no em dash.
    typed = await flow.call(
        "POST", path, fworld, "clerk", json={**body, "recipient_email": "ap@lookalike.test"}
    )
    assert typed.status_code == 400
    assert typed.json()["code"] == "recipient_not_allowed"
    dash = await flow.call("POST", path, fworld, "clerk", json={**body, "message": "Hi — x"})
    assert dash.status_code == 400
    # A category the invoice does not have is refused.
    other = await flow.call(
        "POST", path, fworld, "clerk",
        json={**body, "category": "no_po", "template_id": "po_missing"},
    )  # fmt: skip
    assert other.status_code == 400

    sent = await flow.ok("POST", path, fworld, "clerk", status=201, json=body)
    assert sent["status"] == "awaiting_approval"
    assert sent["recipient_email"] == contact
    approval_id = sent["approval_id"]
    (approval,) = await owner_rows(
        owner_engine, fworld.org_id,
        "SELECT approval_type, payload, preparer_ids FROM approvals WHERE id = :a",
        {"a": approval_id},
    )  # fmt: skip
    assert approval.approval_type == "pay.message"
    assert draft["ai_note"] in approval.payload["text"]
    assert approval.payload["template_key"] == "pay.vendor.message"

    # The writer never approves their own message; nothing is queued before the approval.
    own = await flow.decide(fworld, "clerk", approval_id)
    assert own.status_code != 200
    queued = "SELECT template_key, status FROM notifications WHERE approval_id = :a"
    assert await owner_rows(owner_engine, fworld.org_id, queued, {"a": approval_id}) == []

    decided = await flow.decide(fworld, "approver", approval_id)
    assert decided.status_code == 200, decided.text
    (notification,) = await owner_rows(owner_engine, fworld.org_id, queued, {"a": approval_id})
    assert notification.template_key == "pay.vendor.message"
    (message,) = await flow.ok("GET", path, fworld, "clerk")
    assert message["status"] == "queued"
    assert message["sent_at"] is not None
    actions = await audit_actions(owner_engine, fworld.org_id, message["id"])
    assert actions[:3] == [
        "pay.message.submitted",
        "pay.message.awaiting_approval",
        "pay.message.sent",
    ]
    # Readers without the message permission see the status, not the text.
    (hidden,) = await flow.ok("GET", path, fworld, "auditor")
    assert (hidden["message"], hidden["recipient_email"]) == (None, None)


async def test_a_requester_message_goes_to_a_member_and_a_declined_one_is_never_sent(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_id = await _missing_receipt(flow, fworld)
    draft = await flow.ok(
        "POST", f"/v1/pay/invoices/{invoice_id}/messages/draft", fworld, "clerk",
        json={"category": "missing_receipt", "template_id": "record_receipt", "locale": "ar"},
    )  # fmt: skip
    assert draft["recipient"] == "requester"
    assert draft["ai_note"] is None
    assert "Pairly" in draft["message"]
    path = f"/v1/pay/invoices/{invoice_id}/messages"
    body = {
        "category": "missing_receipt",
        "template_id": "record_receipt",
        "locale": "ar",
        "message": draft["message"],
        "recipient_email": "someone@outside.test",
    }
    outside = await flow.call("POST", path, fworld, "clerk", json=body)
    assert outside.status_code == 400
    member = await _email(owner_engine, fworld, "approver")
    sent = await flow.ok(
        "POST", path, fworld, "clerk", status=201, json={**body, "recipient_email": member.upper()}
    )
    assert sent["recipient_kind"] == "requester"
    declined = await flow.decide(fworld, "finance", sent["approval_id"], "reject")
    assert declined.status_code == 200, declined.text
    (message,) = await flow.ok("GET", path, fworld, "clerk")
    assert message["status"] == "declined"
    rows = await owner_rows(
        owner_engine, fworld.org_id,
        "SELECT id FROM notifications WHERE approval_id = :a",
        {"a": sent["approval_id"]},
    )  # fmt: skip
    assert rows == []
