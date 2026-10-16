# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Checks run again after a person changes an invoice (security fix, WP4.7) on Postgres.

* Correcting the IBAN of a ready invoice to a new account re-runs every check: the
  bank-detail change ticket opens (system opened, source invoice), the vendor is held,
  the invoice goes back to a person, and the risk is assessed again.
* Correcting the total re-runs the arithmetic check; the old check reasons are replaced.
* A change that bypasses Pay (the engine's generic correction route) leaves the checks
  stale: the invoice cannot be marked reviewed until the checks run again.
* Linking another vendor and changing the coding re-run the checks too.
* A check run that fails on a correction leaves the invoice stale (fail closed).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_pay.checks import service as check_service

from . import test_pay_invoices as base
from .pay_support import PayWorld, audit_actions, new_vendor, other_bank, owner_rows, vendor_body
from .test_pay_invoices import Api, dataset, demo, vendor_index

api = base.api
world = base.world


async def _ready(api: Api, world: PayWorld) -> tuple[str, str]:
    truth = dataset().doc(demo("clean")).record.truth
    assert truth.vendor_id is not None
    vendor = await new_vendor(api, world, vendor_body(vendor_index(truth.vendor_id)))
    created = await api.upload_doc(world, demo("clean"))
    detail = await api.invoice(world, created["id"])
    assert detail["status"] == "ready", detail["review_reasons"]
    assert detail["checks_state"] == "current"
    return str(created["id"]), str(vendor["id"])


async def _correct(api: Api, world: PayWorld, invoice_id: str, path: str, value: str) -> Any:
    return await api.ok(
        "POST",
        f"/v1/pay/invoices/{invoice_id}/corrections",
        world,
        "clerk",
        json={"field_path": path, "new_value": value, "reason": "Read from the paper copy."},
    )


def _check(detail: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [c for c in detail["checks"] if c["check"] == name]
    return dict(found)


async def test_correct_the_iban_on_a_ready_invoice_opens_the_ticket_and_holds_the_vendor(
    api: Api, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_id, vendor_id = await _ready(api, world)
    before = await api.invoice(world, invoice_id)
    assert _check(before, "bank")["status"] == "pass"
    old_assessment = before["risk"]["assessment_id"]

    detail = await _correct(api, world, invoice_id, "bank.iban", other_bank(5)["iban"])
    assert detail["status"] == "needs_review"
    assert detail["checks_state"] == "current"
    bank = _check(detail, "bank")
    assert bank["status"] == "fail"
    (mismatch,) = [f for f in bank["findings"] if f["rule"] == "bank_mismatch"]
    ticket_id = mismatch["evidence"]["ticket_id"]
    assert "check:bank_mismatch" in detail["review_reasons"]
    assert "corrected_after_ready" in detail["review_reasons"]
    # The risk was assessed again, on the corrected invoice.
    assert detail["risk"]["assessment_id"] != old_assessment
    assert detail["risk"]["requires_human"] is True

    tickets = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT id, status, source, requested_by_type FROM pay_bank_change_tickets "
        "WHERE vendor_id = :v AND status <> 'completed'",
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
    assert actions.index("pay.invoice.corrected") < actions.index("pay.invoice.rechecked")
    assert "pay.invoice.bank_mismatch" in actions

    # A second correction to yet another account joins the open ticket.
    again = await _correct(api, world, invoice_id, "bank.iban", other_bank(6)["iban"])
    (mismatch2,) = [f for f in _check(again, "bank")["findings"] if f["rule"] == "bank_mismatch"]
    assert mismatch2["evidence"]["ticket_id"] == ticket_id


async def test_correct_the_total_reruns_arithmetic_and_replaces_check_reasons(
    api: Api, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_id, _ = await _ready(api, world)
    before = await api.invoice(world, invoice_id)
    total = before["total"]
    wrong = str(round(float(total) + 1000, 2))
    detail = await _correct(api, world, invoice_id, "total", wrong)
    assert detail["status"] == "needs_review"
    assert _check(detail, "arithmetic")["status"] == "fail"
    assert any(r.startswith("check:") for r in detail["review_reasons"])
    # Corrected back: the arithmetic passes and its old reasons are gone.
    fixed = await _correct(api, world, invoice_id, "total", total)
    assert _check(fixed, "arithmetic")["status"] == "pass"
    arithmetic_codes = {f["code"] for f in _check(detail, "arithmetic")["findings"]}
    assert not any(r.removeprefix("check:") in arithmetic_codes for r in fixed["review_reasons"])
    reviewed = await api.ok(
        "POST", f"/v1/pay/invoices/{invoice_id}/review", world, "clerk", json={"note": "ok"}
    )
    assert reviewed["status"] == "ready"
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT count(*) AS n FROM audit_events WHERE entity_id = :e "
        "AND action = 'pay.invoice.rechecked'",
        {"e": invoice_id},
    )
    assert rows[0].n == 2


async def test_a_change_outside_pay_leaves_the_checks_stale_until_they_run_again(
    api: Api, world: PayWorld
) -> None:
    invoice_id, _ = await _ready(api, world)
    detail = await api.invoice(world, invoice_id)
    extraction_id = detail["extraction"]["id"]
    # The engine's generic correction route does not know about Pay's checks.
    await api.ok(
        "POST",
        f"/v1/extractions/{extraction_id}/corrections",
        world,
        "clerk",
        status=201,
        json={"field_path": "invoice_number", "new_value": "INV-OTHER-1", "reason": "Typo"},
    )
    stale = await api.invoice(world, invoice_id)
    assert stale["checks_state"] == "stale"
    # Move it to needs_review through Pay's own coding route, then try to review.
    coded = await api.ok(
        "POST",
        f"/v1/pay/invoices/{invoice_id}/coding",
        world,
        "clerk",
        json={"department": "operations", "category": "it_services"},
    )
    # Coding re-ran the checks: they are current again, on the new number.
    assert coded["checks_state"] == "current"
    assert coded["department"] == "operations"
    await api.ok(
        "POST",
        f"/v1/extractions/{extraction_id}/corrections",
        world,
        "clerk",
        status=201,
        json={"field_path": "invoice_number", "new_value": "INV-OTHER-2", "reason": "Typo"},
    )
    refused = await api.client.post(
        f"/v1/pay/invoices/{invoice_id}/review", json={}, headers=api.headers(world, "clerk")
    )
    assert refused.status_code == 409, refused.text
    assert "out of date" in refused.text
    rerun = await api.ok("POST", f"/v1/pay/invoices/{invoice_id}/checks", world, "clerk")
    assert rerun["checks_state"] == "current"
    reviewed = await api.ok(
        "POST", f"/v1/pay/invoices/{invoice_id}/review", world, "clerk", json={}
    )
    assert reviewed["status"] == "ready"


async def test_a_failed_recheck_leaves_the_invoice_stale(
    api: Api, world: PayWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    invoice_id, _ = await _ready(api, world)

    async def broken(*_: Any, **__: Any) -> list[str]:
        raise RuntimeError("down")

    monkeypatch.setattr(check_service, "run_invoice_checks", broken)
    detail = await _correct(api, world, invoice_id, "po_number", "PO-NEW-1")
    assert detail["status"] == "needs_review"
    assert detail["checks_state"] == "missing"
    assert "check_failed:RuntimeError" in detail["review_reasons"]
    refused = await api.client.post(
        f"/v1/pay/invoices/{invoice_id}/review", json={}, headers=api.headers(world, "clerk")
    )
    assert refused.status_code == 409


async def test_linking_another_vendor_reruns_the_checks(
    api: Api, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_id, _ = await _ready(api, world)
    other = await new_vendor(api, world, vendor_body(7))
    detail = await api.ok(
        "POST",
        f"/v1/pay/invoices/{invoice_id}/vendor",
        world,
        "clerk",
        json={"vendor_id": other["id"]},
    )
    assert detail["status"] == "needs_review"
    assert detail["checks_state"] == "current"
    # The invoice's IBAN is not the new vendor's: the bank check says so.
    assert _check(detail, "bank")["status"] == "fail"
    assert "pay.invoice.rechecked" in await audit_actions(owner_engine, world.org_id, invoice_id)
