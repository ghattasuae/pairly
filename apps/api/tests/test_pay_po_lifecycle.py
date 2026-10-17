# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Closing and cancelling purchase orders, and matching again when goods arrive
(brief 4.5.2) on Postgres, offline.

* Close (``pay.purchase_order.write``): audited; an invoice received before keeps its
  match and its checks stay current; a later invoice fails matching with the reason; a
  closed PO receives no goods.
* Cancel (``pay.purchase_order.manage``, a second factor): refused while an invoice of
  the PO is approved; otherwise its invoices still with a person run the checks again
  and fail matching.
* A goods receipt re-matches the invoices of its PO waiting for one (``needs_review``
  with ``missing_receipt``), by JSON or CSV; nothing else is touched.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from .pay_flow_support import (
    Flow,
    approve_all,
    flow,
    fother,
    fworld,
    make_vendor,
    ready_invoice,
    seed_po,
    submit,
    upload,
)
from .pay_support import PayWorld, audit_actions
from .test_pay_invoices import dataset, demo

__all__ = ["flow", "fother", "fworld"]  # fixtures

CLEAN = demo("clean")
REASON = {"reason": "All goods delivered and invoiced."}


def _matching(detail: dict[str, Any]) -> dict[str, Any]:
    (found,) = [c for c in detail["checks"] if c["check"] == "matching"]
    assert isinstance(found, dict)
    return found


def _receipts(doc_id: str, po_id: str) -> list[dict[str, Any]]:
    ds = dataset()
    number = ds.doc(doc_id).record.truth.po_number
    return [
        {
            "grn_number": grn.grn_number,
            "po_id": po_id,
            "received_date": grn.date,
            "received_by": grn.received_by,
            "lines": [
                {"po_line_no": x.po_line_no, "quantity": x.quantity_received} for x in grn.lines
            ],
        }
        for grn in ds.receipts
        if grn.po_number == number
    ]


async def test_close_keeps_earlier_matches_and_refuses_later_invoices(
    flow: Flow, fworld: PayWorld, fother: PayWorld, owner_engine: AsyncEngine
) -> None:
    vendor = await make_vendor(flow, fworld, CLEAN)
    po_id = await seed_po(flow, fworld, CLEAN, vendor)
    before = await upload(flow, fworld, CLEAN)
    assert (await flow.invoice(fworld, before))["po_match"]["status"] == "matched"

    path = f"/v1/pay/purchase-orders/{po_id}/close"
    assert (await flow.call("POST", path, fworld, "approver", json=REASON)).status_code == 403
    assert (await flow.call("POST", path, fother, "clerk", json=REASON)).status_code == 404
    assert (await flow.call("POST", path, fworld, "clerk", json={"reason": ""})).status_code == 422
    closed = await flow.ok("POST", path, fworld, "clerk", json=REASON)
    assert closed["status"] == "closed"
    assert closed["close_reason"] == REASON["reason"]
    assert closed["closed_at"] is not None
    assert "pay.purchase_order.closed" in await audit_actions(owner_engine, fworld.org_id, po_id)
    assert (await flow.call("POST", path, fworld, "clerk", json=REASON)).status_code == 409

    # The invoice received before keeps its match: its checks did not go stale.
    kept = await flow.invoice(fworld, before)
    assert kept["checks_state"] == "current"
    again = await flow.ok("POST", f"/v1/pay/invoices/{before}/checks", fworld, "clerk")
    assert again["po_match"]["status"] == "matched"

    # A closed PO receives nothing.
    receipt = {
        "grn_number": "GRN-LATE-1",
        "po_id": po_id,
        "received_date": "2026-10-01",
        "lines": [{"po_line_no": 1, "quantity": "1"}],
    }
    late = await flow.call("POST", "/v1/pay/goods-receipts", fworld, "clerk", json=receipt)
    assert late.status_code == 400
    assert late.json()["errors"][0]["code"] == "po_closed"

    # A later invoice citing a PO closed before it arrived fails matching, with the reason.
    vendor = await make_vendor(flow, fworld, "inv-0003")
    later_po = await seed_po(flow, fworld, "inv-0003", vendor)
    await flow.ok("POST", f"/v1/pay/purchase-orders/{later_po}/close", fworld, "clerk", json=REASON)
    later = await flow.invoice(fworld, await upload(flow, fworld, "inv-0003"))
    assert later["status"] == "needs_review"
    rules = {f["rule"]: f for f in _matching(later)["findings"]}
    assert "closed" in rules["po_closed"]["message"]["en"]
    assert "مغلق" in rules["po_closed"]["message"]["ar"]


