# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pay role dashboards (brief 5.1 items 8 to 10) on Postgres, offline.

* The AP clerk sees the intake queue and their own throughput; the approver what waits
  for them (with the risk level and the SLA), their history and the spend they
  approved this month; the finance controller payables, aging, runs, the forecast,
  the analytics and the month-end status. Every item links to its record.
* Each dashboard needs its permission; another organisation sees none of it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from .pay_flow_support import (
    Flow,
    approve_all,
    current_assignees,
    flow,
    fother,
    fworld,
    ready_invoice,
    submit,
)
from .pay_support import PayWorld
from .test_pay_payment_runs import CLEAN, _propose

__all__ = ["flow", "fother", "fworld"]  # fixtures

BASE = "/v1/pay/dashboards"


def _section(board: dict[str, Any], key: str) -> dict[str, Any]:
    found = [s for s in (*board["action"], *board["sections"]) if s["key"] == key]
    assert found, key
    return found[0]


def _metric(board: dict[str, Any], key: str) -> dict[str, Any]:
    return next(m for m in board["metrics"] if m["key"] == key)


async def test_role_dashboards_follow_the_work_and_the_permissions(
    flow: Flow, fworld: PayWorld, fother: PayWorld, owner_engine: AsyncEngine
) -> None:
    ready_id, _ = await ready_invoice(flow, fworld, CLEAN)
    clerk = await flow.ok("GET", f"{BASE}/ap-clerk", fworld, "clerk")
    assert clerk["role"] == "ap_clerk"
    ready = _section(clerk, "ready")
    assert ready_id in {i["id"] for i in ready["items"]}
    assert all(i["href"] == f"/pay/invoices/{i['id']}" for i in ready["items"])
    for key in ("to_review", "low_confidence", "duplicates", "waiting_on_vendor", "intake"):
        section = _section(clerk, key)
        assert section["count"] >= len(section["items"])
    assert _metric(clerk, "submitted_7d")["value"] == 0

    sub = await submit(flow, fworld, ready_id)
    approval_id = sub["approval"]["approval_id"]
    clerk = await flow.ok("GET", f"{BASE}/ap-clerk", fworld, "clerk")
    sent = _metric(clerk, "submitted_7d")
    assert sent["value"] == 1
    assert [r["id"] for r in sent["records"]] == [ready_id]
    # Throughput is personal: another clerk's board does not count it.
    other_clerk = await flow.ok("GET", f"{BASE}/ap-clerk", fworld, "clerk2")
    assert _metric(other_clerk, "submitted_7d")["value"] == 0

    assignees = await current_assignees(owner_engine, fworld, approval_id)
    decider = next(
        p for p in ("approver", "approver2", "finance") if str(fworld.id(p)) in assignees
    )
    board = await flow.ok("GET", f"{BASE}/approver", fworld, decider)
    waiting = _section(board, "awaiting_me")
    (item,) = [i for i in waiting["items"] if i["id"] == approval_id]
    assert item["href"] == f"/inbox/{approval_id}"
    assert item["detail"]["risk"] in {"low", "medium", "high", "critical"}
    assert item["detail"]["sla"] in {"on_track", "due_soon", "overdue"}
    assert item["detail"]["due_at"]

    used = await approve_all(flow, owner_engine, fworld, approval_id)
    board = await flow.ok("GET", f"{BASE}/approver", fworld, used[0])
    assert approval_id not in {i["id"] for i in _section(board, "awaiting_me")["items"]}
    assert approval_id in {i["id"] for i in _section(board, "history")["items"]}
    detail = await flow.invoice(fworld, ready_id)
    (spend,) = [a for a in board["amounts"] if a["key"] == "approved_this_month"]
    assert (spend["currency"], Decimal(spend["amount"]), spend["count"]) == (
        "AED",
        Decimal(detail["total"]),
        1,
    )

    run = await _propose(flow, fworld)
    finance = await flow.ok("GET", f"{BASE}/finance-controller", fworld, "finance")
    runs = _section(finance, "upcoming_runs")
    assert run["id"] in {i["id"] for i in runs["items"]}
    payables = [a for a in finance["amounts"] if a["key"] == "payables"]
    assert [(a["currency"], Decimal(a["amount"])) for a in payables] == [
        ("AED", Decimal(detail["total"]))
    ]
    aging = [a for a in finance["amounts"] if a["key"].startswith("aging.")]
    assert sum((Decimal(a["amount"]) for a in aging), Decimal(0)) == Decimal(detail["total"])
    assert {m["key"] for m in finance["metrics"]} >= {
        "touchless_rate",
        "cost_per_invoice",
        "days_payable_outstanding",
        "bank_changes_blocked",
    }
    checks = {i["id"]: i["detail"] for i in _section(finance, "month_end")["items"]}
    assert checks["journal_exported"]["done"] is False
    assert checks["statements_matched"]["done"] is True
    assert len(_section(finance, "exceptions_trend")["items"]) == 4

    # Permissions: each dashboard needs its own.
    for path, who in (
        ("ap-clerk", "approver"),
        ("ap-clerk", "auditor"),
        ("approver", "clerk"),
        ("finance-controller", "clerk"),
        ("finance-controller", "approver"),
    ):
        assert (await flow.call("GET", f"{BASE}/{path}", fworld, who)).status_code == 403, path
    # Tenant isolation: another organisation's boards are empty.
    other = await flow.ok("GET", f"{BASE}/finance-controller", fother, "finance")
    assert other["amounts"] == []
    assert _section(other, "upcoming_runs")["items"] == []
    other_clerk_board = await flow.ok("GET", f"{BASE}/ap-clerk", fother, "clerk")
    assert all(s["count"] == 0 for s in other_clerk_board["action"])
