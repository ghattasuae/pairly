# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The month-end report on Postgres, offline, with synthetic data.

* Open payables at the period end, in aging buckets, each listing its invoices; a paid
  invoice leaves them; a period ending before the invoices arrived shows none.
* Goods received not invoiced, valued at the PO price.
* The CSV lists every record; its download is audited with the file's digest.
* Permissions and tenant isolation.
"""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from .pay_flow_support import Flow, flow, fother, fworld
from .pay_support import PayWorld, new_vendor, owner_rows, vendor_body
from .test_pay_payment_runs import CLEAN, _approved

__all__ = ["flow", "fother", "fworld"]  # fixtures

PATH = "/v1/pay/month-end"


def _this_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


async def _po_with_receipt(flow: Flow, world: PayWorld) -> dict[str, Any]:
    vendor = await new_vendor(flow, world, vendor_body(3))
    po = await flow.ok(
        "POST",
        "/v1/pay/purchase-orders",
        world,
        "clerk",
        status=201,
        json={
            "po_number": "PO-2026-0901",
            "vendor_id": vendor["id"],
            "currency": "AED",
            "order_date": "2026-09-20",
            "match_type": "3-way",
            "lines": [
                {
                    "line_no": 1,
                    "description": "Laptop 14 inch",
                    "quantity": "4",
                    "unit_price": "3999.99",
                },
                {
                    "line_no": 2,
                    "description": "USB-C dock",
                    "quantity": "10",
                    "unit_price": "412.345",
                },
            ],
        },
    )
    await flow.ok(
        "POST",
        "/v1/pay/goods-receipts",
        world,
        "clerk",
        status=201,
        json={
            "grn_number": "GRN-2026-0901",
            "po_id": po["id"],
            "received_date": "2026-09-28",
            "lines": [{"po_line_no": 1, "quantity": "3"}],
        },
    )
    return dict(po)


async def test_open_payables_aging_accruals_and_the_csv(
    flow: Flow, fworld: PayWorld, fother: PayWorld, owner_engine: AsyncEngine
) -> None:
    first, _ = await _approved(flow, owner_engine, fworld, CLEAN)
    second, _ = await _approved(flow, owner_engine, fworld, "inv-0030")
    po = await _po_with_receipt(flow, fworld)
    period = _this_month()

    report = await flow.ok("GET", f"{PATH}?period={period}", fworld, "finance")
    assert report["period"] == period
    assert report["can_export_journal"] is True
    (aed,) = report["aging"]
    listed = {i["id"]: i for b in aed["buckets"] for i in b["invoices"]}
    assert {first, second} <= set(listed)
    # Every bucket adds up its invoices, and the buckets add up the total.
    for bucket in aed["buckets"]:
        assert Decimal(bucket["amount"]) == sum(
            (Decimal(i["amount"]) for i in bucket["invoices"]), Decimal(0)
        )
        assert bucket["count"] == len(bucket["invoices"])
    assert Decimal(aed["total"]) == sum((Decimal(b["amount"]) for b in aed["buckets"]), Decimal(0))
    assert [b["bucket"] for b in aed["buckets"]] == [
        "current", "1_30", "31_60", "61_90", "90_plus", "undated",
    ]  # fmt: skip
    detail = await flow.invoice(fworld, first)
    assert Decimal(listed[first]["amount"]) == Decimal(detail["total"])

    (accrual,) = report["accruals"]
    (line,) = accrual["lines"]
    assert line["po_id"] == po["id"]
    assert (line["received"], line["invoiced"], line["quantity"]) == ("3", "0", "3")
    assert line["amount"] == accrual["total"] == "11999.97"

    # A period that ended before anything arrived shows nothing.
    before = await flow.ok("GET", f"{PATH}?period=2026-08", fworld, "finance")
    assert before["aging"] == []
    assert before["accruals"] == []  # the receipt is dated 2026-09-28
    september = await flow.ok("GET", f"{PATH}?period=2026-09", fworld, "auditor")
    assert september["aging"] == []  # the invoices arrived after September
    assert september["accruals"][0]["total"] == "11999.97"
    assert september["can_export_journal"] is False

    # The CSV: one row per record, audited with its digest.
    response = await flow.call("GET", f"{PATH}/export?period={period}", fworld, "finance")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    digest = hashlib.sha256(response.content).hexdigest()
    assert response.headers["x-content-sha256"] == digest
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8"))))
    payables = {r["record_id"]: r for r in rows if r["section"] == "open_payable"}
    assert payables[first]["amount"] == listed[first]["amount"]
    assert [r["amount"] for r in rows if r["section"] == "accrual"] == ["11999.97"]
    audited = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT payload FROM audit_events WHERE action = 'pay.month_end.exported'",
    )
    assert audited[-1].payload == {
        "period": period,
        "sha256": digest,
        "bytes": len(response.content),
    }

    # Permissions and tenant isolation; a bad period is refused.
    assert (await flow.call("GET", PATH, fworld, "clerk")).status_code == 403
    assert (await flow.call("GET", f"{PATH}?period=2026-13", fworld, "finance")).status_code == 422
    other = await flow.ok("GET", f"{PATH}?period={period}", fother, "finance")
    assert other["aging"] == []
    assert other["accruals"] == []
