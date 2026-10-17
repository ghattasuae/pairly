# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""GL coding and cost centre suggestions (brief 4.5.3) on Postgres, offline.

* Lists: the chart of accounts and the cost centres are imported from CSV as versions
  (a second import is version 2), only by people holding ``pay.coding.manage`` with a
  second factor; a bad CSV creates nothing and names every problem.
* Coding a line: codes must be in the lists in force; the checks run again in the same
  transaction and the coding is part of their digest; audited.
* Learning: a later invoice of the vendor gets suggestions from the coded lines, with
  confidence; a correction of a suggestion is filed and weighs more next time.
* Auto-coding, off by default: when the organisation turns it on, high confidence lines
  are coded at intake (source ``auto``), nothing else.
* The optional model suggestion answers from the recordings and is never applied.
* Tenant isolation: another organisation's invoice is unknown.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker, set_tenant
from pairly_pay.ai_recordings import import_ai_recordings
from pairly_pay.coding_demo import cc_csv, gl_csv

from .pay_flow_support import Flow, flow, fother, fworld, make_vendor, upload
from .pay_support import PayWorld, audit_actions, owner_rows

__all__ = ["flow", "fother", "fworld"]  # fixtures

FIRST, SECOND, THIRD = "inv-0028", "inv-0029", "inv-0030"  # one vendor (VEN-003)
CLEAN = "inv-0165"


async def _lists(flow: Flow, world: PayWorld) -> None:
    await flow.ok(
        "POST", "/v1/pay/coding/gl-accounts/import", world, "finance", status=201,
        json={"csv": gl_csv()},
    )  # fmt: skip
    await flow.ok(
        "POST", "/v1/pay/coding/cost-centres/import", world, "finance", status=201,
        json={"csv": cc_csv()},
    )  # fmt: skip


def _line(coding: dict[str, Any], description: str) -> dict[str, Any]:
    (found,) = [x for x in coding["lines"] if x["description"] == description]
    assert isinstance(found, dict)
    return found


async def _coding(flow: Flow, world: PayWorld, invoice_id: str) -> dict[str, Any]:
    body = await flow.ok("GET", f"/v1/pay/invoices/{invoice_id}/coding", world, "clerk")
    assert isinstance(body, dict)
    return body


async def _code(
    flow: Flow, world: PayWorld, invoice_id: str, lines: list[dict[str, Any]], **kw: Any
) -> Any:
    return await flow.call(
        "PUT", f"/v1/pay/invoices/{invoice_id}/coding/lines", world, "clerk",
        json={"lines": lines}, **kw,
    )  # fmt: skip


