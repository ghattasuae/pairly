# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Appeals from the vendor portal (brief 2.3.3, 4.7) on Postgres, through the API, offline.

* The portal page of an invoice lists the decisions about it the vendor may ask a person
  to review: its rejection, a duplicate flag, and the staff or system steps of a
  bank-detail change ticket its checks named; never its other check results and never
  the vendor's own request.
* Asking creates an appeal with the vendor as the requester and the contact as the
  audited actor, tells the Pay admins with the deadline (``data_requests``, urgent), and
  is refused a second time. Staff resolve it on the decision's page and the portal shows
  the outcome.
* Isolation: another vendor's decision, another organisation's, or a decision id of no
  invoice of the vendor reads as not found; in the database a vendor portal session sees
  only these decision records and its own vendor's appeals.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker, set_portal_subject, set_tenant
from pairly_core.decisions.models import DecisionRecord
from pairly_pay.portal.appeals import AppealableKind, appealable_kind, ticket_ids_of
from pairly_pay.portal.auth import portal_subject

from .notify_support import WEB_URL, notifications
from .pay_flow_support import Flow, flow, fother, fworld, ready_invoice
from .pay_support import PayWorld, other_bank, owner_rows
from .test_pay_vendor_portal import PORTAL, _grant, _signed_in

__all__ = ["flow", "fother", "fworld"]  # fixtures

REASON = "We sent this invoice once only; please look again."


async def _detail(flow: Flow, headers: dict[str, str], invoice_id: str) -> dict[str, Any]:
    got = await flow.client.get(f"{PORTAL}/invoices/{invoice_id}", headers=headers)
    assert got.status_code == 200, got.text
    return dict(got.json())


async def _ask(flow: Flow, headers: dict[str, str], invoice_id: str, decision_id: str) -> Any:
    return await flow.client.post(
        f"{PORTAL}/invoices/{invoice_id}/appeals",
        json={"decision_id": decision_id, "reason": REASON},
        headers=headers,
    )


async def _rejected(flow: Flow, world: PayWorld, invoice_id: str) -> None:
    await flow.ok(
        "POST",
        f"/v1/pay/invoices/{invoice_id}/reject",
        world,
        "clerk",
        json={"reason": "This invoice was already paid last month."},
    )


async def test_a_vendor_asks_for_a_human_review_of_a_rejection(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine, monkeypatch: Any
) -> None:
    monkeypatch.setenv("PAIRLY_PUBLIC_WEB_URL", WEB_URL)
    invoice_id, vendor_id = await ready_invoice(flow, fworld, "inv-0001")
    headers = await _signed_in(flow, fworld, await _grant(flow, fworld, vendor_id))
    # A clean invoice: its checks passed, so there is nothing to appeal (and the vendor
    # never sees the check results themselves).
    assert (await _detail(flow, headers, invoice_id))["decisions"] == []

    await _rejected(flow, fworld, invoice_id)
    detail = await _detail(flow, headers, invoice_id)
    (decision,) = detail["decisions"]
    assert (decision["kind"], decision["can_appeal"], decision["appeal"]) == (
        "rejection",
        True,
        None,
    )
    asked = await _ask(flow, headers, invoice_id, decision["decision_id"])
    assert asked.status_code == 201, asked.text
    assert asked.headers["cache-control"] == "no-store"
    (after,) = asked.json()["decisions"]
    assert after["can_appeal"] is False
    appeal = after["appeal"]
    assert appeal["status"] == "open"
    assert appeal["reference"].startswith("APL-")
    # Once per decision.
    again = await _ask(flow, headers, invoice_id, decision["decision_id"])
    assert again.status_code == 409, again.text

    # The appeal: the vendor is the requester, the contact the audited actor.
    (row,) = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT requester_type, requester_id, filed_by, status, sla_due_at - created_at AS sla "
        "FROM appeals WHERE id = :a",
        {"a": appeal["id"]},
    )
    assert (row.requester_type, row.requester_id) == ("vendor", f"pay_vendor:{vendor_id}")
    assert row.filed_by is None
    assert row.sla.days == 14
    (event,) = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT actor_type, actor_id, workflow, payload FROM audit_events "
        "WHERE action = 'core.appeal.created' AND entity_id = :a",
        {"a": appeal["id"]},
    )
    assert (event.actor_type, event.workflow) == ("vendor", "pay")
    contacts = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT id::text AS id FROM pay_portal_contacts WHERE vendor_id = :v",
        {"v": vendor_id},
    )
    assert event.actor_id in {c.id for c in contacts}
    assert REASON not in json.dumps(event.payload)  # the vendor's words stay encrypted

    # The Pay admins are told at once with the deadline; nobody else.
    rows = await notifications(
        create_sessionmaker(flow.engine), fworld.org_id, template_key="pay.vendor_portal.appeal"
    )
    assert {r.recipient_user_id for r, _ in rows} == {fworld.id("admin")}
    assert {r.channel for r, _ in rows} == {"email", "in_app"}
    for r, content in rows:
        assert (r.category, r.urgency) == ("data_requests", "urgent")
        assert REASON not in json.dumps(content)
    (mail,) = [c for r, c in rows if r.channel == "email"]
    assert appeal["reference"] in mail["text"]
    assert appeal["sla_due_at"][:10] in mail["text"]
    assert f"{WEB_URL}/decisions/{decision['decision_id']}" in mail["text"]
    feed = await flow.ok("GET", "/v1/notifications", fworld, "admin")
    (item,) = [i for i in feed["items"] if i["category"] == "data_requests"]
    assert item["link"] == f"/decisions/{decision['decision_id']}"

    # A person resolves it on the decision's page; the portal shows the outcome.
    listed = await flow.ok(
        "GET", f"/v1/appeals?decision_id={decision['decision_id']}", fworld, "admin"
    )
    assert [a["id"] for a in listed] == [appeal["id"]]
    resolved = await flow.ok(
        "POST",
        f"/v1/appeals/{appeal['id']}/resolve",
        fworld,
        "finance",
        json={"outcome": "upheld", "outcome_text": "The invoice number was paid on 2 October."},
    )
    assert resolved["status"] == "resolved"
    shown = (await _detail(flow, headers, invoice_id))["decisions"]
    assert [(d["kind"], d["appeal"]["status"], d["appeal"]["outcome"]) for d in shown] == [
        ("rejection", "resolved", "upheld")
    ]


