# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Payment runs (WP4.7, brief 4.6.3) on Postgres, offline. No bank, no money moves.

* A proposal takes approved invoices due by a date, oldest due first, up to the cash
  limit, and leaves out held vendors, stale checks and what is not due yet, with the
  reasons; the invoices become scheduled.
* A finance controller approves; the proposer never can.
* The CSV and the pain.001.001.09 file carry the vendor master's bank details; a second
  factor is needed; only the file's digest is kept.
* At export everything is checked again: a bank-detail change (open, then completed)
  refuses the file with the reasons, audited, and nothing is built.
* Mark paid after export; rejection and cancellation release the invoices; decision
  records; tenant isolation.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

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
from .pay_support import PayWorld, audit_actions, other_bank, owner_rows, synth_vendors
from .test_pay_invoices import dataset, demo, vendor_index

__all__ = ["flow", "fother", "fworld"]  # fixtures

CLEAN = demo("clean")
PAIN = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
PAYER = {
    "payer_name": "Acme Trading LLC",
    "payer_iban": "AE070331234567890123456",
    "payer_bic": "NBADAEAA",
}


async def _approved(
    flow: Flow, owner: AsyncEngine, world: PayWorld, doc_id: str
) -> tuple[str, str]:
    invoice_id, vendor_id = await ready_invoice(flow, world, doc_id)
    sub = await submit(flow, world, invoice_id)
    await approve_all(flow, owner, world, sub["approval"]["approval_id"])
    detail = await flow.invoice(world, invoice_id)
    assert detail["status"] == "approved", detail
    return invoice_id, vendor_id


async def _settings(flow: Flow, world: PayWorld, **payments: Any) -> None:
    await flow.ok(
        "PUT",
        "/v1/pay/settings",
        world,
        "admin",
        json={"cooling_off_hours": 0, "payments": payments},
    )


async def _propose(flow: Flow, world: PayWorld, who: str = "finance", **body: Any) -> Any:
    payload = {"currency": "AED", "cash_limit": "1000000", "execution_date": "2026-10-20"}
    payload.update(body)
    return await flow.ok("POST", "/v1/pay/payment-runs", world, who, status=201, json=payload)


async def _approve_run(flow: Flow, world: PayWorld, run: dict[str, Any]) -> dict[str, Any]:
    response = await flow.decide(world, "finance2", run["approval_id"])
    assert response.status_code == 200, response.text
    return dict(await flow.ok("GET", f"/v1/pay/payment-runs/{run['id']}", world, "finance"))


async def _export(
    flow: Flow, world: PayWorld, run_id: str, fmt: str = "csv", who: str = "clerk", **kw: Any
) -> Any:
    return await flow.call(
        "POST", f"/v1/pay/payment-runs/{run_id}/export", world, who, json={"format": fmt}, **kw
    )


def _iban(doc_id: str) -> str:
    truth = dataset().doc(doc_id).record.truth
    assert truth.vendor_id is not None
    return str(synth_vendors()[vendor_index(truth.vendor_id)]["bank"]["iban"])


# ------------------------------------------------------------------ proposal


