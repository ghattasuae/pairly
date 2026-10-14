# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay validation and fraud checks (WP4.6) on Postgres, offline.

The ``pay_invoice`` graph runs the checks in ``finish``:

* the demo bank-change invoice (forwarded from a lookalike domain, new bank details,
  hidden instruction) opens a bank-detail change ticket by itself, system opened, the
  vendor's payments held, audited; the risk is critical and the invoice waits for a
  person; injection findings are Pay findings; decision records for the checks, the
  rules, the spend policy check and the risk;
* the near-duplicate demo names its original, and the detail shows both side by side;
* the clean demo stays ``ready`` with a low risk and "no policy found" as information;
* a check that fails, and a policy check that cannot run, fail closed to a person;
* history never crosses organisations; check settings through the settings API.
"""

from __future__ import annotations

import datetime as dt
import re
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker
from pairly_core.integrations.slack.signature import compute
from pairly_core.jobs.registry import JobContext
from pairly_pay import invoice_graph
from pairly_pay.checks import policy as pay_policy
from pairly_pay.invoice_start import start_pending_org

from . import test_pay_invoices as base
from .notify_support import SLACK_SECRET, WEB_URL, notifications, text_of
from .pay_support import PayWorld, audit_actions, owner_rows, vendor_body
from .test_pay_invoices import Api, _address, _email, dataset, demo, vendor_index

# The invoice tests' fixtures: the API with recorded extraction, and two organisations.
api = base.api
world = base.world
other = base.other


def _check(detail: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [c for c in detail["checks"] if c["check"] == name]
    return dict(found)


def _codes(detail: dict[str, Any], name: str) -> set[str]:
    return {f["code"] for f in _check(detail, name)["findings"]}


async def _vendor_for(api: Api, world: PayWorld, doc_id: str, **overrides: Any) -> str:
    truth = dataset().doc(doc_id).record.truth
    assert truth.vendor_id is not None
    created = await api.ok(
        "POST",
        "/v1/pay/vendors",
        world,
        "clerk",
        status=201,
        json=vendor_body(vendor_index(truth.vendor_id), **overrides),
    )
    return str(created["id"])


async def _run_email(api: Api, world: PayWorld, owner: AsyncEngine, email_id: str) -> str:
    address = await _address(api, world, owner)
    intake = await _email(api, address, email_id)
    assert intake.invoice_ids, intake
    ctx = JobContext(
        sessionmaker=create_sessionmaker(api.engine),
        runtime=api.runtime,
        registry=api.runtime.registry,
    )
    await start_pending_org(ctx, world.org_id)
    return str(intake.invoice_ids[0])


# ------------------------------------------------------------------ bank change demo


async def test_bank_change_opens_a_ticket_and_holds_the_vendor(
    api: Api, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    vendor_id = await _vendor_for(api, world, demo("bank_change"))
    invoice_id = await _run_email(api, world, owner_engine, "eml-0003")
    detail = await api.invoice(world, invoice_id)
    assert detail["status"] == "needs_review"
    assert detail["vendor"]["id"] == vendor_id

    bank = _check(detail, "bank")
    assert bank["status"] == "fail"
    (mismatch,) = [f for f in bank["findings"] if f["rule"] == "bank_mismatch"]
    assert mismatch["severity"] == "critical"
    assert mismatch["needs_review"] is True
    assert mismatch["message"]["en"]
    assert mismatch["message"]["ar"]
    ticket_id = mismatch["evidence"]["ticket_id"]

    tickets = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT id, status, source, requested_by_type FROM pay_bank_change_tickets "
        "WHERE vendor_id = :v",
        {"v": vendor_id},
    )
    assert [str(t.id) for t in tickets] == [ticket_id]
    assert tickets[0].status == "awaiting_verification"
    assert tickets[0].source == "invoice"
    assert tickets[0].requested_by_type == "system"
    vendors = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT payment_hold, hold_reason FROM pay_vendors WHERE id = :v",
        {"v": vendor_id},
    )
    assert vendors[0].payment_hold is True
    assert vendors[0].hold_reason == "bank_change"

    actions = await audit_actions(owner_engine, world.org_id, invoice_id)
    assert "pay.invoice.bank_mismatch" in actions
    assert "pay.invoice.checked" in actions
    assert "pay.bank_change.opened" in await audit_actions(owner_engine, world.org_id, ticket_id)
    assert "pay.vendor.hold_placed" in await audit_actions(owner_engine, world.org_id, vendor_id)

    # Forwarded from a lookalike of the vendor's domain.
    assert {"sender_lookalike_domain", "sender_domain_mismatch"} <= _codes(detail, "impersonation")
    # The hidden instruction is a Pay finding too, shown to the reviewer.
    assert _codes(detail, "injection") & {"prompt_injection", "hidden_text"}
    assert detail["findings"]  # the guardrail findings the notice shows

    risk = detail["risk"]
    assert risk["level"] == "critical"
    assert risk["requires_human"] is True
    assert risk["explanation"]["en"]
    assert risk["explanation"]["ar"]
    reasons = detail["review_reasons"]
    assert "check:bank_mismatch" in reasons
    assert "risk:critical" in reasons

    kinds = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT kind FROM decision_records WHERE entity_id = :e",
        {"e": invoice_id},
    )
    assert {"fraud_flag", "risk_score", "policy_check"} <= {k.kind for k in kinds}
    rules = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT source, rule_id, outcome FROM rule_results WHERE subject_id = :e",
        {"e": invoice_id},
    )
    assert {r.source for r in rules} == {"rules", "rag"}
    assert any(r.rule_id == "policy_check" and r.outcome == "no_policy_found" for r in rules)
    policies = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT kind, name, version FROM policy_versions WHERE name = 'pay.invoice'",
    )
    assert {(p.kind, p.version) for p in policies} == {("rules", 1), ("risk_policy", 1)}


def _signed(form: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    body = urlencode(form).encode()
    ts = str(int(time.time()))
    return body, {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": compute(SLACK_SECRET, ts, body),
    }


async def _link_slack(api: Api, world: PayWorld, name: str) -> None:
    """The organisation connects Slack and ``name`` links their Slack account."""
    team = "T" + uuid.uuid4().hex[:10].upper()
    await api.ok(
        "PUT",
        "/v1/integrations/slack",
        world,
        "admin",
        json={"team_id": team, "bot_token": "xoxb-1234567890-test-bot-token"},
    )
    body, headers = _signed(
        {
            "team_id": team,
            "user_id": "U" + uuid.uuid4().hex[:10].upper(),
            "command": "/pairly",
            "text": "connect",
        }
    )
    answer = await api.client.post("/v1/integrations/slack/commands", content=body, headers=headers)
    assert answer.status_code == 200, answer.text
    found = re.search(r"/integrations/slack/connect#([A-Za-z0-9_-]{43})", answer.json()["text"])
    assert found is not None
    linked = await api.client.post(
        "/v1/integrations/slack/connect/confirm",
        json={"token": found.group(1)},
        headers=api.headers(world, name),
    )
    assert linked.status_code == 200, linked.text


async def test_bank_change_alerts_the_approvers_and_finance(
    api: Api, world: PayWorld, owner_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAIRLY_PUBLIC_WEB_URL", WEB_URL)
    await _link_slack(api, world, "approver")
    await _vendor_for(api, world, demo("bank_change"))
    invoice_id = await _run_email(api, world, owner_engine, "eml-0003")
    detail = await api.invoice(world, invoice_id)
    (mismatch,) = [f for f in _check(detail, "bank")["findings"] if f["rule"] == "bank_mismatch"]
    ticket_id = mismatch["evidence"]["ticket_id"]
    last4 = mismatch["evidence"]["printed_last4"]

    rows = await notifications(
        create_sessionmaker(api.engine), world.org_id, template_key="pay.bank_change.alert"
    )
    told = {"approver", "approver2", "finance"}
    by_person: dict[uuid.UUID | None, dict[str, tuple[Any, dict[str, Any]]]] = {}
    for row, content in rows:
        by_person.setdefault(row.recipient_user_id, {})[row.channel] = (row, content)
    # The approvers and the finance controller, nobody else (not the clerks, the admin,
    # the auditor or the recruiter).
    assert set(by_person) == {world.id(n) for n in told}
    for name in told:
        channels = by_person[world.id(name)]
        assert set(channels) == {"email", "slack", "in_app"}
        for row, _ in channels.values():
            assert row.category == "urgent"
            assert row.urgency == "urgent"
        email_row, email = channels["email"]
        assert email_row.status == "queued"
        assert f"{WEB_URL}/pay/bank-changes/{ticket_id}" in email["text"]
        assert email["link"] == f"/pay/bank-changes/{ticket_id}"
        assert last4 in email["text"]
        # Never more of the account number than its last four characters.
        assert not re.search(r"[A-Z]{2}[0-9]{2}[A-Z0-9 ]{8,}", text_of(email))
        slack_row, _ = channels["slack"]
        expected = "queued" if name == "approver" else "suppressed"
        assert slack_row.status == expected, (name, slack_row.status, slack_row.reason)
    # The bell: an unread urgent item that opens the ticket.
    feed = await api.ok("GET", "/v1/notifications", world, "finance")
    (item,) = [i for i in feed["items"] if i["category"] == "urgent"]
    assert item["link"] == f"/pay/bank-changes/{ticket_id}"
    assert item["urgency"] == "urgent"
    assert last4 in item["body"]
    clerk_feed = await api.ok("GET", "/v1/notifications", world, "clerk")
    assert not [i for i in clerk_feed["items"] if i["category"] == "urgent"]


async def test_bank_mismatch_without_phone_holds_the_vendor_anyway(
    api: Api, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    vendor_id = await _vendor_for(api, world, demo("bank_change"), phone_on_file=None)
    invoice_id = await _run_email(api, world, owner_engine, "eml-0003")
    detail = await api.invoice(world, invoice_id)
    bank = _check(detail, "bank")
    assert {f["rule"] for f in bank["findings"]} >= {"bank_mismatch", "bank_ticket_failed"}
    vendors = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT payment_hold, hold_reason FROM pay_vendors WHERE id = :v",
        {"v": vendor_id},
    )
    assert vendors[0].payment_hold is True
    assert vendors[0].hold_reason == "manual"
    assert detail["status"] == "needs_review"


# ------------------------------------------------------------------ duplicates


async def _original(
    api: Api, world: PayWorld, owner: AsyncEngine, vendor_id: str, near_doc: str
) -> str:
    """An earlier invoice row of the vendor, as the near duplicate's original was read."""
    original_id = dataset().doc(near_doc).record.attack.original_id  # type: ignore[union-attr]
    truth = dataset().doc(str(original_id)).record.truth
    placeholder = await api.upload_doc(world, demo("clean"))
    await owner_rows(
        owner,
        world.org_id,
        "UPDATE pay_invoices SET status = 'ready', vendor_id = :v, invoice_number = :n, "
        "number_key = :k, invoice_date = :d, total = :t, currency = :c, "
        "created_at = now() - interval '30 days' WHERE id = :i",
        {
            "v": uuid.UUID(vendor_id),
            "n": truth.invoice_number,
            "k": truth.invoice_number.replace("-", "").upper(),
            "d": dt.date.fromisoformat(truth.invoice_date),
            "t": truth.total,
            "c": truth.currency,
            "i": uuid.UUID(placeholder["id"]),
        },
    )
    return str(placeholder["id"])


