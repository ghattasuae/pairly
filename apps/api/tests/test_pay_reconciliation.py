# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Bank reconciliation on Postgres, offline, with synthetic statements. No bank.

* A CSV statement is matched with the paid items of a run: by end-to-end id and
  amount, by the vendor's IBAN and amount when the reference says nothing, never by
  the amount alone; credits are ignored; an unknown debit stays unmatched.
* The same file twice is refused; a file with any problem imports nothing and lists
  every problem.
* A camt.053 batch debit naming the run matches the whole run.
* A line imported before the payment was marked paid is matched by the hourly job.
* Paid invoices no statement line explains are listed after ``days``.
* Permissions, audit, masked IBANs and tenant isolation.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker
from pairly_core.jobs.registry import JobContext
from pairly_pay.reconciliation import _org_ids, reconcile_org

from .pay_flow_support import Flow, flow, fother, fworld
from .pay_support import PayWorld, owner_rows
from .test_pay_payment_runs import (
    CLEAN,
    PAYER,
    _approve_run,
    _approved,
    _export,
    _iban,
    _propose,
    _settings,
)

__all__ = ["flow", "fother", "fworld"]  # fixtures

PATH = "/v1/pay/reconciliation"
CAMT = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"
UNKNOWN_IBAN = "GB82WEST12345698765432"


async def _exported_run(
    flow: Flow, fworld: PayWorld, owner: AsyncEngine, docs: tuple[str, ...]
) -> dict[str, Any]:
    """An exported run of ``docs``; ``doc_of`` maps each invoice id to its document."""
    await _settings(flow, fworld, **PAYER)
    doc_of: dict[str, str] = {}
    for doc in docs:
        invoice_id, _vendor = await _approved(flow, owner, fworld, doc)
        doc_of[invoice_id] = doc
    run = await _propose(flow, fworld)
    await _approve_run(flow, fworld, run)
    assert (await _export(flow, fworld, run["id"])).status_code == 200
    out = dict(await flow.ok("GET", f"/v1/pay/payment-runs/{run['id']}", fworld, "clerk"))
    out["doc_of"] = doc_of
    return out


async def _mark_paid(flow: Flow, fworld: PayWorld, run_id: str, reference: str) -> None:
    await flow.ok(
        "POST",
        f"/v1/pay/payment-runs/{run_id}/paid",
        fworld,
        "clerk",
        json={"reference": reference},
    )


def _csv(rows: list[tuple[str, str, str, str, str]]) -> str:
    lines = ["date,amount,currency,reference,counterparty_iban"]
    lines += [",".join(r) for r in rows]
    return "\n".join(lines) + "\n"


async def _upload(flow: Flow, world: PayWorld, fmt: str, content: str, who: str = "clerk") -> Any:
    return await flow.call(
        "POST",
        f"{PATH}/statements",
        world,
        who,
        json={"format": fmt, "content": content},
    )