async def test_run_proposal_filters_by_due_date_holds_checks_and_cash_limit(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    early, _ = await _approved(flow, owner_engine, fworld, "inv-0055")  # due 2026-02-10
    big, _ = await _approved(flow, owner_engine, fworld, "inv-0001")  # due 2026-04-12
    held, held_vendor = await _approved(flow, owner_engine, fworld, "inv-0030")
    stale, _ = await _approved(flow, owner_engine, fworld, "inv-0037")
    later, _ = await _approved(flow, owner_engine, fworld, CLEAN)  # due 2026-10-10
    await flow.ok(
        "POST",
        f"/v1/pay/vendors/{held_vendor}/hold",
        fworld,
        "finance",
        json={"reason": "Dispute."},
    )
    stale_detail = await flow.invoice(fworld, stale)
    await flow.ok(
        "POST",
        f"/v1/extractions/{stale_detail['extraction']['id']}/corrections",
        fworld,
        "clerk",
        status=201,
        json={"field_path": "total", "new_value": "1.00", "reason": "Changed after approval"},
    )
    early_total = float((await flow.invoice(fworld, early))["total"])
    body = {"cash_limit": str(early_total + 100), "due_on_or_before": "2026-06-30"}
    preview = await flow.ok(
        "POST",
        "/v1/pay/payment-runs/preview",
        fworld,
        "finance",
        json={"currency": "AED", "execution_date": "2026-10-20", **body},
    )
    assert [i["invoice_id"] for i in preview["items"]] == [early]
    assert preview["items"][0]["discount"] is None  # no discount terms
    reasons = {e["invoice_id"]: e["reasons"] for e in preview["excluded"]}
    assert reasons[big] == ["over_cash_limit"]
    assert "vendor_held:manual" in reasons[held]
    assert "checks_stale" in reasons[stale]
    assert "checks_changed_since_approval" in reasons[stale]
    assert reasons[later] == ["not_due_yet"]

    run = await _propose(flow, fworld, **body)
    assert run["status"] == "awaiting_approval"
    assert run["approval_id"] is not None
    assert [i["invoice_id"] for i in run["items"]] == [early]
    assert float(run["total"]) == early_total
    assert {e["invoice_id"] for e in run["excluded"]} == {big, held, stale, later}
    assert (await flow.invoice(fworld, early))["status"] == "scheduled"
    assert (await flow.invoice(fworld, big))["status"] == "approved"
    # Nothing in another currency: refused with nothing created.
    nothing = await flow.call(
        "POST",
        "/v1/pay/payment-runs",
        fworld,
        "finance",
        json={"currency": "USD", "cash_limit": "10", "execution_date": "2026-10-20"},
    )
    assert nothing.status_code == 409


# ------------------------------------------------------------------ approval, export, paid


async def test_finance_approves_the_run_never_the_proposer_then_export_and_mark_paid(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    await _settings(flow, fworld, **PAYER)
    first, _ = await _approved(flow, owner_engine, fworld, CLEAN)
    second, _ = await _approved(flow, owner_engine, fworld, "inv-0030")
    run = await _propose(flow, fworld, who="finance")
    assert {i["invoice_id"] for i in run["items"]} == {first, second}
    row = await approval_row(owner_engine, fworld, run["approval_id"])
    assert row.payload["items"] == "2"
    assert row.context["kind"] == "pay.payment_run"
    assignees = await current_assignees(owner_engine, fworld, run["approval_id"])
    assert str(fworld.id("finance")) not in assignees  # the proposer
    assert str(fworld.id("approver")) not in assignees  # finance controllers only
    assert (await flow.decide(fworld, "finance", run["approval_id"])).status_code == 403
    assert (await flow.decide(fworld, "approver", run["approval_id"])).status_code == 403
    # No file before the approval.
    early = await _export(flow, fworld, run["id"])
    assert early.status_code == 409
    approved = await _approve_run(flow, fworld, run)
    assert approved["status"] == "approved"
    assert approved["approver_ids"] == [str(fworld.id("finance2"))]

    # A second factor is needed for the file.
    assert (await _export(flow, fworld, run["id"], otp=False)).status_code == 403
    response = await _export(flow, fworld, run["id"])
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["x-content-sha256"] == hashlib.sha256(response.content).hexdigest()
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8"))))
    by_invoice = {r["invoice_reference"]: r for r in rows}
    details = {i: await flow.invoice(fworld, i) for i in (first, second)}
    assert set(by_invoice) == {d["reference"] for d in details.values()}
    for doc_id, invoice_id in ((CLEAN, first), ("inv-0030", second)):
        row_out = by_invoice[details[invoice_id]["reference"]]
        assert row_out["beneficiary_iban"] == _iban(doc_id)
        assert row_out["amount"] == details[invoice_id]["total"]
        assert row_out["currency"] == "AED"
        assert row_out["payment_run"] == run["reference"]

    xml = await _export(flow, fworld, run["id"], "pain001", who="finance")
    assert xml.status_code == 200, xml.text
    root = ET.fromstring(xml.content)  # noqa: S314 (our own API's output)
    header = root.find("p:CstmrCdtTrfInitn/p:GrpHdr", PAIN)
    assert header is not None
    assert header.findtext("p:NbOfTxs", namespaces=PAIN) == "2"
    assert header.findtext("p:CtrlSum", namespaces=PAIN) == run["total"]
    info = root.find("p:CstmrCdtTrfInitn/p:PmtInf", PAIN)
    assert info is not None
    assert info.findtext("p:DbtrAcct/p:Id/p:IBAN", namespaces=PAIN) == PAYER["payer_iban"]
    assert info.findtext("p:ReqdExctnDt/p:Dt", namespaces=PAIN) == "2026-10-20"
    ibans = {
        tx.findtext("p:CdtrAcct/p:Id/p:IBAN", namespaces=PAIN)
        for tx in info.findall("p:CdtTrfTxInf", PAIN)
    }
    assert ibans == {_iban(CLEAN), _iban("inv-0030")}

    exported = await flow.ok("GET", f"/v1/pay/payment-runs/{run['id']}", fworld, "clerk")
    assert exported["status"] == "exported"
    assert [e["format"] for e in exported["exports"]] == ["csv", "pain001"]
    assert exported["exports"][0]["sha256"] == response.headers["x-content-sha256"]

    paid = await flow.ok(
        "POST",
        f"/v1/pay/payment-runs/{run['id']}/paid",
        fworld,
        "clerk",
        json={"invoice_ids": [first], "reference": "BANK-REF-001"},
    )
    assert paid["status"] == "exported"
    assert (await flow.invoice(fworld, first))["status"] == "paid"
    assert (await flow.invoice(fworld, first))["payment_reference"] == "BANK-REF-001"
    done = await flow.ok(
        "POST",
        f"/v1/pay/payment-runs/{run['id']}/paid",
        fworld,
        "clerk",
        json={"reference": "BANK-REF-002"},
    )
    assert done["status"] == "paid"
    assert (await flow.invoice(fworld, second))["status"] == "paid"
    records = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT final_action FROM decision_records WHERE entity_id = :e ORDER BY decided_at",
        {"e": run["id"]},
    )
    actions = [r.final_action for r in records]
    for action in (
        "pay.payment_run.proposed",
        "pay.payment_run.exported",
        "pay.payment_run.marked_paid",
    ):
        assert action in actions
    assert any(a and "approve_payment_run" in a for a in actions)
    assert "pay.invoice.paid" in await audit_actions(owner_engine, fworld.org_id, first)


async def test_export_fails_closed_when_bank_details_change(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    await _settings(flow, fworld, **PAYER)
    invoice_id, vendor_id = await _approved(flow, owner_engine, fworld, CLEAN)
    run = await _propose(flow, fworld)
    await _approve_run(flow, fworld, run)

    # A change ticket opens: the vendor is held. No file.
    ticket = await flow.ok(
        "POST",
        f"/v1/pay/vendors/{vendor_id}/bank-changes",
        fworld,
        "clerk",
        status=201,
        json={"bank": other_bank(5), "source": "email", "note": "New account letter."},
    )
    refused = await _export(flow, fworld, run["id"])
    assert refused.status_code == 409, refused.text
    problems = refused.json()["problems"]
    assert any(p.endswith(":vendor_held:bank_change") for p in problems)
    assert any(p.endswith(":bank_change_open") for p in problems)

    # The change completes (call back, two approvers, no cooling-off): the vendor master
    # now has other details than the ones approved. Still no file.
    called = await flow.ok(
        "POST",
        f"/v1/pay/bank-changes/{ticket['id']}/calls",
        fworld,
        "clerk2",
        json={"result": "confirmed", "notes": "Confirmed.", "called_number_on_file": True},
    )
    for who in ("approver", "approver2"):
        response = await flow.decide(fworld, who, called["approval_id"])
        assert response.status_code == 200, response.text
    ticket_now = await flow.ok("GET", f"/v1/pay/bank-changes/{ticket['id']}", fworld, "clerk")
    assert ticket_now["status"] == "completed"
    again = await _export(flow, fworld, run["id"], "pain001")
    assert again.status_code == 409
    problems = again.json()["problems"]
    assert any(p.endswith(":bank_details_changed") for p in problems)
    actions = await audit_actions(owner_engine, fworld.org_id, run["id"])
    assert actions.count("pay.payment_run.export_refused") == 2
    assert "pay.payment_run.exported" not in actions
    after = await flow.ok("GET", f"/v1/pay/payment-runs/{run['id']}", fworld, "clerk")
    assert after["status"] == "approved"
    assert after["exports"] == []

    # Cancelled: the invoice is approved again, and a new proposal leaves it out.
    cancelled = await flow.ok(
        "POST",
        f"/v1/pay/payment-runs/{run['id']}/cancel",
        fworld,
        "finance",
        json={"reason": "Bank details changed."},
    )
    assert cancelled["status"] == "cancelled"
    assert (await flow.invoice(fworld, invoice_id))["status"] == "approved"
    preview = await flow.ok(
        "POST",
        "/v1/pay/payment-runs/preview",
        fworld,
        "finance",
        json={"currency": "AED", "cash_limit": "1000000", "execution_date": "2026-10-20"},
    )
    assert preview["items"] == []
    (excluded,) = preview["excluded"]
    assert "bank_details_changed" in excluded["reasons"]


async def test_rejected_run_releases_invoices_and_paid_needs_export(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_id, _ = await _approved(flow, owner_engine, fworld, CLEAN)
    run = await _propose(flow, fworld, who="clerk")
    early_paid = await flow.call(
        "POST",
        f"/v1/pay/payment-runs/{run['id']}/paid",
        fworld,
        "clerk",
        json={"reference": "X-1"},
    )
    assert early_paid.status_code == 409
    rejected = await flow.decide(fworld, "finance", run["approval_id"], "reject", "Cash is short.")
    assert rejected.status_code == 200, rejected.text
    after = await flow.ok("GET", f"/v1/pay/payment-runs/{run['id']}", fworld, "clerk")
    assert after["status"] == "rejected"
    assert after["items"][0]["status"] == "released"
    assert (await flow.invoice(fworld, invoice_id))["status"] == "approved"
    # Approved again, it can go in a new run.
    again = await _propose(flow, fworld, who="clerk")
    assert [i["invoice_id"] for i in again["items"]] == [invoice_id]


async def test_tenant_isolation(
    flow: Flow, fworld: PayWorld, fother: PayWorld, owner_engine: AsyncEngine
) -> None:
    await _approved(flow, owner_engine, fworld, CLEAN)
    run = await _propose(flow, fworld)
    assert (
        await flow.call("GET", f"/v1/pay/payment-runs/{run['id']}", fother, "finance")
    ).status_code == 404
    assert (await _export(flow, fother, run["id"])).status_code == 404
    listed = await flow.ok("GET", "/v1/pay/payment-runs", fother, "finance")
    assert listed["items"] == []
    nothing = await flow.call(
        "POST",
        "/v1/pay/payment-runs",
        fother,
        "finance",
        json={"currency": "AED", "cash_limit": "1000000", "execution_date": "2026-10-20"},
    )
    assert nothing.status_code == 409


# ------------------------------------------------------------------ early-payment discounts


async def test_early_payment_discounts_are_highlighted_and_taken_only_when_asked(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    await _settings(flow, fworld, **PAYER)
    invoice_id, vendor_id = await _approved(flow, owner_engine, fworld, CLEAN)
    detail = await flow.invoice(fworld, invoice_id)
    assert detail["early_payment"] is None  # "Net 30": no discount
    await flow.ok(
        "PATCH",
        f"/v1/pay/vendors/{vendor_id}",
        fworld,
        "clerk",
        json={"payment_terms": "2/10 net 30"},
    )
    updated = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT payload FROM audit_events WHERE entity_id = :v "
        "AND action = 'pay.vendor.updated'",
        {"v": vendor_id},
    )
    assert updated[-1].payload["fields"] == ["payment_terms"]  # the web vendor page edits it
    detail = await flow.invoice(fworld, invoice_id)
    offer = detail["early_payment"]
    total = Decimal(detail["total"])
    saving = (total * Decimal("0.02")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    invoice_date = dt.date.fromisoformat(detail["invoice_date"])
    pay_by = invoice_date + dt.timedelta(days=10)
    assert offer["terms"] == "2/10 net 30"
    assert offer["source"] == "vendor"
    assert Decimal(offer["saving"]) == saving
    assert Decimal(offer["pay_early"]) == total - saving
    assert offer["pay_by"] == pay_by.isoformat()
    assert offer["net_due"] == (invoice_date + dt.timedelta(days=30)).isoformat()
    assert Decimal(offer["annual_rate"]) == Decimal("0.3724")

    not_due = (invoice_date - dt.timedelta(days=1)).isoformat()
    base = {"currency": "AED", "cash_limit": "1000000", "due_on_or_before": not_due}
    # Highlighted, not taken: not due yet, with what paying it early would save.
    plain = await flow.ok(
        "POST",
        "/v1/pay/payment-runs/preview",
        fworld,
        "finance",
        json={**base, "execution_date": pay_by.isoformat()},
    )
    (left_out,) = [e for e in plain["excluded"] if e["invoice_id"] == invoice_id]
    assert left_out["reasons"] == ["not_due_yet"]
    assert Decimal(left_out["saving"]) == saving
    assert left_out["pay_by"] == pay_by.isoformat()
    assert Decimal(plain["savings"]) == 0
    assert Decimal(plain["savings_available"]) == saving
    # Too late for the discount: nothing to highlight or take.
    late = await flow.ok(
        "POST",
        "/v1/pay/payment-runs/preview",
        fworld,
        "finance",
        json={
            **base,
            "execution_date": (pay_by + dt.timedelta(days=1)).isoformat(),
            "include_early_payment": True,
        },
    )
    (late_out,) = [e for e in late["excluded"] if e["invoice_id"] == invoice_id]
    assert late_out["reasons"] == ["not_due_yet"]
    assert late_out["saving"] is None

    # Taken when asked: the discounted amount, the saving recorded on the item.
    early: dict[str, Any] = {
        **base,
        "execution_date": pay_by.isoformat(),
        "include_early_payment": True,
    }
    taken = await flow.ok("POST", "/v1/pay/payment-runs/preview", fworld, "finance", json=early)
    (item,) = taken["items"]
    assert Decimal(item["discount"]) == saving
    assert Decimal(item["amount"]) == total - saving
    assert Decimal(taken["savings"]) == saving
    run = await _propose(flow, fworld, **early)
    (run_item,) = run["items"]
    assert Decimal(run_item["discount"]) == saving
    assert Decimal(run_item["amount"]) == total - saving
    assert Decimal(run["total"]) == total - saving
    approved = await _approve_run(flow, fworld, run)
    assert approved["status"] == "approved"

    # The discount is checked again at export: changed terms refuse the file.
    await flow.ok(
        "PATCH", f"/v1/pay/vendors/{vendor_id}", fworld, "clerk", json={"payment_terms": "Net 30"}
    )
    refused = await _export(flow, fworld, run["id"])
    assert refused.status_code == 409, refused.text
    assert "discount_changed" in refused.text
    await flow.ok(
        "PATCH",
        f"/v1/pay/vendors/{vendor_id}",
        fworld,
        "clerk",
        json={"payment_terms": "2/10 net 30"},
    )
    response = await _export(flow, fworld, run["id"])
    assert response.status_code == 200, response.text
    (row,) = list(csv.DictReader(io.StringIO(response.content.decode("utf-8"))))
    assert Decimal(row["amount"]) == total - saving