async def test_only_the_vendors_own_decisions_can_be_appealed(
    flow: Flow, fworld: PayWorld, fother: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_a, vendor_a = await ready_invoice(flow, fworld, "inv-0001")
    invoice_b, vendor_b = await ready_invoice(flow, fworld, "inv-0030")
    other_invoice, other_vendor = await ready_invoice(flow, fother, "inv-0001")
    for world, invoice in ((fworld, invoice_a), (fworld, invoice_b), (fother, other_invoice)):
        await _rejected(flow, world, invoice)
    session_a = await _signed_in(flow, fworld, await _grant(flow, fworld, vendor_a))
    session_b = await _signed_in(flow, fworld, await _grant(flow, fworld, vendor_b))
    session_other = await _signed_in(flow, fother, await _grant(flow, fother, other_vendor))
    (decision_b,) = (await _detail(flow, session_b, invoice_b))["decisions"]
    (decision_other,) = (await _detail(flow, session_other, other_invoice))["decisions"]
    (decision_a,) = (await _detail(flow, session_a, invoice_a))["decisions"]

    # Another vendor's decision, through its own invoice or through ours; another
    # organisation's; the vendor's own decision filed under the wrong invoice: not found.
    for invoice, decision in (
        (invoice_b, decision_b["decision_id"]),
        (invoice_a, decision_b["decision_id"]),
        (invoice_a, decision_other["decision_id"]),
        (invoice_b, decision_a["decision_id"]),
        (invoice_a, "dec_not_a_decision"),
    ):
        refused = await _ask(flow, session_a, invoice, decision)
        assert refused.status_code == 404, (invoice, decision, refused.text)
    # The other check records of the invoice (its fraud_flag record passed) are not
    # appealable either.
    flags = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT id FROM decision_records WHERE entity_id = :i AND kind = 'fraud_flag'",
        {"i": invoice_a},
    )
    assert flags
    for flag in flags:
        assert (await _ask(flow, session_a, invoice_a, flag.id)).status_code == 404
    # A staff credential does nothing on the portal route.
    staff = await flow.client.post(
        f"{PORTAL}/invoices/{invoice_a}/appeals",
        json={"decision_id": decision_a["decision_id"], "reason": REASON},
        headers=flow.headers(fworld, "admin"),
    )
    assert staff.status_code == 401
    assert (await _ask(flow, session_a, invoice_a, decision_a["decision_id"])).status_code == 201

    # In the database: vendor B's portal session sees none of vendor A's appeals or
    # decision records; vendor A's sees its appeal and its invoice's rejection only.
    async def rows(
        subject: str | None, sql: str, params: dict[str, Any] | None = None
    ) -> list[Any]:
        session = create_sessionmaker(flow.engine)()
        set_tenant(session, fworld.org_id, None)
        set_portal_subject(session, subject)
        async with session, session.begin():
            return list((await session.execute(text(sql), params or {})).all())

    subject_a = portal_subject(uuid.UUID(vendor_a))
    subject_b = portal_subject(uuid.UUID(vendor_b))
    assert len(await rows(subject_a, "SELECT id FROM appeals")) == 1
    assert await rows(subject_b, "SELECT id FROM appeals") == []
    seen_a = {
        (r.entity_id, r.kind)
        for r in await rows(subject_a, "SELECT entity_id, kind FROM decision_records")
    }
    assert (invoice_a, "final_action") in seen_a
    assert all(entity == invoice_a for entity, _ in seen_a)
    seen_b = await rows(subject_b, "SELECT entity_id FROM decision_records")
    assert invoice_a not in {r.entity_id for r in seen_b}
    # A candidate's portal session sees no vendor appeal.
    assert await rows(f"hire_candidate:{uuid.uuid4()}", "SELECT id FROM appeals") == []
    # A portal session cannot write an appeal for another vendor.
    with pytest.raises(Exception, match="row-level security"):
        await rows(
            subject_a,
            "INSERT INTO appeals (id, org_id, decision_id, requester_type, requester_id, "
            "reason, status, sla_due_at, created_at, updated_at) VALUES (gen_random_uuid(), "
            ":o, :d, 'vendor', :r, 'x', 'open', now(), now(), now())",
            {"o": fworld.org_id, "d": decision_b["decision_id"], "r": f"pay_vendor:{vendor_b}"},
        )
    assert len(await rows(None, "SELECT id FROM appeals")) == 1