async def test_a_csv_statement_matches_paid_items_by_reference_or_iban_never_amount_alone(
    flow: Flow, fworld: PayWorld, fother: PayWorld, owner_engine: AsyncEngine
) -> None:
    run = await _exported_run(flow, fworld, owner_engine, (CLEAN, "inv-0030"))
    await _mark_paid(flow, fworld, run["id"], "BANK-REF-77")
    first, second = run["items"]
    second_iban = _iban(run["doc_of"][second["invoice_id"]])
    content = _csv(
        [
            # By the end-to-end id the bank file carried, and the amount.
            ("2026-10-21", f"-{first['amount']}", "AED", f"PMT {first['end_to_end_id']}", ""),
            # No reference at all: the vendor's IBAN and the amount.
            ("2026-10-21", f"-{second['amount']}", "AED", "", second_iban),
            # The same amount as the first, but no reference and an unknown IBAN.
            ("2026-10-22", f"-{first['amount']}", "AED", "TRANSFER", UNKNOWN_IBAN),
            # Money in: not Pay's to explain.
            ("2026-10-22", "1500.00", "AED", "Customer receipt", ""),
        ]
    )
    response = await _upload(flow, fworld, "csv", content)
    assert response.status_code == 201, response.text
    imported = response.json()
    assert imported["matched"] == 2
    assert imported["unmatched"] == 1
    statement = imported["statement"]
    assert statement["format"] == "csv"
    assert statement["line_count"] == 4
    assert len(statement["file_sha256"]) == 64

    report = await flow.ok("GET", PATH, fworld, "approver")
    assert report["can_upload"] is False
    assert report["matched_count"] == 2
    assert report["unmatched_count"] == 1
    assert report["ignored_count"] == 1
    by_invoice = {m["invoice_id"]: m for m in report["matched"]}
    assert by_invoice[first["invoice_id"]]["match_reasons"] == ["amount", "reference"]
    assert by_invoice[first["invoice_id"]]["match_kind"] == "item"
    assert by_invoice[first["invoice_id"]]["payment_run_reference"] == run["reference"]
    assert by_invoice[second["invoice_id"]]["match_reasons"] == ["amount", "iban"]
    (left,) = report["unmatched"]
    assert left["unmatched_reason"] == "no_candidate"
    assert left["counterparty_iban_masked"] == "GB** **** 5432"
    assert UNKNOWN_IBAN not in str(report)
    assert second_iban not in str(report)
    assert report["unexplained"] == []

    # The IBAN is encrypted at rest; the blind index is what matching compares.
    stored = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT counterparty_iban, counterparty_iban_bidx FROM pay_bank_statement_lines "
        "WHERE counterparty_iban IS NOT NULL",
    )
    assert stored
    assert all(UNKNOWN_IBAN not in r.counterparty_iban for r in stored)
    assert all(len(r.counterparty_iban_bidx) == 64 for r in stored)

    # The same file twice is refused.
    again = await _upload(flow, fworld, "csv", content)
    assert again.status_code == 409, again.text
    assert "statement_already_imported" in again.text

    audited = await owner_rows(
        owner_engine,
        fworld.org_id,
        "SELECT action, payload FROM audit_events WHERE action LIKE 'pay.%statement%' ORDER BY seq",
    )
    actions = [r.action for r in audited]
    assert audited[-1].payload["sha256"] == statement["file_sha256"]
    assert "pay.bank_statement.imported" in actions
    assert actions.count("pay.statement_line.matched") == 2

    # Permissions: an approver reads, never imports.
    assert (await _upload(flow, fworld, "csv", content, who="approver")).status_code == 403
    # Another organisation sees none of it.
    other = await flow.ok("GET", PATH, fother, "clerk")
    assert other["statements"] == []
    assert other["matched"] == []
    assert other["unmatched"] == []


async def test_a_bad_statement_imports_nothing_and_lists_every_problem(
    flow: Flow, fworld: PayWorld
) -> None:
    content = _csv(
        [
            ("2026-13-01", "-10.00", "AED", "x", ""),
            ("2026-10-01", "-10.005", "AED", "x", ""),
            ("2026-10-01", "1e3", "AED", "x", ""),
            ("2026-10-01", "0", "AED", "x", ""),
            ("2026-10-01", "-5.00", "aed", "x", ""),
            ("2026-10-01", "-5.00", "AED", "x", "AE000000000000000000000"),
            ("2026-10-01", "-5.00", "AED", "=HYPERLINK(1)", ""),
        ]
    )
    response = await _upload(flow, fworld, "csv", content)
    assert response.status_code == 400, response.text
    codes = {(e["row"], e["field"], e["code"]) for e in response.json()["errors"]}
    assert codes == {
        (2, "date", "invalid_date"),
        (3, "amount", "invalid_amount"),
        (4, "amount", "invalid_amount"),
        (5, "amount", "invalid_amount"),
        (6, "currency", "invalid_currency"),
        (7, "counterparty_iban", "invalid_iban"),
        (8, "reference", "formula_not_allowed"),
    }
    missing = await _upload(flow, fworld, "csv", "date,amount,currency\n2026-10-01,-5.00,AED\n")
    assert missing.status_code == 400
    assert {e["field"] for e in missing.json()["errors"]} == {"reference", "counterparty_iban"}
    xxe = (
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        f'<Document xmlns="{CAMT}"><BkToCstmrStmt>&x;</BkToCstmrStmt></Document>'
    )
    refused = await _upload(flow, fworld, "camt053", xxe)
    assert refused.status_code == 400
    assert refused.json()["errors"][0]["code"] == "malformed_xml"
    report = await flow.ok("GET", PATH, fworld, "clerk")
    assert report["statements"] == []