async def test_cancel_needs_a_second_factor_and_never_touches_approved_money(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_id, _ = await ready_invoice(flow, fworld, CLEAN, with_po=True)
    approval_id = (await submit(flow, fworld, invoice_id))["approval"]["approval_id"]
    await approve_all(flow, owner_engine, fworld, approval_id)
    assert (await flow.invoice(fworld, invoice_id))["status"] == "approved"
    po_id = (await flow.invoice(fworld, invoice_id))["po_id"]

    path = f"/v1/pay/purchase-orders/{po_id}/cancel"
    assert (await flow.call("POST", path, fworld, "clerk", json=REASON)).status_code == 403
    weak = await flow.call("POST", path, fworld, "finance", otp=False, json=REASON)
    assert weak.status_code == 403
    assert weak.json()["code"] == "second_factor_required"
    refused = await flow.call("POST", path, fworld, "finance", json=REASON)
    assert refused.status_code == 409
    assert (await flow.ok("GET", f"/v1/pay/purchase-orders/{po_id}", fworld, "clerk"))[
        "status"
    ] == "open"
    assert (await flow.invoice(fworld, invoice_id))["checks_state"] == "current"


async def test_cancel_rechecks_open_invoices_which_then_fail_matching(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    clean_vendor = await make_vendor(flow, fworld, CLEAN)
    await seed_po(flow, fworld, CLEAN, clean_vendor)
    vendor = await make_vendor(flow, fworld, "inv-0037")
    po_id = await seed_po(flow, fworld, "inv-0037", vendor)
    invoice_id = await upload(flow, fworld, "inv-0037")
    assert (await flow.invoice(fworld, invoice_id))["status"] == "needs_review"

    cancelled = await flow.ok(
        "POST", f"/v1/pay/purchase-orders/{po_id}/cancel", fworld, "finance",
        json={"reason": "Order withdrawn by the requester."},
    )  # fmt: skip
    assert cancelled["status"] == "cancelled"
    assert cancelled["rechecked"] == [invoice_id]
    detail = await flow.invoice(fworld, invoice_id)
    assert detail["checks_state"] == "current"
    rules = {f["rule"]: f for f in _matching(detail)["findings"]}
    assert "cancelled" in rules["po_closed"]["message"]["en"]
    assert "check:po_mismatch" in detail["review_reasons"]
    actions = await audit_actions(owner_engine, fworld.org_id, invoice_id)
    assert "pay.invoice.rechecked" in actions
    assert "pay.purchase_order.cancelled" in await audit_actions(owner_engine, fworld.org_id, po_id)


async def test_a_goods_receipt_rematches_invoices_waiting_for_it(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    vendor = await make_vendor(flow, fworld, "inv-0001")
    po_id = await seed_po(flow, fworld, "inv-0001", vendor, receipts=False)
    invoice_id = await upload(flow, fworld, "inv-0001")
    waiting = await flow.invoice(fworld, invoice_id)
    assert "check:missing_receipt" in waiting["review_reasons"]
    assert waiting["status"] == "needs_review"

    (first, *rest) = _receipts("inv-0001", po_id)
    created = await flow.ok(
        "POST", "/v1/pay/goods-receipts", fworld, "clerk", status=201, json=first
    )
    assert created["rematched"] == [invoice_id]
    after = await flow.invoice(fworld, invoice_id)
    assert "check:missing_receipt" not in after["review_reasons"]
    assert after["checks_state"] == "current"
    actions = await audit_actions(owner_engine, fworld.org_id, invoice_id)
    assert "pay.invoice.rechecked" in actions
    # Once nothing waits, a further receipt touches no invoice.
    for body in rest:
        more = await flow.ok(
            "POST", "/v1/pay/goods-receipts", fworld, "clerk", status=201, json=body
        )
        assert more["rematched"] == []


async def test_a_csv_receipt_rematches_too(flow: Flow, fworld: PayWorld) -> None:
    vendor = await make_vendor(flow, fworld, "inv-0001")
    po_id = await seed_po(flow, fworld, "inv-0001", vendor, receipts=False)
    invoice_id = await upload(flow, fworld, "inv-0001")
    po = await flow.ok("GET", f"/v1/pay/purchase-orders/{po_id}", fworld, "clerk")
    rows = ["grn_number,po_number,received_date,po_line_no,quantity"]
    rows += [
        f"GRN-CSV-1,{po['po_number']},2026-10-01,{x['line_no']},{x['quantity']}"
        for x in po["lines"]
    ]
    imported = await flow.ok(
        "POST",
        "/v1/pay/goods-receipts/import",
        fworld,
        "clerk",
        json={"csv": "\n".join(rows) + "\n"},
    )
    assert imported["rematched"] == [invoice_id]
    assert "check:missing_receipt" not in (await flow.invoice(fworld, invoice_id))["review_reasons"]
