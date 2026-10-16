# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The generic journal export on Postgres, offline, with synthetic invoices.

* Approved invoices and payments of the period, one balanced entry each, at the
  currency's precision; the payables credited equal the approved amounts.
* A second factor is needed; only admins and finance controllers may export.
* Every download is recorded with its SHA-256 and audited; tenant isolation.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from .pay_flow_support import Flow, flow, fother, fworld
from .pay_support import PayWorld, owner_rows
from .test_pay_payment_runs import (
    CLEAN,
    PAYER,
    _approve_run,
    _approved,
    _export,
    _propose,
    _settings,
)

__all__ = ["flow", "fother", "fworld"]  # fixtures

PATH = "/v1/pay/journal/export"


def _rows(response: Any) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8"))))


async def test_the_journal_balances_per_entry_and_every_export_is_recorded(
    flow: Flow, fworld: PayWorld, fother: PayWorld, owner_engine: AsyncEngine
) -> None:
    await _settings(flow, fworld, **PAYER)
    first, _ = await _approved(flow, owner_engine, fworld, CLEAN)
    second, _ = await _approved(flow, owner_engine, fworld, "inv-0030")
    run = await _propose(flow, fworld)
    await _approve_run(flow, fworld, run)
    assert (await _export(flow, fworld, run["id"])).status_code == 200
    await flow.ok(
        "POST", f"/v1/pay/payment-runs/{run['id']}/paid", fworld, "clerk", json={"reference": "B-1"}
    )
    period = datetime.now(UTC).strftime("%Y-%m")

    # A second factor is needed, and the permission.
    no_otp = await flow.call("POST", PATH, fworld, "finance", otp=False, json={"period": period})
    assert no_otp.status_code == 403
    assert (
        await flow.call("POST", PATH, fworld, "clerk", json={"period": period})
    ).status_code == 403
    assert (
        await flow.call("POST", PATH, fworld, "finance", json={"period": "2026-13"})
    ).status_code == 422

    response = await flow.call("POST", PATH, fworld, "finance", json={"period": period})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    digest = hashlib.sha256(response.content).hexdigest()
    assert response.headers["x-content-sha256"] == digest
    rows = _rows(response)
    debit: dict[str, Decimal] = defaultdict(Decimal)
    credit: dict[str, Decimal] = defaultdict(Decimal)
    for r in rows:
        for field in ("debit", "credit"):
            assert r[field].count(".") == 1
            assert len(r[field].split(".")[1]) == 2  # AED: two places
        debit[r["entry"]] += Decimal(r["debit"])
        credit[r["entry"]] += Decimal(r["credit"])
    assert debit == credit
    assert {r["journal"] for r in rows} == {"AP_INVOICE", "AP_PAYMENT"}

    details = {i: await flow.invoice(fworld, i) for i in (first, second)}
    for invoice_id, detail in details.items():
        mine = [r for r in rows if r["reference"] == detail["reference"]]
        invoice_rows = [r for r in mine if r["journal"] == "AP_INVOICE"]
        payables = [r for r in invoice_rows if r["account_code"] == "AP_CONTROL"]
        assert [Decimal(r["credit"]) for r in payables] == [Decimal(detail["total"])]
        assert {r["document_id"] for r in mine} == {detail["document_id"]}
        payment = [r for r in mine if r["journal"] == "AP_PAYMENT"]
        assert {r["account_code"] for r in payment} == {"AP_CONTROL", "BANK"}
        assert invoice_id  # every invoice has both entries

    records = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT period, sha256, entry_count, line_count FROM pay_journal_exports",
    )
    (record,) = records
    assert record.period == period
    assert record.sha256 == digest
    assert record.entry_count == 4
    assert record.line_count == len(rows)
    audited = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT payload FROM audit_events WHERE action = 'pay.journal.exported'",
    )
    assert audited[-1].payload["sha256"] == digest
    listed = await flow.ok("GET", f"/v1/pay/journal/exports?period={period}", fworld, "auditor")
    assert [x["sha256"] for x in listed] == [digest]

    # Another organisation exports an empty journal and sees no record of this one.
    other = await flow.call("POST", PATH, fother, "finance", json={"period": period})
    assert other.status_code == 200
    assert _rows(other) == []
    assert (
        await flow.ok("GET", f"/v1/pay/journal/exports?period={period}", fother, "finance")
        != listed
    )
    # An earlier period has nothing.
    empty = await flow.call("POST", PATH, fworld, "admin", json={"period": "2026-01"})
    assert _rows(empty) == []