def _camt(entries: list[str]) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Document xmlns="{CAMT}"><BkToCstmrStmt>'
        "<GrpHdr><MsgId>SYNTH-1</MsgId></GrpHdr><Stmt><Id>STMT-2026-10-22</Id>"
        "<Acct><Id><IBAN>AE070331234567890123456</IBAN></Id></Acct>"
        + "".join(entries)
        + "</Stmt></BkToCstmrStmt></Document>"
    )


def _entry(amount: str, indicator: str, day: str, reference: str, status: str = "BOOK") -> str:
    return (
        f'<Ntry><Amt Ccy="AED">{amount}</Amt><CdtDbtInd>{indicator}</CdtDbtInd>'
        f"<Sts>{status}</Sts><BookgDt><Dt>{day}</Dt></BookgDt>"
        f"<NtryDtls><TxDtls><RmtInf><Ustrd>{reference}</Ustrd></RmtInf></TxDtls></NtryDtls>"
        "</Ntry>"
    )


async def test_a_camt053_batch_debit_matches_the_whole_run_and_the_job_matches_later(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    run = await _exported_run(flow, fworld, owner_engine, (CLEAN, "inv-0030"))
    total = sum((Decimal(i["amount"]) for i in run["items"]), Decimal(0))
    content = _camt(
        [
            _entry(f"{total:.2f}", "DBIT", "2026-10-21", f"Payment run {run['reference']}"),
            _entry("99.00", "DBIT", "2026-10-21", "pending", status="PDNG"),
            _entry("10.00", "CRDT", "2026-10-21", "interest"),
        ]
    )
    # Imported before anything was marked paid: nothing to match yet.
    response = await _upload(flow, fworld, "camt053", content)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["matched"] == 0
    assert body["unmatched"] == 1
    assert body["statement"]["line_count"] == 2  # the pending entry is skipped
    assert body["statement"]["bank_statement_id"] == "STMT-2026-10-22"
    assert body["statement"]["account_masked"] == "AE** **** 3456"

    await _mark_paid(flow, fworld, run["id"], "BATCH-1")
    ctx = JobContext(
        sessionmaker=create_sessionmaker(flow.engine),
        runtime=flow.runtime,
        registry=flow.runtime.registry,
    )
    assert fworld.org_id in await _org_ids(ctx)
    assert await reconcile_org(ctx, fworld.org_id) == {"matched": 1, "unmatched": 0}
    assert fworld.org_id not in await _org_ids(ctx)
    report = await flow.ok("GET", PATH, fworld, "finance")
    (matched,) = report["matched"]
    assert matched["match_kind"] == "run"
    assert matched["payment_run_id"] == run["id"]
    assert matched["invoice_id"] is None
    assert report["unexplained"] == []


async def test_paid_invoices_without_a_statement_line_are_listed_after_days(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    run = await _exported_run(flow, fworld, owner_engine, (CLEAN,))
    await _mark_paid(flow, fworld, run["id"], "LATE-1")
    report = await flow.ok("GET", PATH, fworld, "clerk")
    assert report["unexplained"] == []  # paid just now
    ten_days_ago = dt.datetime.now(dt.UTC) - dt.timedelta(days=10)
    await owner_rows(
        owner_engine,
        fworld.org_id,
        "UPDATE pay_payment_run_items SET paid_at = :t WHERE payment_run_id = :r",
        {"t": ten_days_ago, "r": run["id"]},
    )
    report = await flow.ok("GET", f"{PATH}?days=5", fworld, "clerk")
    (late,) = report["unexplained"]
    assert late["invoice_id"] == run["items"][0]["invoice_id"]
    assert late["payment_run_reference"] == run["reference"]
    assert late["days_since_paid"] == 10
    assert report["unexplained_count"] == 1
    assert (await flow.ok("GET", f"{PATH}?days=30", fworld, "clerk"))["unexplained"] == []
    assert (await flow.call("GET", f"{PATH}?days=0", fworld, "clerk")).status_code == 422
