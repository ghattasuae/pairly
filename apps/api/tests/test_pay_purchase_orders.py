# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Purchase orders and goods receipts (brief 4.5.1) on Postgres, through the API.

* Create a PO from JSON: line totals and the subtotal are computed (Decimal, minor
  unit), numbers are unique per organisation without case or separators, audited.
* CSV import of POs and receipts: strict validation with the row of every problem, all
  or nothing; formulas, signs and exponents refused.
* Receipts: never more than ordered (counting earlier receipts), only on open POs.
* Permissions and tenant isolation (another organisation's PO and vendor are unknown).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_pay.purchase_orders import number_key, parse_decimal

from .pay_support import (
    PayApi,
    PayWorld,
    audit_actions,
    new_vendor,
    other,
    owner_rows,
    pay_api,
    vendor_body,
    world,
)

__all__ = ["other", "pay_api", "world"]  # fixtures


async def _vendor(api: PayApi, world: PayWorld, index: int = 0) -> dict[str, Any]:
    created = await new_vendor(api, world, vendor_body(index))
    assert isinstance(created, dict)
    return created


def _po(vendor_id: str, number: str = "PO-2026-0079", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "po_number": number,
        "vendor_id": vendor_id,
        "currency": "AED",
        "order_date": "2026-09-20",
        "match_type": "3-way",
        "department": "IT",
        "lines": [
            {
                "line_no": 1,
                "description": "Laptop 14 inch",
                "quantity": "4",
                "unit_price": "3999.99",
            },
            {"line_no": 2, "description": "USB-C dock", "quantity": "10", "unit_price": "412.345"},
        ],
    }
    body.update(extra)
    return body


def test_number_key_and_strict_decimals() -> None:
    assert number_key("PO-2026-0079") == number_key(" po 2026/0079 ") == "PO20260079"
    assert number_key("  ") is None
    assert parse_decimal("12.5", positive=True) is not None
    for bad in ("-1", "+1", "1e3", "1,000", "0", "1.23456", "NaN", "", " "):
        assert parse_decimal(bad, positive=True) is None, bad
    assert parse_decimal("0", positive=False) is not None


async def test_create_purchase_order_and_receipt(
    pay_api: PayApi, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    api = pay_api
    vendor = await _vendor(api, world)
    po = await api.ok("POST", "/v1/pay/purchase-orders", world, "clerk", status=201,
                      json=_po(vendor["id"]))  # fmt: skip
    assert po["status"] == "open"
    assert po["match_type"] == "3-way"
    assert po["vendor_reference"] == vendor["reference"]
    assert [line["line_total"] for line in po["lines"]] == ["15999.96", "4123.45"]
    assert po["subtotal"] == "20123.41"
    assert po["receipt_count"] == 0
    assert "pay.purchase_order.created" in await audit_actions(owner_engine, world.org_id, po["id"])

    # The same number, however it is typed, is taken.
    taken = await api.call("POST", "/v1/pay/purchase-orders", world, "clerk",
                           json=_po(vendor["id"], number="po 2026 0079"))  # fmt: skip
    assert taken.status_code == 409
    assert taken.json()["code"] == "number_taken"

    # Strict input: a negative quantity, an unknown field, an unknown vendor.
    bad = _po(vendor["id"], number="PO-X1")
    bad["lines"][0]["quantity"] = "-1"
    assert (
        await api.call("POST", "/v1/pay/purchase-orders", world, "clerk", json=bad)
    ).status_code == 422
    unknown = await api.call(
        "POST", "/v1/pay/purchase-orders", world, "clerk",
        json=_po("00000000-0000-0000-0000-000000000001", number="PO-X2"),
    )  # fmt: skip
    assert unknown.status_code == 400
    assert unknown.json()["errors"][0]["code"] == "vendor_not_found"

    receipt = {
        "grn_number": "GRN-2026-0001",
        "po_id": po["id"],
        "received_date": "2026-09-28",
        "received_by": "Store keeper",
        "lines": [{"po_line_no": 1, "quantity": "3"}],
    }
    grn = await api.ok("POST", "/v1/pay/goods-receipts", world, "clerk", status=201, json=receipt)
    assert grn["line_count"] == 1
    assert "pay.goods_receipt.created" in await audit_actions(owner_engine, world.org_id, grn["id"])
    # One more than ordered, counting the first receipt: refused.
    over = receipt | {"grn_number": "GRN-2026-0002", "lines": [{"po_line_no": 1, "quantity": "2"}]}
    response = await api.call("POST", "/v1/pay/goods-receipts", world, "clerk", json=over)
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "more_than_ordered"
    missing = receipt | {
        "grn_number": "GRN-2026-0003",
        "lines": [{"po_line_no": 9, "quantity": "1"}],
    }
    response = await api.call("POST", "/v1/pay/goods-receipts", world, "clerk", json=missing)
    assert response.json()["errors"][0]["code"] == "po_line_not_found"

    detail = await api.ok("GET", f"/v1/pay/purchase-orders/{po['id']}", world, "auditor")
    assert [(x["line_no"], x["received"], x["invoiced"]) for x in detail["lines"]] == [
        (1, "3", "0"),
        (2, "0", "0"),
    ]
    assert [r["grn_number"] for r in detail["receipts"]] == ["GRN-2026-0001"]
    listed = await api.ok("GET", "/v1/pay/purchase-orders?q=0079", world, "approver")
    assert [p["po_number"] for p in listed["items"]] == ["PO-2026-0079"]
    assert listed["items"][0]["receipt_count"] == 1

    # Cancelled POs receive nothing.
    await owner_rows(owner_engine, world.org_id,
                     "UPDATE pay_purchase_orders SET status = 'cancelled', closed_at = now(), "
                     "closed_by_type = 'user', closed_by_id = 'test' WHERE id = :i",
                     {"i": po["id"]})  # fmt: skip
    late = receipt | {"grn_number": "GRN-2026-0004"}
    response = await api.call("POST", "/v1/pay/goods-receipts", world, "clerk", json=late)
    assert response.json()["errors"][0]["code"] == "po_cancelled"


async def test_csv_import_is_strict_and_all_or_nothing(
    pay_api: PayApi, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    api = pay_api
    first = await _vendor(api, world, 0)
    second = await _vendor(api, world, 1)
    good = (
        "po_number,vendor_reference,currency,order_date,match_type,line_no,description,"
        "quantity,unit_price\n"
        f"PO-1,{first['reference']},AED,2026-09-01,3-way,1,Paper A4,100,12.50\n"
        f"PO-1,{first['reference']},AED,2026-09-01,3-way,2,Toner,4,310\n"
        f"PO-2,{second['reference']},USD,2026-09-02,2-way,1,Consulting day,5,1200.00\n"
    )
    bad = (
        good
        + f"PO-3,{first['reference']},AED,2026-09-03,3-way,1,=HYPERLINK(1),1,1\n"
        + f"PO-4,{first['reference']},AED,2026-13-01,3-way,1,Chair,1e3,1\n"
        + "PO-5,VEN-NOPE00,AED,2026-09-03,3-way,1,Desk,2,-5\n"
        + f"PO-1,{second['reference']},AED,2026-09-01,3-way,3,Pens,1,1\n"
    )
    response = await api.call("POST", "/v1/pay/purchase-orders/import", world, "clerk",
                              json={"csv": bad})  # fmt: skip
    assert response.status_code == 400
    found = {(e["row"], e["field"], e["code"]) for e in response.json()["errors"]}
    assert {
        (5, "description", "formula_not_allowed"),
        (6, "order_date", "invalid_date"),
        (6, "quantity", "invalid_quantity"),
        (7, "vendor_reference", "vendor_not_found"),
        (7, "unit_price", "invalid_unit_price"),
        (8, "vendor_reference", "differs_within_po"),
    } <= found
    # Nothing was created.
    assert (await api.ok("GET", "/v1/pay/purchase-orders", world, "clerk"))["items"] == []

    unknown = await api.call("POST", "/v1/pay/purchase-orders/import", world, "clerk",
                             json={"csv": "po_number,colour\nPO-9,red\n"})  # fmt: skip
    codes = {(e["field"], e["code"]) for e in unknown.json()["errors"]}
    assert ("colour", "unknown_column") in codes
    assert ("vendor_reference", "missing_column") in codes

    done = await api.ok("POST", "/v1/pay/purchase-orders/import", world, "clerk",
                        json={"csv": good})  # fmt: skip
    assert done["created"] == 2
    assert sorted(done["numbers"]) == ["PO-1", "PO-2"]
    again = await api.call("POST", "/v1/pay/purchase-orders/import", world, "clerk",
                           json={"csv": good})  # fmt: skip
    assert again.status_code == 400
    assert {e["code"] for e in again.json()["errors"]} == {"number_taken"}

    receipts = (
        "grn_number,po_number,received_date,received_by,po_line_no,quantity\n"
        "GRN-1,PO-1,2026-09-10,Store,1,60\n"
        "GRN-1,PO-1,2026-09-10,Store,2,4\n"
        "GRN-2,PO-1,2026-09-12,Store,1,40\n"
    )
    over = receipts + "GRN-3,PO-1,2026-09-13,Store,1,1\n"
    response = await api.call("POST", "/v1/pay/goods-receipts/import", world, "clerk",
                              json={"csv": over})  # fmt: skip
    assert response.status_code == 400
    assert {(e["row"], e["code"]) for e in response.json()["errors"]} == {(5, "more_than_ordered")}
    done = await api.ok("POST", "/v1/pay/goods-receipts/import", world, "clerk",
                        json={"csv": receipts})  # fmt: skip
    assert done["created"] == 2
    rows = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT source, count(*) AS n FROM pay_goods_receipts GROUP BY source",
    )
    assert [(r.source, r.n) for r in rows] == [("csv", 2)]
    (po1,) = [p for p in (await api.ok("GET", "/v1/pay/purchase-orders", world, "clerk"))["items"]
              if p["po_number"] == "PO-1"]  # fmt: skip
    assert po1["subtotal"] == "2490.00"
    assert po1["source"] == "csv"


async def test_permissions_and_tenant_isolation(
    pay_api: PayApi, world: PayWorld, other: PayWorld
) -> None:
    api = pay_api
    vendor = await _vendor(api, world)
    body = _po(vendor["id"])
    assert (
        await api.call("POST", "/v1/pay/purchase-orders", world, "approver", json=body)
    ).status_code == 403
    assert (
        await api.call("POST", "/v1/pay/purchase-orders", world, "auditor", json=body)
    ).status_code == 403
    assert (await api.call("GET", "/v1/pay/purchase-orders", world, "recruiter")).status_code == 403
    po = await api.ok("POST", "/v1/pay/purchase-orders", world, "clerk", status=201, json=body)

    # The other organisation sees nothing and cannot use this vendor or PO.
    assert (await api.ok("GET", "/v1/pay/purchase-orders", other, "clerk"))["items"] == []
    assert (
        await api.call("GET", f"/v1/pay/purchase-orders/{po['id']}", other, "clerk")
    ).status_code == 404
    response = await api.call("POST", "/v1/pay/purchase-orders", other, "clerk", json=body)
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "vendor_not_found"
    receipt = {
        "grn_number": "GRN-X",
        "po_id": po["id"],
        "received_date": "2026-09-28",
        "lines": [{"po_line_no": 1, "quantity": "1"}],
    }
    assert (
        await api.call("POST", "/v1/pay/goods-receipts", other, "clerk", json=receipt)
    ).status_code == 404
    # The same number is free in another organisation.
    other_vendor = await _vendor(api, other)
    await api.ok("POST", "/v1/pay/purchase-orders", other, "clerk", status=201,
                 json=_po(other_vendor["id"]))  # fmt: skip
