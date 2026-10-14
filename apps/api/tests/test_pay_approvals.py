# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Invoice approvals (WP4.7, brief 4.6.1 and 4.6.2) on Postgres, offline.

* Approve from the web inbox with the evidence (fields, findings, risk, compare); the
  invoice binds what was approved; decision records and audit events.
* Rules route by department, category and risk (amount and vendor risk in the unit
  tests); the rules page lists the active YAML and its versions.
* Segregation of duties: the person who processed or corrected the invoice, or sent it
  for approval, cannot approve it; one person cannot approve two steps.
* Approval by a rule only when the organisation turns it on, never for high risk;
  recorded as a rule decision.
* A held vendor, an open bank-detail change or stale checks block submission, and a
  hold placed while the approval waits makes the final action refuse.
* Email (signed single-use link, confirmation with a second factor) and Slack (the
  buttons open the same web confirmation page; nothing is decided in Slack).
* Rejection and change requests; tenant isolation.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker
from pairly_core.integrations.slack.signature import compute
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.notifications.delivery import Transports, deliver_org
from pairly_core.notifications.email import FakeEmailTransport

from .notify_support import SLACK_SECRET, WEB_URL, link_token, notifications, notify_settings
from .pay_flow_support import (
    Flow,
    approval_row,
    approve_all,
    current_assignees,
    flow,
    fother,
    fworld,
    ready_invoice,
    submit,
)
from .pay_support import PayWorld, audit_actions, other_bank, owner_rows
from .test_pay_invoices import demo

__all__ = ["flow", "fother", "fworld"]  # fixtures

CLEAN = demo("clean")
LINK_RE = re.compile(re.escape(WEB_URL) + r"/approvals/link#[A-Za-z0-9_-]{43}")


async def _settings(flow: Flow, world: PayWorld, **payments: Any) -> None:
    await flow.ok(
        "PUT",
        "/v1/pay/settings",
        world,
        "admin",
        json={"cooling_off_hours": 0, "payments": payments},
    )


# ------------------------------------------------------------------ web inbox


async def test_approve_from_the_inbox_with_evidence(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_id, vendor_id = await ready_invoice(flow, fworld, CLEAN)
    submitted = await submit(flow, fworld, invoice_id)
    assert submitted["status"] == "awaiting_approval"
    approval_id = submitted["approval"]["approval_id"]
    assert approval_id is not None
    assert submitted["approval"]["submitted_by"] == str(fworld.id("clerk"))

    # The approver sees the evidence in the inbox.
    detail = await flow.ok("GET", f"/v1/approvals/{approval_id}", fworld, "approver")
    context = detail["context"]
    assert context["kind"] == "pay.invoice"
    assert context["invoice_path"] == f"/pay/invoices/{invoice_id}"
    assert context["document_id"] == submitted["document_id"]
    assert context["fields"]["total"] == submitted["total"]
    assert context["risk"]["level"] == "low"
    assert {"checks", "findings", "compare", "vendor", "bank"} <= set(context)
    assert any(f["code"] == "no_policy_found" for f in context["findings"])
    payload = detail["payload"]
    assert payload["invoice_id"] == invoice_id
    assert payload["vendor_id"] == vendor_id
    assert payload["risk_level"] == "low"
    row = await approval_row(owner_engine, fworld, approval_id)
    assert row.chain_name == "standard"
    # Who processed it is a preparer.
    assert str(fworld.id("clerk")) in [str(p) for p in row.preparer_ids]

    # A clerk cannot decide; an approver without a second factor cannot either.
    assert (await flow.decide(fworld, "clerk", approval_id)).status_code == 403
    assert (await flow.decide(fworld, "approver", approval_id, otp=False)).status_code == 403
    # Nothing about the invoice can change while it waits.
    blocked = await flow.call(
        "POST",
        f"/v1/pay/invoices/{invoice_id}/corrections",
        fworld,
        "clerk",
        json={"field_path": "total", "new_value": "1.00", "reason": "x"},
    )
    assert blocked.status_code == 409

    approved = await flow.decide(fworld, "approver", approval_id)
    assert approved.status_code == 200, approved.text
    done = await flow.invoice(fworld, invoice_id)
    assert done["status"] == "approved"
    assert done["approval"]["approver_ids"] == [str(fworld.id("approver"))]
    assert done["approval"]["approved_by_rule"] is False
    assert done["approval"]["approved_amount"] == done["total"]
    assert done["approval"]["approved_account_id"] is not None
    actions = await audit_actions(owner_engine, fworld.org_id, invoice_id)
    assert {
        "pay.invoice.submitted_for_approval",
        "pay.invoice.awaiting_approval",
        "pay.invoice.approved",
    } <= set(actions)
    kinds = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT kind, final_action FROM decision_records WHERE entity_id = :e ORDER BY decided_at",
        {"e": invoice_id},
    )
    assert "approval" in [k.kind for k in kinds]
    assert any(k.final_action == "pay.invoice.submitted_for_approval" for k in kinds)
    assert any(
        k.kind == "final_action" and k.final_action and "approve_invoice" in k.final_action
        for k in kinds
    )


