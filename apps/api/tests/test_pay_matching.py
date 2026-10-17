# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Purchase order matching in the invoice checks (brief 4.5.2) on Postgres, offline.

* A clean invoice with its PO and goods receipt on file is matched: the PO is recorded
  on the invoice, the PO shows what was invoiced, the checks' decision record names it.
* A price above the tolerance, a missing receipt and a PO number not on file send the
  invoice to a person with the exception.
* A PO created after the invoice makes its checks stale; running them again matches.
* Another organisation's PO never matches (tenant isolation).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from .pay_flow_support import Flow, flow, fother, fworld, make_vendor, seed_po, upload
from .pay_support import PayWorld, owner_rows
from .test_pay_invoices import demo

__all__ = ["flow", "fother", "fworld"]  # fixtures

CLEAN = demo("clean")


def _check(detail: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [c for c in detail["checks"] if c["check"] == name]
    assert isinstance(found, dict)
    return found


async def test_clean_invoice_is_matched_and_records_its_po(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    vendor_id = await make_vendor(flow, fworld, CLEAN)
    po_id = await seed_po(flow, fworld, CLEAN, vendor_id)
    invoice_id = await upload(flow, fworld, CLEAN)
    detail = await flow.invoice(fworld, invoice_id)
    assert detail["status"] == "ready", detail["review_reasons"]
    assert detail["po_id"] == po_id
    assert detail["po_match"]["status"] == "matched"
    matching = _check(detail, "matching")
    assert matching["status"] == "pass"
    assert matching["findings"][0]["rule"] == "po_matched"
    assert "PO-2026-0079" in matching["findings"][0]["message"]["ar"]

    po = await flow.ok("GET", f"/v1/pay/purchase-orders/{po_id}", fworld, "clerk")
    assert [x["invoiced"] for x in po["lines"]] == [x["quantity"] for x in po["lines"]]
    assert [i["id"] for i in po["invoices"]] == [invoice_id]
    assert po["invoices"][0]["matching"] == "matched"
    (record,) = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT inputs FROM decision_records WHERE entity_id = :e AND kind = 'fraud_flag'",
        {"e": invoice_id},
    )
    assert record.inputs["purchase_order"]["po_id"] == po_id
    assert record.inputs["purchase_order"]["match"] == "matched"


async def test_exceptions_go_to_a_person_and_a_later_po_makes_checks_stale(
    flow: Flow, fworld: PayWorld
) -> None:
    # The organisation keeps POs: the clean demo invoice's PO is on file.
    clean_vendor = await make_vendor(flow, fworld, CLEAN)
    await seed_po(flow, fworld, CLEAN, clean_vendor)

    # A unit price 2.2% over the PO (synthetic case price_outside).
    vendor = await make_vendor(flow, fworld, "inv-0037")
    await seed_po(flow, fworld, "inv-0037", vendor)
    over = await flow.invoice(fworld, await upload(flow, fworld, "inv-0037"))
    assert over["status"] == "needs_review"
    assert "check:price_variance" in over["review_reasons"]
    assert over["po_match"]["status"] == "exception"
    assert over["po_id"] is not None  # the PO it cites, so later invoices see what it took

    # Goods PO without a goods receipt yet.
    vendor = await make_vendor(flow, fworld, "inv-0001")
    await seed_po(flow, fworld, "inv-0001", vendor, receipts=False)
    missing = await flow.invoice(fworld, await upload(flow, fworld, "inv-0001"))
    assert "check:missing_receipt" in missing["review_reasons"]

    # A PO number that is not on file, then the PO arrives: the checks are stale until
    # they run again, and then the invoice is matched.
    vendor = await make_vendor(flow, fworld, "inv-0030")
    unknown_id = await upload(flow, fworld, "inv-0030")
    unknown = await flow.invoice(fworld, unknown_id)
    assert "check:no_po" in unknown["review_reasons"]
    assert unknown["po_id"] is None
    assert unknown["checks_state"] == "current"
    await seed_po(flow, fworld, "inv-0030", vendor)
    assert (await flow.invoice(fworld, unknown_id))["checks_state"] == "stale"
    again = await flow.ok("POST", f"/v1/pay/invoices/{unknown_id}/checks", fworld, "clerk")
    assert again["checks_state"] == "current"
    assert again["po_match"]["status"] == "matched"
    assert not [r for r in again["review_reasons"] if r.startswith("check:")]


async def test_another_organisations_po_never_matches(
    flow: Flow, fworld: PayWorld, fother: PayWorld
) -> None:
    vendor_id = await make_vendor(flow, fworld, CLEAN)
    await seed_po(flow, fworld, CLEAN, vendor_id)
    await make_vendor(flow, fother, CLEAN)
    invoice_id = await upload(flow, fother, CLEAN)
    detail = await flow.invoice(fother, invoice_id)
    assert detail["po_id"] is None
    # The other organisation keeps no POs: a note, and a person approves.
    assert detail["po_match"]["status"] == "not_cited"
    assert _check(detail, "matching")["findings"][0]["rule"] == "po_not_on_file"
    assert (await flow.ok("GET", "/v1/pay/purchase-orders", fother, "clerk"))["items"] == []
