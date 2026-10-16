# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The vendor portal (brief 4.7, 5.1 item 11) on Postgres, through the API, offline.

* Sign-in: AP gives a contact on file access and the email carries a magic link that
  opens a portal session once; expired, reused and tampered links (another vendor's id,
  another organisation's) fail alike; a revoked contact loses its session; the portal
  session never works on staff routes or on the candidate portal, and staff credentials
  never work on portal routes; asking for a link answers the same for every address.
* Isolation: one vendor's contact never reaches another vendor's invoices, messages or
  details, in the same organisation or another; in the database itself the restrictive
  Row Level Security layer shows a vendor portal session only its own vendor's rows and
  nothing of payment runs, purchase orders or coding, and refuses writes for another
  vendor.
* Contact details change directly (audited as the vendor; a new phone number on file is
  recorded and flags the next bank change); bank details never change: a request opens
  the locked change ticket and holds payments.
* Invoices and credit notes go through Pay intake (source ``vendor_portal``); a credit
  note applied by AP reduces what is paid on its invoice.
* Messages: a vendor writes about its invoice, AP replies in the portal (no approval for
  the default template; an organisation's sensitive version waits for one).
"""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.crypto.blind_index import blind_index_candidates
from pairly_core.db import create_sessionmaker, set_portal_subject, set_tenant
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.notifications.delivery import Transports, deliver_org
from pairly_core.notifications.outbox import OutboxTransport
from pairly_pay.portal.auth import REQUEST_TTL, issue_login_token, portal_subject
from pairly_pay.portal.models import PayPortalContact

from .notify_support import WEB_URL, notifications, notify_settings, text_of
from .pay_flow_support import (
    Flow,
    approve_all,
    flow,
    fother,
    fworld,
    make_vendor,
    ready_invoice,
    submit,
    upload,
)
from .pay_support import PayWorld, other_bank, owner_rows
from .test_pay_invoices import MEDIA, blob, dataset

__all__ = ["flow", "fother", "fworld"]  # fixtures

HEADER = "X-Pairly-Vendor-Portal"
LINK_RE = re.compile(r"/(en|ar)/vendor-portal/login#(vl1\.[A-Za-z0-9._-]+)")
PORTAL = "/v1/pay/vendor-portal"


# ------------------------------------------------------------------ helpers


async def _deliver(flow: Flow, world: PayWorld, outbox: Path) -> list[dict[str, Any]]:
    await deliver_org(
        create_sessionmaker(flow.engine),
        world.org_id,
        Transports(
            settings=notify_settings(), email=OutboxTransport(outbox), slack=FakeSlackTransport()
        ),
    )
    return [json.loads(p.read_text()) for p in sorted(outbox.glob("*.json"))]


def _link(mails: list[dict[str, Any]], email: str) -> str:
    for mail in reversed(mails):
        if email in mail["to"]:
            found = LINK_RE.search(mail["text"])
            if found:
                return found.group(2)
    raise AssertionError(f"no vendor portal link for {email}")


async def _contact_email(flow: Flow, world: PayWorld, vendor_id: str, index: int = 0) -> str:
    vendor = await flow.ok("GET", f"/v1/pay/vendors/{vendor_id}", world, "clerk")
    return str(vendor["contacts"][index]["email"])


async def _grant(flow: Flow, world: PayWorld, vendor_id: str, index: int = 0) -> str:
    email = await _contact_email(flow, world, vendor_id, index)
    await flow.ok(
        "POST",
        f"/v1/pay/vendors/{vendor_id}/portal-contacts",
        world,
        "clerk",
        status=201,
        json={"email": email, "locale": "en"},
    )
    return email


async def _contact(flow: Flow, world: PayWorld, email: str) -> PayPortalContact:
    session = create_sessionmaker(flow.engine)()
    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        row = await session.scalar(
            select(PayPortalContact).where(
                PayPortalContact.email_bidx.in_(
                    blind_index_candidates("email", email, world.org_id)
                ),
                PayPortalContact.status == "active",
            )
        )
        assert row is not None
        session.expunge(row)
        return row


async def _token(flow: Flow, world: PayWorld, email: str, *, now: datetime | None = None) -> str:
    contact = await _contact(flow, world, email)
    session = create_sessionmaker(flow.engine)()
    set_tenant(session, world.org_id, None)
    async with session, session.begin():
        token, _ = await issue_login_token(
            session, contact, purpose="request", ttl=REQUEST_TTL, now=now
        )
    return token


async def _open(flow: Flow, token: str) -> httpx.Response:
    return await flow.client.post(f"{PORTAL}/session", json={"token": token})


async def _signed_in(flow: Flow, world: PayWorld, email: str) -> dict[str, str]:
    opened = await _open(flow, await _token(flow, world, email))
    assert opened.status_code == 200, opened.text
    return {HEADER: opened.json()["session_token"]}


async def _portal_upload(
    flow: Flow,
    headers: dict[str, str],
    doc_id: str,
    *,
    document_type: str = "invoice",
    credit_for: str | None = None,
) -> httpx.Response:
    record = dataset().doc(doc_id).record
    data = {"document_type": document_type}
    if credit_for is not None:
        data["credit_for"] = credit_for
    return await flow.client.post(
        f"{PORTAL}/invoices",
        files={"file": (Path(record.file).name, blob(doc_id), MEDIA[record.format])},
        data=data,
        headers=headers,
    )


def _swap(token: str, index: int, value: uuid.UUID) -> str:
    parts = token.split(".")
    parts[index] = value.hex
    return ".".join(parts)


# ------------------------------------------------------------------ sign-in


async def test_magic_link_is_single_use_bound_and_never_a_staff_credential(
    flow: Flow, fworld: PayWorld, fother: PayWorld, tmp_path: Path
) -> None:
    vendor_a = await make_vendor(flow, fworld, "inv-0001")
    vendor_b = await make_vendor(flow, fworld, "inv-0030")
    email = await _grant(flow, fworld, vendor_a)

    # Only a contact on file can get access.
    refused = await flow.call(
        "POST",
        f"/v1/pay/vendors/{vendor_a}/portal-contacts",
        fworld,
        "clerk",
        json={"email": "someone.else@example.test"},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "contact_not_on_file"
    # The access email carries the link; the API never returns it.
    listed = await flow.ok("GET", f"/v1/pay/vendors/{vendor_a}/portal-contacts", fworld, "clerk")
    assert [c["email"] for c in listed["items"]] == [email]
    assert listed["portal_path"] == f"/vendor-portal?org={fworld.org_id.hex}"
    assert "vl1." not in json.dumps(listed)
    token = _link(await _deliver(flow, fworld, tmp_path), email)

    opened = await _open(flow, token)
    assert opened.status_code == 200, opened.text
    assert opened.headers["cache-control"] == "no-store"
    session = {HEADER: opened.json()["session_token"]}
    assert (await _open(flow, token)).status_code == 404  # single use

    me = await flow.client.get(f"{PORTAL}/me", headers=session)
    assert me.status_code == 200, me.text
    assert me.json()["email"] == email
    assert me.json()["org_id"] == fworld.org_id.hex

    # Expired, tampered (another vendor, another organisation) and garbled links fail
    # alike.
    expired = await _token(flow, fworld, email, now=datetime.now(UTC) - timedelta(hours=1))
    assert (await _open(flow, expired)).status_code == 404
    fresh = await _token(flow, fworld, email)
    assert (await _open(flow, _swap(fresh, 3, uuid.UUID(vendor_b)))).status_code == 404
    assert (await _open(flow, _swap(fresh, 1, fother.org_id))).status_code == 404
    assert (await _open(flow, fresh[:-2] + "xx")).status_code == 404
    assert (await _open(flow, fresh)).status_code == 200  # the untouched link still works

    # The portal session is no staff credential and no candidate portal session.
    bearer = {"Authorization": f"Bearer {session[HEADER]}", "X-Pairly-Org": str(fworld.org_id)}
    for headers in (bearer, {**session, "X-Pairly-Org": str(fworld.org_id)}):
        staff = await flow.client.get("/v1/pay/invoices", headers=headers)
        assert staff.status_code == 401, staff.text
    candidate = await flow.client.get(
        "/v1/hire/portal/me", headers={"X-Pairly-Portal-Session": session[HEADER]}
    )
    assert candidate.status_code == 401
    # A staff credential does nothing on the portal.
    staff_on_portal = await flow.client.get(
        f"{PORTAL}/invoices", headers=flow.headers(fworld, "clerk")
    )
    assert staff_on_portal.status_code == 401

    # Asking for a link: the same answer for a known and an unknown address.
    for address in (email, "nobody@example.test"):
        asked = await flow.client.post(
            f"{PORTAL}/login-link",
            json={"org": fworld.org_id.hex, "email": address, "locale": "en"},
        )
        assert asked.status_code == 202
        assert asked.json() == {"sent": True}

    # Sign-out ends the session; revoking the contact ends the others.
    out = await flow.client.post(f"{PORTAL}/sign-out", headers=session)
    assert out.status_code == 204
    assert (await flow.client.get(f"{PORTAL}/me", headers=session)).status_code == 401
    second = await _signed_in(flow, fworld, email)
    contact = await _contact(flow, fworld, email)
    await flow.ok(
        "POST",
        f"/v1/pay/vendors/{vendor_a}/portal-contacts/{contact.id}/revoke",
        fworld,
        "clerk",
    )
    assert (await flow.client.get(f"{PORTAL}/me", headers=second)).status_code == 401


# ------------------------------------------------------------------ isolation


async def test_a_vendor_sees_only_its_own_invoices_and_threads(
    flow: Flow, fworld: PayWorld, fother: PayWorld
) -> None:
    invoice_a, vendor_a = await ready_invoice(flow, fworld, "inv-0001")
    invoice_b, _ = await ready_invoice(flow, fworld, "inv-0030")
    other_invoice, other_vendor = await ready_invoice(flow, fother, "inv-0001")
    session_a = await _signed_in(flow, fworld, await _grant(flow, fworld, vendor_a))
    session_other = await _signed_in(flow, fother, await _grant(flow, fother, other_vendor))

    listed = (await flow.client.get(f"{PORTAL}/invoices", headers=session_a)).json()
    assert [i["id"] for i in listed] == [invoice_a]
    assert listed[0]["status"] == "in_review"
    assert listed[0]["steps"][0]["step"] == "received"

    for target in (invoice_b, other_invoice):
        assert (
            await flow.client.get(f"{PORTAL}/invoices/{target}", headers=session_a)
        ).status_code == 404
        wrote = await flow.client.post(
            f"{PORTAL}/invoices/{target}/messages", json={"text": "Hello"}, headers=session_a
        )
        assert wrote.status_code == 404
    # A credit note can only name the vendor's own invoice.
    named = await _portal_upload(
        flow, session_a, "inv-0003", document_type="credit_note", credit_for=invoice_b
    )
    assert named.status_code == 400, named.text
    # The other organisation's contact sees its own invoice only.
    theirs = (await flow.client.get(f"{PORTAL}/invoices", headers=session_other)).json()
    assert [i["id"] for i in theirs] == [other_invoice]
    # Details are the vendor's own.
    details = (await flow.client.get(f"{PORTAL}/details", headers=session_a)).json()
    vendor = await flow.ok("GET", f"/v1/pay/vendors/{vendor_a}", fworld, "clerk")
    assert details["reference"] == vendor["reference"]
    assert details["account"]["iban_masked"][-4:] == vendor["accounts"][0]["iban_masked"][-4:]
    iban = str(dataset().doc("inv-0001").record.truth.bank.iban)  # type: ignore[union-attr]
    assert iban not in json.dumps(details)


async def test_row_level_security_limits_a_portal_session_to_its_vendor(
    flow: Flow, fworld: PayWorld
) -> None:
    invoice_a, vendor_a = await ready_invoice(flow, fworld, "inv-0001")
    invoice_b, vendor_b = await ready_invoice(flow, fworld, "inv-0030")
    await _grant(flow, fworld, vendor_a)
    await _grant(flow, fworld, vendor_b)

    async def rows(subject: str | None, sql: str, params: dict[str, Any] | None = None) -> Any:
        session = create_sessionmaker(flow.engine)()
        set_tenant(session, fworld.org_id, None)
        set_portal_subject(session, subject)
        async with session, session.begin():
            return (await session.execute(text(sql), params or {})).all()

    subject_a = portal_subject(uuid.UUID(vendor_a))
    assert [str(r.id) for r in await rows(subject_a, "SELECT id FROM pay_vendors")] == [vendor_a]
    assert [str(r.id) for r in await rows(subject_a, "SELECT id FROM pay_invoices")] == [invoice_a]
    contacts = await rows(subject_a, "SELECT vendor_id FROM pay_portal_contacts")
    assert {str(r.vendor_id) for r in contacts} == {vendor_a}
    accounts = await rows(subject_a, "SELECT vendor_id FROM pay_vendor_bank_accounts")
    assert {str(r.vendor_id) for r in accounts} == {vendor_a}
    for hidden in (
        "pay_payment_runs",
        "pay_purchase_orders",
        "pay_vendor_invites",
        "pay_intake_emails",
        "pay_coding_lists",
        "pay_invoice_line_codings",
        "pay_bank_change_calls",
    ):
        assert await rows(subject_a, f"SELECT 1 FROM {hidden}") == [], hidden  # noqa: S608
    # Staff (no portal subject) see both; a candidate's portal subject sees no Pay row.
    assert len(await rows(None, "SELECT id FROM pay_invoices")) == 2
    candidate = f"hire_candidate:{uuid.uuid4()}"
    assert await rows(candidate, "SELECT id FROM pay_vendors") == []
    assert await rows(candidate, "SELECT id FROM pay_invoices") == []

    # Writes for another vendor are refused by the database itself.
    # An UPDATE of another vendor's row finds nothing to change.
    updated = await rows(
        subject_a,
        "UPDATE pay_vendors SET payment_hold = true WHERE id = :v RETURNING id",
        {"v": vendor_b},
    )
    assert updated == []
    # An INSERT for another vendor is refused.
    with pytest.raises(Exception, match="row-level security"):
        await rows(
            subject_a,
            "INSERT INTO pay_messages (org_id, reference, invoice_id, category, "
            "template_id, recipient_kind, recipient_name, locale, message, status, "
            "channel, direction, vendor_id, portal_contact_id) VALUES (:o, 'MSG-RLS001', "
            ":i, 'portal', 'portal', 'ap', 'x', 'en', 'x', 'received', 'portal', "
            "'inbound', :v, :c) RETURNING id",
            {"o": fworld.org_id, "i": invoice_b, "v": vendor_b, "c": uuid.uuid4()},
        )
    held = await rows(None, "SELECT payment_hold FROM pay_vendors WHERE id = :v", {"v": vendor_b})
    assert held[0].payment_hold is False


async def test_a_portal_bank_change_alerts_the_approvers_and_finance(
    flow: Flow, fworld: PayWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same urgent alert as a ticket an invoice check opens: the approvers and the
    finance controller, every channel, the ticket's page, never more than four digits."""
    monkeypatch.setenv("PAIRLY_PUBLIC_WEB_URL", WEB_URL)
    vendor_id = await make_vendor(flow, fworld, "inv-0001")
    email = await _grant(flow, fworld, vendor_id)
    session = await _signed_in(flow, fworld, email)
    new_bank = other_bank()
    asked = await flow.client.post(f"{PORTAL}/bank-change", json=new_bank, headers=session)
    assert asked.status_code == 201, asked.text
    request = asked.json()["request"]
    last4 = request["iban_last4"]
    tickets = await flow.ok("GET", "/v1/pay/bank-changes", fworld, "clerk")
    items = tickets["items"] if isinstance(tickets, dict) else tickets
    (ticket,) = [t for t in items if t["reference"] == request["reference"]]
    page = f"/pay/bank-changes/{ticket['id']}"

    rows = await notifications(
        create_sessionmaker(flow.engine),
        fworld.org_id,
        template_key="pay.bank_change.portal_alert",
    )
    by_person: dict[uuid.UUID | None, dict[str, tuple[Any, dict[str, Any]]]] = {}
    for row, content in rows:
        by_person.setdefault(row.recipient_user_id, {})[row.channel] = (row, content)
    told = {"approver", "approver2", "finance", "finance2"}
    assert set(by_person) == {fworld.id(n) for n in told}
    for name in told:
        channels = by_person[fworld.id(name)]
        assert set(channels) == {"email", "slack", "in_app"}
        for row, _ in channels.values():
            assert (row.category, row.urgency) == ("urgent", "urgent")
        email_row, mail = channels["email"]
        assert email_row.status == "queued"
        assert f"{WEB_URL}{page}" in mail["text"]
        assert "vendor portal" in mail["text"]
        assert last4 in mail["text"]
        assert new_bank["iban"].replace(" ", "")[:-4] not in text_of(mail).replace(" ", "")
    feed = await flow.ok("GET", "/v1/notifications", fworld, "finance")
    (item,) = [i for i in feed["items"] if i["category"] == "urgent"]
    assert item["link"] == page
    assert last4 in item["body"]
    clerk_feed = await flow.ok("GET", "/v1/notifications", fworld, "clerk")
    assert not [i for i in clerk_feed["items"] if i["category"] == "urgent"]


# ------------------------------------------------------------------ details


async def test_the_portal_never_changes_contacts(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    """Security review 2026-10, L-10. The mailbox attacker of H-01 signs in and adds their
    own address as a contact (or swaps an existing contact's email or phone), so the
    next message AP sends goes to them. The portal shows contacts and changes none."""
    vendor_id = await make_vendor(flow, fworld, "inv-0001")
    session = await _signed_in(flow, fworld, await _grant(flow, fworld, vendor_id))
    before = (await flow.client.get(f"{PORTAL}/details", headers=session)).json()
    assert before["contacts"]
    first = before["contacts"][0]
    attempts = [
        [*before["contacts"], {"name": "Billing", "role": None, "email": "pay@attacker.example"}],
        [{**first, "email": "pay@attacker.example"}, *before["contacts"][1:]],
        [{**first, "phone": "+971 4 555 0199"}, *before["contacts"][1:]],
        [{**first, "name": "Someone Else"}, *before["contacts"][1:]],
        before["contacts"][1:],
        [],
    ]
    for contacts in attempts:
        refused = await flow.client.put(
            f"{PORTAL}/details/contacts", json={"contacts": contacts}, headers=session
        )
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "contacts_by_ap_only"
    after = (await flow.client.get(f"{PORTAL}/details", headers=session)).json()
    assert after["contacts"] == before["contacts"]
    staff = await flow.ok("GET", f"/v1/pay/vendors/{vendor_id}", fworld, "clerk")
    assert "pay@attacker.example" not in [c["email"] for c in staff["contacts"]]
    # The list on file again changes nothing and writes nothing.
    same = await flow.client.put(
        f"{PORTAL}/details/contacts", json={"contacts": before["contacts"]}, headers=session
    )
    assert same.status_code == 200, same.text
    assert same.json()["contacts"] == before["contacts"]
    audit = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT actor_type FROM audit_events WHERE entity_id = :v "
        "AND action = 'pay.vendor.updated'",
        {"v": vendor_id},
    )
    assert [r for r in audit if r.actor_type == "vendor"] == []
    # Accounts payable still changes them.
    added = [*staff["contacts"], {"name": "Sara Nasser", "role": "Accounts", "email": None}]
    await flow.ok(
        "PATCH", f"/v1/pay/vendors/{vendor_id}", fworld, "clerk", json={"contacts": added}
    )
    shown = (await flow.client.get(f"{PORTAL}/details", headers=session)).json()
    assert [c["name"] for c in shown["contacts"]][-1] == "Sara Nasser"


async def test_bank_change_is_only_a_request(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    vendor_id = await make_vendor(flow, fworld, "inv-0001")
    session = await _signed_in(flow, fworld, await _grant(flow, fworld, vendor_id))
    before = (await flow.client.get(f"{PORTAL}/details", headers=session)).json()
    assert before["phone_changed_at"] is None
    assert before["phone_masked"].startswith("••••")

    # Bank details: a request that holds payments, never applied.
    account_before = before["account"]["iban_masked"]
    new_bank = other_bank()
    asked = await flow.client.post(
        f"{PORTAL}/bank-change",
        json={**new_bank, "note": "We moved banks."},
        headers=session,
    )
    assert asked.status_code == 201, asked.text
    request = asked.json()["request"]
    assert request["status"] == "awaiting_verification"
    assert request["source"] == "vendor_portal"
    details = (await flow.client.get(f"{PORTAL}/details", headers=session)).json()
    assert details["account"]["iban_masked"] == account_before
    assert details["payment_hold"] is True
    assert [r["reference"] for r in details["open_requests"]] == [request["reference"]]
    (ticket,) = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT status, source, requested_by_type FROM pay_bank_change_tickets "
        "WHERE vendor_id = :v",
        {"v": vendor_id},
    )
    assert ticket.source == "vendor_portal"
    assert ticket.requested_by_type == "vendor"
    vendor = await flow.ok("GET", f"/v1/pay/vendors/{vendor_id}", fworld, "clerk")
    assert vendor["payment_hold"] is True
    # A second request is refused.
    again = await flow.client.post(f"{PORTAL}/bank-change", json=new_bank, headers=session)
    assert again.status_code == 409


async def test_the_portal_never_chooses_the_call_back_number(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    """Security review 2026-10, H-01. Business email compromise through the portal: the
    attacker holds a vendor contact's mailbox, signs in with a magic link, sets the phone
    number on file to their own, waits out the "recently changed" window and asks for new
    bank details. Accounts payable would then confirm the change by calling the attacker.
    The portal may not change the number on file at all."""
    vendor_id = await make_vendor(flow, fworld, "inv-0001")
    on_file = (await flow.ok("GET", f"/v1/pay/vendors/{vendor_id}", fworld, "clerk"))[
        "phone_on_file"
    ]
    session = await _signed_in(flow, fworld, await _grant(flow, fworld, vendor_id))
    refused = await flow.client.put(
        f"{PORTAL}/details/contacts",
        json={"phone_on_file": "+971 4 555 0199"},
        headers=session,
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "phone_on_file_by_ap_only"
    # Contacts and a new number together: nothing is written.
    mixed = await flow.client.put(
        f"{PORTAL}/details/contacts",
        json={
            "contacts": [{"name": "Mallory", "role": None, "email": None}],
            "phone_on_file": "+971 4 555 0199",
        },
        headers=session,
    )
    assert mixed.status_code == 403
    details = (await flow.client.get(f"{PORTAL}/details", headers=session)).json()
    assert details["phone_changed_at"] is None
    assert "Mallory" not in [c["name"] for c in details["contacts"]]
    # The current number again is accepted (no change, nothing flagged).
    same = await flow.client.put(
        f"{PORTAL}/details/contacts", json={"phone_on_file": on_file}, headers=session
    )
    assert same.status_code == 200, same.text
    assert same.json()["phone_changed_at"] is None

    asked = await flow.client.post(f"{PORTAL}/bank-change", json=other_bank(), headers=session)
    assert asked.status_code == 201, asked.text
    (ticket,) = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT id, phone_recently_changed FROM pay_bank_change_tickets WHERE vendor_id = :v",
        {"v": vendor_id},
    )
    assert ticket.phone_recently_changed is False
    view = await flow.ok("GET", f"/v1/pay/bank-changes/{ticket.id}", fworld, "clerk2")
    assert view["verification"]["phone_on_file"] == on_file


# ------------------------------------------------------------------ submissions


async def test_credit_note_through_intake_reduces_what_is_paid(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_id, vendor_id = await ready_invoice(flow, fworld, "inv-0001")
    session = await _signed_in(flow, fworld, await _grant(flow, fworld, vendor_id))

    sent = await _portal_upload(
        flow, session, "inv-0003", document_type="credit_note", credit_for=invoice_id
    )
    assert sent.status_code == 202, sent.text
    credit = sent.json()["invoice"]
    assert credit["document_type"] == "credit_note"
    assert credit["credit_for"] is not None
    credit_id = credit["id"]
    staff_view = await flow.invoice(fworld, credit_id)
    assert staff_view["source"] == "vendor_portal"
    assert staff_view["vendor"]["id"] == vendor_id
    assert staff_view["status"] == "needs_review"
    assert "credit_note" in staff_view["review_reasons"]
    # A credit note pays nothing: its printed bank details are not compared (no ticket,
    # no hold), every other check ran.
    checks = {c["check"]: c for c in staff_view["checks"]}
    assert checks["bank"]["status"] == "pass"
    assert {"arithmetic", "duplicates"} <= set(checks)
    tickets = await flow.ok("GET", "/v1/pay/bank-changes", fworld, "clerk")
    items = tickets["items"] if isinstance(tickets, dict) else tickets
    assert [t for t in items if t.get("vendor_id") == vendor_id] == []
    # Never sent for approval or paid.
    refused = await flow.call(
        "POST", f"/v1/pay/invoices/{credit_id}/submit", fworld, "clerk", json={"note": "x"}
    )
    assert refused.status_code == 409

    applied = await flow.ok(
        "POST",
        f"/v1/pay/invoices/{credit_id}/apply-credit",
        fworld,
        "clerk2",
        json={"note": "Matches the returned goods."},
    )
    credit_total = Decimal(str(dataset().doc("inv-0003").record.truth.total))
    invoice_total = Decimal(str(dataset().doc("inv-0001").record.truth.total))
    assert applied["status"] == "applied"
    assert applied["invoice_id"] == invoice_id
    assert Decimal(applied["invoice_credit_total"]) == credit_total
    again = await flow.call(
        "POST", f"/v1/pay/invoices/{credit_id}/apply-credit", fworld, "clerk2", json={}
    )
    assert again.status_code == 409

    portal = (await flow.client.get(f"{PORTAL}/invoices", headers=session)).json()
    by_id = {i["id"]: i for i in portal}
    assert by_id[credit_id]["status"] == "applied"
    assert Decimal(by_id[invoice_id]["amount_to_pay"]) == invoice_total - credit_total

    sub = await submit(flow, fworld, invoice_id)
    await approve_all(flow, owner_engine, fworld, sub["approval"]["approval_id"])
    preview = await flow.ok(
        "POST",
        "/v1/pay/payment-runs/preview",
        fworld,
        "finance",
        json={"currency": "AED", "execution_date": "2026-10-20", "cash_limit": "1000000"},
    )
    (item,) = [i for i in preview["items"] if i["invoice_id"] == invoice_id]
    assert Decimal(item["amount"]) == invoice_total - credit_total
    assert credit_id not in {i["invoice_id"] for i in preview["items"]}
    # Analytics: the open payable is the approved amount less the applied credit.
    analytics = await flow.ok("GET", "/v1/pay/analytics", fworld, "finance")
    (aed,) = [m for m in analytics["open_payables"] if m["key"] == "open_payables.AED"]
    assert Decimal(aed["detail"]["amount"]) == invoice_total - credit_total
    assert Decimal(aed["detail"]["gross"]) == invoice_total
    assert Decimal(aed["detail"]["credits_applied"]) == credit_total
    assert {r["id"] for r in aed["records"]} == {invoice_id}
    approved = {
        i["id"]: i for i in (await flow.client.get(f"{PORTAL}/invoices", headers=session)).json()
    }
    assert approved[invoice_id]["status"] == "approved"
    assert approved[invoice_id]["expected_payment_date"] is not None

    # The journal: the applied credit note is the invoice entry reversed, balanced, and the
    # payables it takes off are what it reduced the payment by.
    period = datetime.now(UTC).strftime("%Y-%m")
    exported = await flow.call(
        "POST", "/v1/pay/journal/export", fworld, "finance", json={"period": period}
    )
    assert exported.status_code == 200, exported.text
    rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8"))))
    debit: dict[str, Decimal] = {}
    credit_side: dict[str, Decimal] = {}
    for r in rows:
        debit[r["entry"]] = debit.get(r["entry"], Decimal(0)) + Decimal(r["debit"])
        credit_side[r["entry"]] = credit_side.get(r["entry"], Decimal(0)) + Decimal(r["credit"])
    assert debit == credit_side
    credit_rows = [r for r in rows if r["journal"] == "AP_CREDIT"]
    assert {r["reference"] for r in credit_rows} == {staff_view["reference"]}
    (payable,) = [r for r in credit_rows if r["account_code"] == "AP_CONTROL"]
    assert Decimal(payable["debit"]) == credit_total
    assert Decimal(payable["credit"]) == 0
    assert all(Decimal(r["debit"]) == 0 for r in credit_rows if r is not payable)
    invoice_rows = [r for r in rows if r["journal"] == "AP_INVOICE"]
    (owed,) = [r for r in invoice_rows if r["account_code"] == "AP_CONTROL"]
    assert Decimal(owed["credit"]) == invoice_total


async def test_portal_invoice_keeps_its_vendor_and_duplicates_are_one(
    flow: Flow, fworld: PayWorld
) -> None:
    vendor_id = await make_vendor(flow, fworld, "inv-0001")
    session = await _signed_in(flow, fworld, await _grant(flow, fworld, vendor_id))
    sent = await _portal_upload(flow, session, "inv-0004")
    assert sent.status_code == 202, sent.text
    assert sent.json()["duplicate"] is False
    invoice_id = sent.json()["invoice"]["id"]
    detail = await flow.invoice(fworld, invoice_id)
    assert detail["source"] == "vendor_portal"
    assert detail["vendor"]["id"] == vendor_id
    assert detail["vendor_match"]["method"] == "vendor_portal"
    again = await _portal_upload(flow, session, "inv-0004")
    assert again.status_code == 202
    assert again.json()["duplicate"] is True
    # The same file AP already holds for no vendor of this contact: refused, not leaked.
    staff_copy = await upload(flow, fworld, "inv-0030")
    assert staff_copy
    clash = await _portal_upload(flow, session, "inv-0030")
    assert clash.status_code == 409, clash.text
    bad = await _portal_upload(flow, session, "inv-0005", document_type="receipt")
    assert bad.status_code == 400


# ------------------------------------------------------------------ messages


async def test_messages_with_ap_are_threaded_per_invoice(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    invoice_id, vendor_id = await ready_invoice(flow, fworld, "inv-0001")
    email = await _grant(flow, fworld, vendor_id)
    session = await _signed_in(flow, fworld, email)

    wrote = await flow.client.post(
        f"{PORTAL}/invoices/{invoice_id}/messages",
        json={"text": "When will this invoice be paid?"},
        headers=session,
    )
    assert wrote.status_code == 201, wrote.text
    assert [m["from_vendor"] for m in wrote.json()["thread"]] == [True]
    thread = await flow.ok("GET", f"/v1/pay/invoices/{invoice_id}/portal-thread", fworld, "clerk")
    assert [m["text"] for m in thread] == ["When will this invoice be paid?"]
    assert thread[0]["status"] == "received"
    # The exception messages list stays the email channel only.
    assert await flow.ok("GET", f"/v1/pay/invoices/{invoice_id}/messages", fworld, "clerk") == []

    # AP replies: the default template is not sensitive, so it is shown at once.
    replied = await flow.ok(
        "POST",
        f"/v1/pay/invoices/{invoice_id}/portal-replies",
        fworld,
        "clerk",
        status=201,
        json={"text": "It is approved for the next payment run."},
    )
    assert [m["status"] for m in replied] == ["received", "posted"]
    seen = (await flow.client.get(f"{PORTAL}/invoices/{invoice_id}", headers=session)).json()
    assert [m["text"] for m in seen["thread"]] == [
        "When will this invoice be paid?",
        "It is approved for the next payment run.",
    ]
    mails = await _deliver(flow, fworld, tmp_path)
    assert any(
        email in m["to"] and "It is approved for the next payment run." in m["text"] for m in mails
    )
    em_dash = await flow.call(
        "POST",
        f"/v1/pay/invoices/{invoice_id}/portal-replies",
        fworld,
        "clerk",
        json={"text": "Paid — soon."},
    )
    assert em_dash.status_code == 400

    # An organisation's sensitive version of the reply template: it waits for approval
    # and the vendor sees nothing until then.
    await owner_rows(
        owner_engine,
        fworld.org_id,
        "INSERT INTO templates (org_id, key, channel, locale, version, category, subject, "
        "body, variables, requires_approval, created_by_type, created_by_id) "
        "SELECT org_id_new, key, channel, locale, 2, category, subject, body, variables, "
        "true, 'user', 'test' FROM (SELECT CAST(:o AS uuid) AS org_id_new, * FROM templates "
        "WHERE org_id IS NULL AND key = 'pay.vendor_portal.reply' AND locale = 'en') t",
        {"o": fworld.org_id},
    )
    pending = await flow.ok(
        "POST",
        f"/v1/pay/invoices/{invoice_id}/portal-replies",
        fworld,
        "clerk",
        status=201,
        json={"text": "We need the delivery note first."},
    )
    assert pending[-1]["status"] in {"submitted", "awaiting_approval"}
    seen = (await flow.client.get(f"{PORTAL}/invoices/{invoice_id}", headers=session)).json()
    assert "We need the delivery note first." not in [m["text"] for m in seen["thread"]]