async def test_rules_route_by_department_category_and_risk(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    # Department: marketing goes to a budget owner (an approver, one step).
    invoice_id, _ = await ready_invoice(flow, fworld, CLEAN)
    await flow.ok(
        "POST",
        f"/v1/pay/invoices/{invoice_id}/coding",
        fworld,
        "clerk",
        json={"department": "marketing", "category": None},
    )
    await flow.ok("POST", f"/v1/pay/invoices/{invoice_id}/review", fworld, "clerk2", json={})
    submitted = await submit(flow, fworld, invoice_id)
    row = await approval_row(owner_engine, fworld, submitted["approval"]["approval_id"])
    assert row.chain_name == "marketing_budget"
    assert row.payload["department"] == "marketing"
    assignees = await current_assignees(owner_engine, fworld, str(row.id))
    assert str(fworld.id("finance")) not in assignees  # approvers only

    # Category: capital spend needs an approver and then a finance controller.
    second, _ = await ready_invoice(flow, fworld, "inv-0030")
    await flow.ok(
        "POST",
        f"/v1/pay/invoices/{second}/coding",
        fworld,
        "clerk",
        json={"department": None, "category": "it_hardware"},
    )
    await flow.ok("POST", f"/v1/pay/invoices/{second}/review", fworld, "clerk2", json={})
    sub2 = await submit(flow, fworld, second)
    row2 = await approval_row(owner_engine, fworld, sub2["approval"]["approval_id"])
    assert row2.chain_name == "capital_spend"

    # Risk: a high risk invoice goes to the high risk chain, whatever its amount.
    third, _ = await ready_invoice(flow, fworld, "inv-0037")
    await owner_rows(
        owner_engine,
        fworld.org_id,
        "UPDATE pay_invoices SET risk = jsonb_set(jsonb_set(risk, '{level}', '\"high\"'), "
        "'{requires_human}', 'true') WHERE id = :i",
        {"i": third},
    )
    sub3 = await submit(flow, fworld, third)
    row3 = await approval_row(owner_engine, fworld, sub3["approval"]["approval_id"])
    assert row3.chain_name == "high_risk"
    assert row3.payload["risk_level"] == "high"

    # The rules page shows the active YAML and its versions.
    rules = await flow.ok("GET", "/v1/pay/approval-rules", fworld, "approver")
    by_type = {r["approval_type"]: r for r in rules["items"]}
    assert set(by_type) >= {"pay.invoice", "pay.bank_change"}
    invoice_rules = by_type["pay.invoice"]
    assert invoice_rules["active"]["version"] == 1
    assert "marketing_budget" in invoice_rules["active"]["content"]
    assert [v["version"] for v in invoice_rules["versions"]] == [1]
    assert by_type["pay.bank_change"]["active"] is None
    assert "dual_control" in by_type["pay.bank_change"]["default_content"]


# ------------------------------------------------------------------ segregation of duties


async def test_whoever_processed_or_corrected_it_cannot_approve_and_nobody_twice(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_id, _ = await ready_invoice(flow, fworld, CLEAN)
    # A finance controller corrects a field and codes it (capital spend: two steps).
    await flow.ok(
        "POST",
        f"/v1/pay/invoices/{invoice_id}/corrections",
        fworld,
        "finance",
        json={"field_path": "po_number", "new_value": "PO-7781", "reason": "From the PO"},
    )
    await flow.ok(
        "POST",
        f"/v1/pay/invoices/{invoice_id}/coding",
        fworld,
        "clerk",
        json={"department": None, "category": "it_hardware"},
    )
    await flow.ok("POST", f"/v1/pay/invoices/{invoice_id}/review", fworld, "clerk2", json={})
    submitted = await submit(flow, fworld, invoice_id)
    approval_id = submitted["approval"]["approval_id"]
    row = await approval_row(owner_engine, fworld, approval_id)
    preparers = {str(p) for p in row.preparer_ids}
    assert {str(fworld.id(p)) for p in ("finance", "clerk", "clerk2")} <= preparers
    first_step = await current_assignees(owner_engine, fworld, approval_id)
    assert str(fworld.id("finance")) not in first_step
    denied = await flow.decide(fworld, "finance", approval_id)
    assert denied.status_code == 403, denied.text

    # finance2 approves step one, then may not approve step two as well.
    assert (await flow.decide(fworld, "finance2", approval_id)).status_code == 200
    twice = await flow.decide(fworld, "finance2", approval_id)
    assert twice.status_code == 403, twice.text
    # Step two has only finance controllers, and both are out: nobody can finish it.
    second_step = await current_assignees(owner_engine, fworld, approval_id)
    assert str(fworld.id("finance")) not in second_step
    assert (await flow.invoice(fworld, invoice_id))["status"] == "awaiting_approval"


# ------------------------------------------------------------------ approval by a rule


async def test_approval_by_rule_only_when_enabled_and_never_for_high_risk(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    # Off by default: the invoice goes to a person.
    settings = await flow.ok("GET", "/v1/pay/settings", fworld, "admin")
    assert settings["payments"]["auto_approve_low_risk"] is False
    first_id, _ = await ready_invoice(flow, fworld, "inv-0030", owner=owner_engine, age_days=400)
    first = await submit(flow, fworld, first_id)
    assert first["status"] == "awaiting_approval"

    await _settings(
        flow, fworld, auto_approve_low_risk=True, auto_approve_limit="100000",
        auto_approve_currency="AED",
    )  # fmt: skip
    low, _ = await ready_invoice(flow, fworld, CLEAN, owner=owner_engine, age_days=400)
    assert (await flow.invoice(fworld, low))["risk"]["level"] == "low"
    by_rule = await submit(flow, fworld, low)
    assert by_rule["status"] == "approved"
    assert by_rule["approval"]["approved_by_rule"] is True
    assert by_rule["approval"]["approval_id"] is None
    assert by_rule["approval"]["approver_ids"] == []
    records = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT kind, actor_type FROM decision_records WHERE entity_id = :e "
        "AND final_action = 'pay.invoice.approved_by_rule'",
        {"e": low},
    )
    assert [(r.kind, r.actor_type) for r in records] == [("policy_check", "system")]
    assert "pay.invoice.approved_by_rule" in await audit_actions(owner_engine, fworld.org_id, low)

    # High risk: never approved by a rule, whatever the limit.
    high, _ = await ready_invoice(flow, fworld, "inv-0037", owner=owner_engine, age_days=400)
    await owner_rows(
        owner_engine,
        fworld.org_id,
        "UPDATE pay_invoices SET risk = jsonb_set(risk, '{level}', '\"high\"') WHERE id = :i",
        {"i": high},
    )
    human = await submit(flow, fworld, high)
    assert human["status"] == "awaiting_approval"
    assert human["approval"]["approved_by_rule"] is False
    row = await approval_row(owner_engine, fworld, human["approval"]["approval_id"])
    assert row.chain_name == "high_risk"


# ------------------------------------------------------------------ blocks


async def test_held_vendor_open_ticket_and_stale_checks_block_approval(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_id, vendor_id = await ready_invoice(flow, fworld, CLEAN)
    await flow.ok(
        "POST",
        f"/v1/pay/vendors/{vendor_id}/hold",
        fworld,
        "finance",
        json={"reason": "Checking a complaint."},
    )
    refused = await flow.call(
        "POST", f"/v1/pay/invoices/{invoice_id}/submit", fworld, "clerk", json={}
    )
    assert refused.status_code == 409, refused.text
    assert "vendor_held:manual" in refused.json()["reasons"]
    detail = await flow.invoice(fworld, invoice_id)
    assert "vendor_held:manual" in detail["blockers"]
    await flow.ok(
        "POST",
        f"/v1/pay/vendors/{vendor_id}/hold/release",
        fworld,
        "finance",
        json={"reason": "Resolved."},
    )

    # Stale checks (a change that bypassed Pay) block it too.
    await flow.ok(
        "POST",
        f"/v1/extractions/{detail['extraction']['id']}/corrections",
        fworld,
        "clerk",
        status=201,
        json={"field_path": "invoice_number", "new_value": "INV-X-9", "reason": "Typo"},
    )
    stale = await flow.call(
        "POST", f"/v1/pay/invoices/{invoice_id}/submit", fworld, "clerk", json={}
    )
    assert stale.status_code == 409
    assert "checks_stale" in stale.json()["reasons"]
    await flow.ok("POST", f"/v1/pay/invoices/{invoice_id}/checks", fworld, "clerk")
    current = await flow.invoice(fworld, invoice_id)
    if current["status"] == "needs_review":
        await flow.ok("POST", f"/v1/pay/invoices/{invoice_id}/review", fworld, "clerk2", json={})

    # Submitted, then a bank-detail change opens while the approval waits: the final
    # action refuses and the invoice goes back to a person.
    submitted = await submit(flow, fworld, invoice_id)
    approval_id = submitted["approval"]["approval_id"]
    await flow.ok(
        "POST",
        f"/v1/pay/vendors/{vendor_id}/bank-changes",
        fworld,
        "clerk",
        status=201,
        json={"bank": other_bank(5), "source": "email", "note": "New account letter."},
    )
    assert (await flow.decide(fworld, "approver", approval_id)).status_code == 200
    after = await flow.invoice(fworld, invoice_id)
    assert after["status"] == "needs_review"
    assert any(r.startswith("approval_refused:blocked:") for r in after["review_reasons"])
    reviewed = await flow.call(
        "POST", f"/v1/pay/invoices/{invoice_id}/review", fworld, "clerk2", json={}
    )
    assert reviewed.status_code == 200
    blocked = await flow.call(
        "POST", f"/v1/pay/invoices/{invoice_id}/submit", fworld, "clerk", json={}
    )
    assert blocked.status_code == 409
    assert "bank_change_open" in blocked.json()["reasons"]


# ------------------------------------------------------------------ email and Slack


async def test_approve_from_a_signed_email_link(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_id, _ = await ready_invoice(flow, fworld, CLEAN)
    submitted = await submit(flow, fworld, invoice_id)
    fake = FakeEmailTransport()
    await deliver_org(
        create_sessionmaker(flow.engine),
        fworld.org_id,
        Transports(settings=notify_settings(), email=fake, slack=FakeSlackTransport()),
    )
    (email,) = [m for m in fake.sent if m.to.startswith("approver-")]
    approve_url, reject_url = LINK_RE.findall(email.text)[:2]
    token = link_token(approve_url)
    bound = flow.headers(fworld, "approver", org=False)
    described = await flow.client.post(
        "/v1/approval-links/describe", json={"token": token}, headers=bound
    )
    assert described.status_code == 200, described.text
    assert described.json()["approval_type"] == "pay.invoice"
    assert described.json()["second_factor_required"] is True
    # Without a second factor the confirmation page cannot decide.
    no_otp = await flow.client.post(
        "/v1/approval-links/confirm",
        json={"token": token},
        headers=flow.headers(fworld, "approver", org=False, otp=False),
    )
    assert no_otp.status_code == 403
    confirmed = await flow.client.post(
        "/v1/approval-links/confirm", json={"token": token}, headers=bound
    )
    assert confirmed.status_code == 200, confirmed.text
    done = await flow.invoice(fworld, invoice_id)
    assert done["status"] == "approved"
    channels = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT channel FROM approval_decisions WHERE approval_id = :a",
        {"a": submitted["approval"]["approval_id"]},
    )
    assert [c.channel for c in channels] == ["email_link"]
    # Single use: the Reject link of the same email is dead now.
    dead = await flow.client.post(
        "/v1/approval-links/confirm", json={"token": link_token(reject_url)}, headers=bound
    )
    assert dead.status_code == 410


def _signed(form: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    body = urlencode(form).encode()
    ts = str(int(time.time()))
    return body, {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": compute(SLACK_SECRET, ts, body),
    }


async def _slack(flow: Flow, path: str, form: dict[str, str]) -> Any:
    body, headers = _signed(form)
    response = await flow.client.post(
        f"/v1/integrations/slack/{path}", content=body, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_slack_approve_opens_the_web_confirmation_page(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    team = "T" + uuid.uuid4().hex[:10].upper()
    configured = await flow.call(
        "PUT",
        "/v1/integrations/slack",
        fworld,
        "admin",
        json={
            "team_id": team,
            "bot_token": "xoxb-1234567890-test-bot-token",
            "channel_mappings": {"sla": "C0TEST01"},
        },
    )
    assert configured.status_code == 200, configured.text
    slack_user = "U" + uuid.uuid4().hex[:10].upper()
    answer = await _slack(
        flow,
        "commands",
        {"team_id": team, "user_id": slack_user, "command": "/pairly", "text": "connect"},
    )
    connect = re.search(r"/integrations/slack/connect#([A-Za-z0-9_-]{43})", answer["text"])
    assert connect is not None
    linked = await flow.client.post(
        "/v1/integrations/slack/connect/confirm",
        json={"token": connect.group(1)},
        headers=flow.headers(fworld, "approver"),
    )
    assert linked.status_code == 200, linked.text

    invoice_id, _ = await ready_invoice(flow, fworld, CLEAN)
    submitted = await submit(flow, fworld, invoice_id)
    approval_id = submitted["approval"]["approval_id"]
    ((_, content),) = [
        (r, c)
        for r, c in await notifications(
            create_sessionmaker(flow.engine), fworld.org_id, channel="slack"
        )
        if r.recipient_user_id == fworld.id("approver")
    ]
    (actions,) = [b for b in content["blocks"] if b["type"] == "actions"]
    buttons = {e["action_id"]: e for e in actions["elements"]}
    approve_token = buttons["approve"]["value"]
    assert buttons["approve"]["url"] == f"{WEB_URL}/approvals/link#{approve_token}"

    click = {
        "type": "block_actions",
        "team": {"id": team},
        "user": {"id": slack_user},
        "response_url": "https://hooks.slack.com/actions/T0/1/test",
        "actions": [
            {
                "action_id": "approve",
                "action_ts": f"{int(time.time())}.{uuid.uuid4().int % 10**6:06d}",
                "value": approve_token,
            }
        ],
    }
    outcome = await _slack(flow, "interactions", {"payload": json.dumps(click)})
    assert outcome["outcome"] == "opened_web"
    # Nothing was decided in Slack.
    assert (await approval_row(owner_engine, fworld, approval_id)).status == "pending"
    # The web confirmation page decides, with the second factor.
    confirmed = await flow.client.post(
        "/v1/approval-links/confirm",
        json={"token": approve_token},
        headers=flow.headers(fworld, "approver", org=False),
    )
    assert confirmed.status_code == 200, confirmed.text
    assert (await flow.invoice(fworld, invoice_id))["status"] == "approved"


# ------------------------------------------------------------------ reject, changes


async def test_reject_and_request_changes(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_id, _ = await ready_invoice(flow, fworld, CLEAN)
    sub = await submit(flow, fworld, invoice_id)
    changes = await flow.decide(
        fworld, "approver", sub["approval"]["approval_id"], "request_changes", "Wrong PO."
    )
    assert changes.status_code == 200, changes.text
    back = await flow.invoice(fworld, invoice_id)
    assert back["status"] == "needs_review"
    assert "changes_requested_by_approver" in back["review_reasons"]

    await flow.ok("POST", f"/v1/pay/invoices/{invoice_id}/review", fworld, "clerk2", json={})
    again = await submit(flow, fworld, invoice_id)
    assert again["approval"]["approval_id"] != sub["approval"]["approval_id"]
    rejected = await flow.decide(
        fworld, "approver2", again["approval"]["approval_id"], "reject", "Not our order."
    )
    assert rejected.status_code == 200, rejected.text
    closed = await flow.invoice(fworld, invoice_id)
    assert closed["status"] == "rejected"
    assert "pay.invoice.approval_rejected" in await audit_actions(
        owner_engine, fworld.org_id, invoice_id
    )


async def test_tenant_isolation(
    flow: Flow, fworld: PayWorld, fother: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_id, _ = await ready_invoice(flow, fworld, CLEAN)
    foreign = await flow.call(
        "POST", f"/v1/pay/invoices/{invoice_id}/submit", fother, "clerk", json={}
    )
    assert foreign.status_code == 404
    sub = await submit(flow, fworld, invoice_id)
    approval_id = sub["approval"]["approval_id"]
    assert (await flow.decide(fother, "approver", approval_id)).status_code == 404
    used = await approve_all(flow, owner_engine, fworld, approval_id)
    assert used == ["approver"]
