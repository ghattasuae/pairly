# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Three-place currencies through approval and a payment run (WP4.7), offline.

A Bahraini dinar invoice (three decimals) keeps its last digit everywhere an amount is
bound: the invoice approval payload and context, the payment run's approval payload,
its items digest, the run detail and the bank file. Before, every one of these was
rounded to two places, so the approvers approved an amount that was not the invoice's.

The invoice is a UBL e-invoice (read without a model): the synthetic one-line invoice
``inv-0019`` with its currency and amounts rewritten to BHD.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_pay.bank_files import amount_text
from pairly_pay.checks.settings import amount_display
from pairly_pay.checks.settings import amount_text as payload_amount

from .pay_flow_support import (
    Flow,
    approval_row,
    approve_all,
    flow,
    fworld,
    make_vendor,
    submit,
)
from .pay_support import PayWorld
from .test_pay_invoices import blob

__all__ = ["flow", "fworld"]  # fixtures

DOC = "inv-0019"  # one line, no PO, UBL
SUBTOTAL = "1234.567"
VAT = "61.728"  # 5% of 1234.567 = 61.72835, half up to three places
TOTAL = "1296.295"  # two places would be 1296.30


def bhd_invoice() -> bytes:
    xml = blob(DOC).decode("utf-8")
    for old, new in (
        ("USD", "BHD"),
        ("50784.78", SUBTOTAL),
        ("2539.24", VAT),
        ("53324.02", TOTAL),
        ("<cbc:ID>20261373</cbc:ID>", "<cbc:ID>BH-2026-0001</cbc:ID>"),
    ):
        assert old in xml, old
        xml = xml.replace(old, new)
    return xml.encode("utf-8")


async def _ready(flow: Flow, world: PayWorld) -> tuple[str, str]:
    vendor_id = await make_vendor(flow, world, DOC)
    response = await flow.client.post(
        "/v1/pay/invoices",
        files={"file": ("bh-2026-0001.xml", bhd_invoice(), "application/xml")},
        headers=flow.headers(world, "clerk"),
    )
    assert response.status_code == 202, response.text
    invoice_id = str(response.json()["id"])
    detail = await flow.invoice(world, invoice_id)
    if detail["status"] == "needs_review":
        detail = await flow.ok(
            "POST",
            f"/v1/pay/invoices/{invoice_id}/review",
            world,
            "clerk2",
            json={"note": "Checked against the paper copy."},
        )
    assert detail["status"] == "ready", detail["review_reasons"]
    assert detail["currency"] == "BHD"
    assert Decimal(str(detail["total"])) == Decimal(TOTAL)
    return invoice_id, vendor_id


def test_amounts_keep_the_currency_minor_unit() -> None:
    assert payload_amount(Decimal("1296.2950"), "BHD") == "1296.295"
    assert payload_amount(Decimal("1296.2954"), "KWD") == "1296.295"
    assert payload_amount(Decimal("1296.2955"), "OMR") == "1296.296"  # half up
    assert payload_amount(Decimal("1296.295"), "AED") == "1296.30"
    assert payload_amount(Decimal("1296"), "BHD") == "1296.000"
    assert payload_amount(None, "BHD") is None
    assert amount_display(Decimal("51234.5675"), "BHD") == "51,234.568"
    assert amount_display(Decimal("51234.5"), "AED") == "51,234.50"
    # The bank file and the payloads agree to the digit.
    assert amount_text(Decimal("1296.2955"), "BHD") == payload_amount(Decimal("1296.2955"), "BHD")


async def test_bhd_invoice_through_approval_and_a_payment_run(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    invoice_id, _ = await _ready(flow, fworld)
    sub = await submit(flow, fworld, invoice_id)
    approval_id = sub["approval"]["approval_id"]
    row = await approval_row(owner_engine, fworld, approval_id)
    assert row.payload["amount"] == TOTAL
    assert row.payload["currency"] == "BHD"
    assert row.context["fields"]["total"] == TOTAL
    assert row.context["fields"]["subtotal"] == SUBTOTAL
    assert row.context["fields"]["vat_amount"] == VAT
    await approve_all(flow, owner_engine, fworld, approval_id)
    assert (await flow.invoice(fworld, invoice_id))["status"] == "approved"

    await flow.ok(
        "PUT",
        "/v1/pay/settings",
        fworld,
        "admin",
        json={
            "cooling_off_hours": 0,
            "payments": {
                "payer_name": "Acme Trading LLC",
                "payer_iban": "AE070331234567890123456",
                "payer_bic": "NBADAEAA",
            },
        },
    )
    run = await flow.ok(
        "POST",
        "/v1/pay/payment-runs",
        fworld,
        "finance",
        status=201,
        json={"currency": "BHD", "cash_limit": "1296.2955", "execution_date": "2026-10-20"},
    )
    assert [i["invoice_id"] for i in run["items"]] == [invoice_id]
    assert Decimal(str(run["total"])) == Decimal(TOTAL)
    assert Decimal(str(run["items"][0]["amount"])) == Decimal(TOTAL)
    run_row = await approval_row(owner_engine, fworld, run["approval_id"])
    assert run_row.payload["total"] == TOTAL
    assert run_row.payload["cash_limit"] == "1296.296"
    assert run_row.context["items"][0]["amount"] == TOTAL

    response = await flow.decide(fworld, "finance2", run["approval_id"])
    assert response.status_code == 200, response.text
    approved = await flow.ok("GET", f"/v1/pay/payment-runs/{run['id']}", fworld, "finance")
    assert approved["status"] == "approved"  # the items digest held at three places
    exported = await flow.call(
        "POST",
        f"/v1/pay/payment-runs/{run['id']}/export",
        fworld,
        "clerk",
        json={"format": "csv"},
    )
    assert exported.status_code == 200, exported.text
    (line,) = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8"))))
    assert line["amount"] == TOTAL
    assert line["currency"] == "BHD"
