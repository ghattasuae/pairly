# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pay analytics (brief 4.9) through the API on Postgres, offline, on a hand-built dataset.

Four invoices are uploaded (recorded extraction) and then set by hand to known states:
touchless and paid on time, a bank-mismatch fraud flag rejected, a duplicate held for a
person, and one a person reviewed and paid late. One bank-detail change is rejected and
one stays open. The analytics read exactly that back, link each number to its rows,
stay inside the organisation, and need ``pay.analytics.read`` (finance controllers and
admins).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from . import test_pay_invoices as base
from .pay_support import PayWorld, other_bank, owner_rows, vendor_body
from .test_pay_invoices import Api, dataset

api = base.api
world = base.world
other = base.other

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def _check(name: str, code: str, severity: str) -> dict[str, Any]:
    return {
        "check": name,
        "status": "fail",
        "error": None,
        "findings": [
            {
                "code": code,
                "rule": code,
                "severity": severity,
                "message": {"en": code, "ar": code},
                "evidence": {},
                "original_id": None,
                "related_ids": [],
                "needs_review": True,
            }
        ],
    }


async def _invoice(
    api: Api, world: PayWorld, owner: AsyncEngine, doc_id: str, account: uuid.UUID, **values: Any
) -> str:
    created = await api.upload_doc(world, doc_id)
    columns = {
        "status": "ready",
        "created_at": T0,
        "review_reasons": [],
        "checks": [],
        "approved_at": None,
        "approved_by_rule": False,
        "paid_at": None,
        "reviewed_by": None,
        "invoice_date": None,
        "due_date": None,
        "currency": "AED",
        "total": None,
    } | values
    if columns["approved_at"] is not None:
        # What a real approval binds to the invoice (the table refuses an approved
        # invoice without it); synthetic values.
        columns |= {
            "approved_account_id": account,
            "approved_bank_digest": "b" * 64,
            "approved_checks_digest": "c" * 64,
            "approved_amount": columns["total"],
            "approved_currency": columns["currency"],
        }
    if columns["paid_at"] is not None:
        columns |= {"paid_by": world.id("finance"), "payment_run_id": await _run(owner, world)}
    sets = []
    params: dict[str, Any] = {"i": uuid.UUID(created["id"])}
    for name, value in columns.items():
        if name in {"review_reasons", "checks"}:
            sets.append(f"{name} = CAST(:{name} AS jsonb)")
            params[name] = json.dumps(value)
        else:
            sets.append(f"{name} = :{name}")
            params[name] = value
    await owner_rows(
        owner,
        world.org_id,
        f"UPDATE pay_invoices SET {', '.join(sets)} WHERE id = :i",  # noqa: S608 (fixed names)
        params,
    )
    return str(created["id"])


async def _run(owner: AsyncEngine, world: PayWorld) -> uuid.UUID:
    """A payment run row for paid invoices to belong to (the table requires one)."""
    run_id = uuid.uuid4()
    await owner_rows(
        owner,
        world.org_id,
        "INSERT INTO pay_payment_runs (id, org_id, reference, status, currency, cash_limit, "
        "total, item_count, execution_date, proposed_by) VALUES (:id, :o, :r, "
        "'awaiting_approval', 'AED', 100000, 0, 0, :d, :p)",
        {
            "id": run_id,
            "o": world.org_id,
            "r": f"PR-{run_id.hex[:6].upper()}",
            "d": date(2026, 9, 2),
            "p": world.id("finance"),
        },
    )
    return run_id


def _metric(data: dict[str, Any], key: str) -> dict[str, Any]:
    for value in data.values():
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict) and item.get("key") == key:
                return item
    raise AssertionError(key)