async def test_lists_are_versioned_and_need_the_manage_permission(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    csv = "code,name,name_ar\n6100,Office supplies,\n6200,Telecommunications,الاتصالات\n"
    path = "/v1/pay/coding/gl-accounts/import"
    clerk = await flow.call("POST", path, fworld, "clerk", json={"csv": csv})
    assert clerk.status_code == 403
    no_otp = await flow.call("POST", path, fworld, "finance", otp=False, json={"csv": csv})
    assert no_otp.status_code == 403
    assert no_otp.json()["code"] == "second_factor_required"

    bad = "code,name\n6100,Office supplies\n6100,Again\n=1+1,Formula\n,Empty code\n"
    refused = await flow.call("POST", path, fworld, "finance", json={"csv": bad})
    assert refused.status_code == 400
    codes = {(e["row"], e["code"]) for e in refused.json()["errors"]}
    assert (3, "duplicate_code") in codes
    assert any(code == "formula_not_allowed" for _, code in codes)

    first = await flow.ok("POST", path, fworld, "finance", status=201, json={"csv": csv})
    assert (first["version"], first["entry_count"], first["source"]) == (1, 2, "csv")
    assert first["entries"][1] == {
        "code": "6200",
        "name": "Telecommunications",
        "name_ar": "الاتصالات",
    }
    second = await flow.ok(
        "POST", "/v1/pay/coding/gl-accounts", fworld, "admin", status=201,
        json={"entries": [{"code": "6300", "name": "Repairs"}]},
    )  # fmt: skip
    assert second["version"] == 2
    lists = await flow.ok("GET", "/v1/pay/coding", fworld, "clerk")
    assert lists["gl_accounts"]["version"] == 2
    assert [e["code"] for e in lists["gl_accounts"]["entries"]] == ["6300"]
    assert lists["cost_centres"] is None
    assert lists["can_manage"] is False
    versions = await flow.ok("GET", "/v1/pay/coding/gl-accounts/versions", fworld, "auditor")
    assert [v["version"] for v in versions] == [2, 1]
    actions = await owner_rows(
        owner_engine, fworld.org_id,
        "SELECT action FROM audit_events WHERE action = 'pay.coding_list.created'",
    )  # fmt: skip
    assert len(actions) == 2


async def test_coding_learns_from_history_and_corrections(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    await _lists(flow, fworld)
    await make_vendor(flow, fworld, FIRST)
    first = await upload(flow, fworld, FIRST)
    coding = await _coding(flow, fworld, first)
    assert coding["gl_list_version"] == 1
    assert coding["can_code"] is True
    assert coding["auto_coding"] is False
    assert all(x["suggestion"] is None and x["gl_code"] is None for x in coding["lines"])
    digest_before = await _digest(owner_engine, fworld, first)

    # Codes must be in the lists in force.
    wrong = await _code(flow, fworld, first, [{"line_index": 0, "gl_code": "9999"}])
    assert wrong.status_code == 400
    assert wrong.json()["errors"][0]["code"] == "code_not_in_list"
    missing = await _code(flow, fworld, first, [{"line_index": 99, "gl_code": "6100"}])
    assert missing.status_code == 400

    by_text = {x["description"]: x["line_index"] for x in coding["lines"]}
    lever, paper, board = (
        by_text["Lever arch files"],
        by_text["A4 copy paper, box of 5 reams"],
        by_text["Whiteboard 120 x 90 cm"],
    )
    response = await _code(
        flow, fworld, first,
        [
            {"line_index": lever, "gl_code": "6100", "cost_centre": "CC-ADM"},
            {"line_index": paper, "gl_code": "6100", "cost_centre": "CC-ADM"},
            {"line_index": board, "gl_code": "6100", "cost_centre": "CC-FAC"},
        ],
    )  # fmt: skip
    assert response.status_code == 200, response.text
    coded = response.json()
    assert coded["checks_state"] == "current"
    assert _line(coded, "Lever arch files")["source"] == "person"
    # The coding is part of what the checks read: the digest changed, and they ran again.
    assert await _digest(owner_engine, fworld, first) != digest_before
    actions = await audit_actions(owner_engine, fworld.org_id, first)
    assert "pay.invoice.lines_coded" in actions
    assert actions.index("pay.invoice.rechecked") > actions.index("pay.invoice.lines_coded")

    # The next invoice of the vendor: suggestions from the coded lines, with confidence.
    second = await upload(flow, fworld, SECOND)
    coding = await _coding(flow, fworld, second)
    suggested = _line(coding, "Lever arch files")["suggestion"]
    assert (suggested["gl_code"], suggested["cost_centre"]) == ("6100", "CC-ADM")
    assert suggested["method"] == "history"
    assert suggested["level"] == "medium"  # one line backs it
    assert suggested["examples"]
    assert _line(coding, "Ergonomic office chair")["suggestion"] is None

    # A person accepts one suggestion and corrects another: the correction is filed.
    lines = {x["description"]: x["line_index"] for x in coding["lines"]}
    lever, board = lines["Lever arch files"], lines["Whiteboard 120 x 90 cm"]
    response = await _code(
        flow, fworld, second,
        [
            {"line_index": lever, "gl_code": "6100", "cost_centre": "CC-ADM"},
            {"line_index": board, "gl_code": "1500", "cost_centre": "CC-FAC"},
        ],
    )  # fmt: skip
    assert response.status_code == 200, response.text
    assert _line(response.json(), "Whiteboard 120 x 90 cm")["corrected"] is True
    assert _line(response.json(), "Lever arch files")["corrected"] is False
    (correction,) = await owner_rows(
        owner_engine, fworld.org_id,
        "SELECT suggested, chosen_gl, corrected_by FROM pay_coding_corrections "
        "WHERE invoice_id = :i",
        {"i": second},
    )  # fmt: skip
    assert correction.suggested["gl_code"] == "6100"
    assert correction.chosen_gl == "1500"
    assert correction.corrected_by == fworld.id("clerk")

    # Auto-coding (off by default) turned on: the high confidence line is coded at
    # intake, the others are not.
    await flow.ok(
        "PUT", "/v1/pay/settings", fworld, "finance",
        json={"cooling_off_hours": 24, "checks": {"auto_code_high_confidence": True}},
    )  # fmt: skip
    third = await upload(flow, fworld, THIRD)
    coding = await _coding(flow, fworld, third)
    assert coding["auto_coding"] is True
    lever_line = _line(coding, "Lever arch files")
    assert (lever_line["gl_code"], lever_line["source"]) == ("6100", "auto")
    assert lever_line["suggestion"]["level"] == "high"  # two lines back it
    assert _line(coding, "Ergonomic office chair")["gl_code"] is None
    assert "pay.invoice.auto_coded" in await audit_actions(owner_engine, fworld.org_id, third)
    assert coding["checks_state"] == "current"  # the checks read the auto coding


async def test_model_suggestion_is_shown_never_applied(
    flow: Flow, fworld: PayWorld, owner_engine: AsyncEngine
) -> None:
    session = create_sessionmaker(owner_engine)()
    async with session:
        set_tenant(session, fworld.org_id, None)
        async with session.begin():
            await import_ai_recordings(session, fworld.org_id)
    await _lists(flow, fworld)
    await make_vendor(flow, fworld, CLEAN)
    clean = await upload(flow, fworld, CLEAN)
    coding = await _coding(flow, fworld, clean)
    paper = _line(coding, "A4 copy paper, box of 5 reams")
    assert paper["suggestion"] is None
    asked = await flow.ok(
        "POST", f"/v1/pay/invoices/{clean}/coding/model-suggestions", fworld, "clerk",
        json={"line_indexes": [paper["line_index"]]},
    )  # fmt: skip
    (answer,) = asked["lines"]
    assert answer["problem"] is None, answer
    assert answer["from_recording"] is True
    assert answer["suggestion"]["method"] == "model"
    assert (answer["suggestion"]["gl_code"], answer["suggestion"]["cost_centre"]) == (
        "6100",
        "CC-ADM",
    )
    # Nothing was applied.
    assert (
        _line(await _coding(flow, fworld, clean), "A4 copy paper, box of 5 reams")["gl_code"]
        is None
    )
    # An auditor may read the coding but not ask the model or code.
    assert (
        await flow.call(
            "POST", f"/v1/pay/invoices/{clean}/coding/model-suggestions", fworld, "auditor",
            json={},
        )
    ).status_code == 403  # fmt: skip


async def test_coding_is_closed_after_rejection_and_isolated_per_organisation(
    flow: Flow, fworld: PayWorld, fother: PayWorld
) -> None:
    await _lists(flow, fworld)
    await make_vendor(flow, fworld, FIRST)
    invoice_id = await upload(flow, fworld, FIRST)
    assert (
        await flow.call("GET", f"/v1/pay/invoices/{invoice_id}/coding", fother, "clerk")
    ).status_code == 404
    other = await flow.call(
        "PUT", f"/v1/pay/invoices/{invoice_id}/coding/lines", fother, "clerk",
        json={"lines": [{"line_index": 0, "gl_code": "6100"}]},
    )  # fmt: skip
    assert other.status_code == 404
    await flow.ok(
        "POST", f"/v1/pay/invoices/{invoice_id}/reject", fworld, "clerk",
        json={"reason": "Not ours."},
    )  # fmt: skip
    closed = await _code(flow, fworld, invoice_id, [{"line_index": 0, "gl_code": "6100"}])
    assert closed.status_code == 409
    assert (await _coding(flow, fworld, invoice_id))["can_code"] is False


async def _digest(owner: AsyncEngine, world: PayWorld, invoice_id: str) -> str:
    (row,) = await owner_rows(
        owner, world.org_id, "SELECT checks_digest FROM pay_invoices WHERE id = :i",
        {"i": invoice_id},
    )  # fmt: skip
    return str(row.checks_digest)