async def test_near_duplicate_names_its_original_side_by_side(
    api: Api, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    near = demo("near_duplicate")
    vendor_id = await _vendor_for(api, world, near)
    original = await _original(api, world, owner_engine, vendor_id, near)
    created = await api.upload_doc(world, near)
    detail = await api.invoice(world, created["id"])
    assert detail["status"] == "needs_review"
    dup = _check(detail, "duplicates")
    (finding,) = dup["findings"]
    assert finding["code"] == "near_duplicate"
    assert finding["original_id"] == original
    assert "check:near_duplicate" in detail["review_reasons"]
    (pair,) = detail["compare"]
    assert pair["rules"] == ["near_number"]
    assert pair["original"]["id"] == original
    assert pair["invoice"]["id"] == created["id"]
    assert pair["original"]["invoice_number"] != pair["invoice"]["invoice_number"]
    assert pair["original"]["total"] == pair["invoice"]["total"]
    assert pair["invoice"]["lines"]


async def test_history_never_crosses_organisations(
    api: Api, world: PayWorld, other: PayWorld, owner_engine: AsyncEngine
) -> None:
    near = demo("near_duplicate")
    theirs = await _vendor_for(api, other, near)
    await _original(api, other, owner_engine, theirs, near)
    await _vendor_for(api, world, near)
    created = await api.upload_doc(world, near)
    detail = await api.invoice(world, created["id"])
    assert _check(detail, "duplicates")["status"] == "pass"
    assert detail["compare"] == []
    refused = await api.client.get(
        f"/v1/pay/invoices/{created['id']}", headers=api.headers(other, "clerk")
    )
    assert refused.status_code == 404


# ------------------------------------------------------------------ clean, fail closed


async def test_clean_invoice_stays_ready_with_low_risk(api: Api, world: PayWorld) -> None:
    await _vendor_for(api, world, demo("clean"))
    created = await api.upload_doc(world, demo("clean"))
    detail = await api.invoice(world, created["id"])
    assert detail["status"] == "ready", detail["review_reasons"]
    assert detail["risk"]["level"] == "low"
    assert detail["risk"]["requires_human"] is False
    assert {c["check"] for c in detail["checks"]} == {
        "arithmetic", "duplicates", "bank", "impersonation", "amounts", "forgery",
        "injection", "matching", "policy",
    }  # fmt: skip
    statuses = {c["check"]: c["status"] for c in detail["checks"]}
    assert set(statuses.values()) <= {"pass", "fail"}
    # "No policy found" is information, never a pass.
    assert statuses["policy"] == "fail"
    assert _codes(detail, "policy") == {"no_policy_found"}
    # No purchase orders kept in Pairly: not matched (information), a person approves.
    assert statuses["matching"] == "fail"
    assert _codes(detail, "matching") == {"no_po"}
    assert detail["compare"] == []


async def test_a_failing_check_sends_the_invoice_to_a_person(
    api: Api, world: PayWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _vendor_for(api, world, demo("clean"))

    async def broken(*_: Any) -> list[str]:
        raise RuntimeError("down")

    monkeypatch.setattr(invoice_graph, "CHECKS", [broken])
    created = await api.upload_doc(world, demo("clean"))
    detail = await api.invoice(world, created["id"])
    assert detail["status"] == "needs_review"
    assert "check_failed:RuntimeError" in detail["review_reasons"]


async def test_a_policy_check_that_cannot_run_fails_closed(
    api: Api, world: PayWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _vendor_for(api, world, demo("clean"))

    async def down(*_: Any, **__: Any) -> Any:
        raise ConnectionError("model down")

    monkeypatch.setattr(pay_policy, "check_policy", down)
    created = await api.upload_doc(world, demo("clean"))
    detail = await api.invoice(world, created["id"])
    assert detail["status"] == "needs_review"
    assert _check(detail, "policy")["status"] == "error"
    assert any(r.startswith("check_error:policy") for r in detail["review_reasons"])
    assert detail["risk"]["requires_human"] is True


# ------------------------------------------------------------------ settings


async def test_check_settings_through_the_settings_api(api: Api, world: PayWorld) -> None:
    before = await api.ok("GET", "/v1/pay/settings", world, "admin")
    assert before["checks"]["standard_vat_rate"] == "5.00"
    saved = await api.ok(
        "PUT",
        "/v1/pay/settings",
        world,
        "admin",
        json={
            "cooling_off_hours": 24,
            "checks": {"duplicate_window_days": 21, "approval_threshold": "25000"},
        },
    )
    assert saved["checks"]["duplicate_window_days"] == 21
    assert saved["checks"]["approval_threshold"] == "25000"
    again = await api.ok("GET", "/v1/pay/settings", world, "admin")
    assert again["checks"]["duplicate_window_days"] == 21
    bad = await api.client.put(
        "/v1/pay/settings",
        json={"cooling_off_hours": 24, "checks": {"standard_vat_rate": "-1"}},
        headers=api.headers(world, "admin"),
    )
    assert bad.status_code == 422