async def test_pay_analytics_on_a_hand_built_dataset(
    api: Api, world: PayWorld, other: PayWorld, owner_engine: AsyncEngine
) -> None:
    docs = [s.record.id for s in dataset().docs if s.record.format == "pdf"][:4]
    assert len(docs) == 4
    vendor = await api.ok(
        "POST", "/v1/pay/vendors", world, "clerk", status=201, json=vendor_body(0)
    )
    (account,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT id FROM pay_vendor_bank_accounts WHERE vendor_id = :v AND status = 'active'",
        {"v": uuid.UUID(vendor["id"])},
    )
    touchless = await _invoice(
        api, world, owner_engine, docs[0], account.id,
        status="paid", approved_at=T0 + timedelta(hours=2), approved_by_rule=True,
        paid_at=T0 + timedelta(hours=26), invoice_date=date(2026, 8, 31),
        due_date=date(2026, 9, 30), total=1000,
    )  # fmt: skip
    fraud = await _invoice(
        api, world, owner_engine, docs[1], account.id,
        status="rejected", review_reasons=["check:bank_mismatch", "risk:critical"],
        checks=[_check("bank", "bank_mismatch", "critical")], total=48000,
    )  # fmt: skip
    duplicate = await _invoice(
        api, world, owner_engine, docs[2], account.id,
        status="needs_review", review_reasons=["check:duplicate_invoice_number"],
        checks=[_check("duplicates", "duplicate_invoice_number", "high")], total=500,
    )  # fmt: skip
    late = await _invoice(
        api, world, owner_engine, docs[3], account.id,
        status="paid", review_reasons=["extraction:low_confidence:total"],
        reviewed_by=world.id("clerk"), approved_at=T0 + timedelta(hours=6),
        paid_at=datetime(2026, 9, 10, 9, 0, tzinfo=UTC), invoice_date=date(2026, 8, 1),
        due_date=date(2026, 9, 2), total=200,
    )  # fmt: skip

    tickets = []
    for index in (5, 6):
        opened = await api.ok(
            "POST",
            f"/v1/pay/vendors/{vendor['id']}/bank-changes",
            world,
            "clerk",
            status=201,
            json={"bank": other_bank(index), "source": "email"},
        )
        tickets.append(opened["id"])
        if index == 5:
            await owner_rows(
                owner_engine,
                world.org_id,
                "UPDATE pay_bank_change_tickets SET status = 'rejected' WHERE id = :t",
                {"t": uuid.UUID(opened["id"])},
            )

    # Who may see it: finance controllers and admins.
    for name in ("clerk", "approver", "auditor"):
        denied = await api.client.get("/v1/pay/analytics", headers=api.headers(world, name))
        assert denied.status_code == 403, name
    admin = await api.ok("GET", "/v1/pay/analytics", world, "admin")
    data = await api.ok("GET", "/v1/pay/analytics", world, "finance")
    assert data["invoices"] == 4 == admin["invoices"]

    rate = data["touchless_rate"]
    assert (rate["value"], rate["n"]) == (0.5, 2)
    assert [r["id"] for r in rate["records"]] == [touchless]
    assert rate["records"][0]["kind"] == "invoice"
    assert _metric(data, "cycle.receipt_to_approval")["value"] == 4.0
    assert _metric(data, "exceptions.all")["value"] == 0.75
    assert [r["id"] for r in _metric(data, "exceptions.check.bank")["records"]] == [fraud]
    prevented = _metric(data, "duplicates_prevented")
    assert prevented["value"] == 1.0
    assert [r["id"] for r in prevented["records"]] == [duplicate]
    assert _metric(data, "duplicates_prevented_value.AED")["detail"]["amount"] == "500.00"
    blocked = data["bank_changes_blocked"]
    assert blocked["value"] == 1.0
    assert [(r["kind"], r["id"]) for r in blocked["records"]] == [("bank_change", tickets[0])]
    assert blocked["detail"]["open"] == 1
    assert _metric(data, "fraud_flags.rejected")["value"] == 1.0
    on_time = data["on_time_payment_rate"]
    assert (on_time["value"], on_time["detail"]["late"]) == (0.5, 1)
    assert {r["id"] for r in data["days_payable_outstanding"]["records"]} == {touchless, late}
    assert data["days_payable_outstanding"]["value"] == 21.0
    # No Pay approvals ran for these rows: no data, not 0.
    assert data["approval_sla_breaches"]["value"] is None
    cost = data["cost_per_invoice"]
    if cost["n"]:
        # Recorded mode: the calls replay recordings and cost 0, and it says so.
        assert cost["value"] == 0.0
        assert cost["detail"]["recorded_mode"] is True
    else:
        assert cost["value"] is None

    # A period with nothing in it: no data everywhere, never 0.
    empty = await api.ok(
        "GET", "/v1/pay/analytics?since=2020-01-01&until=2020-01-31", world, "finance"
    )
    assert empty["invoices"] == 0
    assert empty["touchless_rate"]["value"] is None
    assert empty["bank_changes_blocked"]["value"] is None
    assert all(m["value"] is None for m in empty["fraud_flags"])
    bad = await api.client.get(
        "/v1/pay/analytics?since=2026-10-02&until=2026-10-01", headers=api.headers(world, "finance")
    )
    assert bad.status_code == 400

    # Another organisation sees none of it.
    theirs = await api.ok("GET", "/v1/pay/analytics", other, "finance")
    assert theirs["invoices"] == 0
    assert theirs["bank_changes_blocked"]["records"] == []