async def test_a_bank_change_step_named_by_the_invoice_checks_can_be_appealed(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    """The ticket an invoice's bank check opened or joined: its staff steps (here a
    cancellation) can be appealed from the invoice; the vendor's own request cannot."""
    invoice_id, vendor_id = await ready_invoice(flow, fworld, "inv-0001")
    headers = await _signed_in(flow, fworld, await _grant(flow, fworld, vendor_id))
    asked = await flow.client.post(f"{PORTAL}/bank-change", json=other_bank(), headers=headers)
    assert asked.status_code == 201, asked.text
    (ticket,) = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT id::text AS id FROM pay_bank_change_tickets WHERE vendor_id = :v",
        {"v": vendor_id},
    )
    # As the bank check records it when it joins the open ticket.
    await owner_rows(
        owner_engine,
        fworld.org_id,
        "UPDATE pay_invoices SET checks = CAST(:c AS jsonb) WHERE id = :i",
        {
            "c": json.dumps(
                [
                    {
                        "check": "bank",
                        "status": "fail",
                        "findings": [
                            {"rule": "bank_mismatch", "evidence": {"ticket_id": ticket.id}}
                        ],
                    }
                ]
            ),
            "i": invoice_id,
        },
    )
    # The vendor's own request is not something to appeal.
    assert (await _detail(flow, headers, invoice_id))["decisions"] == []
    await flow.ok(
        "POST",
        f"/v1/pay/bank-changes/{ticket.id}/cancel",
        fworld,
        "clerk",
        json={"reason": "The vendor confirmed by phone that nothing changed."},
    )
    (decision,) = (await _detail(flow, headers, invoice_id))["decisions"]
    assert decision["kind"] == "bank_change"
    done = await _ask(flow, headers, invoice_id, decision["decision_id"])
    assert done.status_code == 201, done.text


def _record(**fields: Any) -> DecisionRecord:
    base: dict[str, Any] = {
        "workflow": "pay",
        "entity_type": "pay.invoice",
        "entity_id": "",
        "kind": "final_action",
        "final_action": None,
        "actor_type": "user",
        "output": {},
    }
    base.update(fields)
    return DecisionRecord(**base)


def test_what_a_vendor_may_appeal() -> None:
    invoice = uuid.uuid4()
    ticket = uuid.uuid4()
    mine = str(invoice)
    tickets = [ticket]

    def kind(**fields: Any) -> AppealableKind | None:
        return appealable_kind(_record(**fields), invoice_id=invoice, ticket_ids=tickets)

    assert kind(entity_id=mine, final_action="pay.invoice.rejected") is AppealableKind.REJECTION
    assert (
        kind(entity_id=mine, kind="fraud_flag", output={"checks": {"duplicates": "fail"}})
        is AppealableKind.DUPLICATE_FLAG
    )
    # Other check results stay internal; another invoice's decision is never appealable.
    assert kind(entity_id=mine, kind="fraud_flag", output={"checks": {"bank": "fail"}}) is None
    assert kind(entity_id=str(uuid.uuid4()), final_action="pay.invoice.rejected") is None
    assert kind(entity_id=mine, final_action="pay.invoice.reviewed") is None
    assert kind(entity_id=mine, final_action="pay.invoice.rejected", workflow="hire") is None
    on_ticket = {"entity_type": "pay.bank_change", "entity_id": str(ticket)}
    assert kind(**on_ticket, final_action="bank_change_cancelled") is AppealableKind.BANK_CHANGE
    assert (
        kind(**on_ticket, final_action="bank_change_requested", actor_type="system")
        is AppealableKind.BANK_CHANGE
    )
    assert kind(**on_ticket, final_action="bank_change_requested", actor_type="vendor") is None
    assert kind(**on_ticket, final_action="call_confirmed") is None
    assert (
        kind(
            entity_type="pay.bank_change",
            entity_id=str(uuid.uuid4()),
            final_action="bank_change_rejected",
        )
        is None
    )
    checks = [
        {"check": "duplicates", "findings": [{"evidence": {"ticket_id": str(uuid.uuid4())}}]},
        {"check": "bank", "findings": [{"evidence": {"ticket_id": str(ticket)}}, {"evidence": {}}]},
        {"check": "bank", "findings": [{"evidence": {"ticket_id": "not-a-uuid"}}]},
    ]
    assert ticket_ids_of(checks) == [ticket]