async def test_cash_forecast_by_week_net_of_credits_with_review_apart(
    api: Api, world: PayWorld, other: PayWorld, owner_engine: AsyncEngine
) -> None:
    docs = [s.record.id for s in dataset().docs if s.record.format == "pdf"][:5]
    vendor = await api.ok(
        "POST", "/v1/pay/vendors", world, "clerk", status=201, json=vendor_body(0)
    )
    (account,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT id FROM pay_vendor_bank_accounts WHERE vendor_id = :v AND status = 'active'",
        {"v": uuid.UUID(vendor["id"])},
    )
    today = datetime.now(UTC).date()
    monday = today - timedelta(days=today.weekday())
    overdue = await _invoice(
        api, world, owner_engine, docs[0], account.id,
        status="approved", approved_at=T0, due_date=today - timedelta(days=20), total=100,
    )  # fmt: skip
    credited = await _invoice(
        api, world, owner_engine, docs[1], account.id,
        status="approved", approved_at=T0, due_date=monday + timedelta(days=15), total=1000,
        credit_total=250,
    )  # fmt: skip
    review = await _invoice(
        api, world, owner_engine, docs[2], account.id,
        status="needs_review", due_date=monday + timedelta(days=16), total=400,
    )  # fmt: skip
    credit_note = await _invoice(
        api, world, owner_engine, docs[3], account.id,
        status="needs_review", due_date=monday + timedelta(days=15), total=90,
        document_type="credit_note",
    )  # fmt: skip
    undated = await _invoice(
        api, world, owner_engine, docs[4], account.id, status="approved", approved_at=T0, total=70,
    )  # fmt: skip

    for name in ("clerk", "approver", "auditor"):
        denied = await api.client.get("/v1/pay/forecast", headers=api.headers(world, name))
        assert denied.status_code == 403, name
    too_long = await api.client.get(
        "/v1/pay/forecast?weeks=27", headers=api.headers(world, "finance")
    )
    assert too_long.status_code == 422
    data = await api.ok("GET", "/v1/pay/forecast", world, "finance")
    assert data["weeks"] == 12
    assert data["today"] == today.isoformat()
    (aed,) = data["currencies"]
    assert aed["currency"] == "AED"
    weeks = aed["weeks"]
    assert len(weeks) == 12
    assert weeks[0]["start"] == monday.isoformat()
    assert weeks[0]["committed"]["amount"] == "100.00"
    assert [i["id"] for i in weeks[0]["committed"]["invoices"]] == [overdue]
    assert weeks[0]["committed"]["invoices"][0]["expected_date"] == today.isoformat()
    third = weeks[2]
    assert third["committed"]["amount"] == "750.00"
    assert (third["committed"]["gross"], third["committed"]["credits"]) == ("1000.00", "250.00")
    assert [i["id"] for i in third["committed"]["invoices"]] == [credited]
    assert third["in_review"]["amount"] == "400.00"
    assert [i["id"] for i in third["in_review"]["invoices"]] == [review]
    assert aed["committed_total"] == "850.00"
    assert aed["in_review_total"] == "400.00"
    assert [i["id"] for i in aed["undated_committed"]["invoices"]] == [undated]
    listed = {
        i["id"] for w in weeks for line in ("committed", "in_review") for i in w[line]["invoices"]
    }
    assert credit_note not in listed  # a credit note pays nothing
    # Another organisation sees none of it.
    theirs = await api.ok("GET", "/v1/pay/forecast", other, "finance")
    assert theirs["currencies"] == []
