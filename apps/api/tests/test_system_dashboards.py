# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Shared and system dashboards (brief 5.1 items 12 to 15) on Postgres, offline.

* Auditor: the chain checked now, the audit log, approvals, overrides, AI decisions and
  (Pay) bank-change tickets with their verification evidence, never the phone number.
* Admin: members, invitations, integrations, rules, policies, templates, budget,
  retention, and Hire's data requests and calendar integrations (second factor).
* AI governance: eval results read from the files (a missing file fails its metrics,
  a missing directory says so), usage, overrides, versions, guardrails.
* Platform operations: only the system organisation's admins; numbers only, no tenant
  content; ``GET /v1/ops/jobs`` lists ``job_runs``.
* Each board needs its permission and only ever shows the viewer's organisation.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from pathlib import Path
from typing import Any

from pairly_core.dashboards.model import (
    Board,
    DashboardSectionSpec,
    Section,
    SectionContext,
    build_section_registry,
)
from pairly_core.db import create_sessionmaker
from pairly_core.guardrails.redaction_counts import RedactionTally, flush
from pairly_core.jobs.runs import finish_run, start_run
from pairly_core.llm.store import DatabaseCallStore
from pairly_core.plugins import discover_plugins

from .pay_support import PayApi, PayWorld, other, pay_api, world
from .test_pay_bank_changes import _call, _open, _vendor

__all__ = ["other", "pay_api", "world"]  # fixtures

BASE = "/v1/dashboards"


def _all(board: dict[str, Any]) -> list[dict[str, Any]]:
    return [*board["action"], *board["sections"]]


def _section(board: dict[str, Any], key: str) -> dict[str, Any]:
    found = [s for s in _all(board) if s["key"] == key]
    assert found, (key, [s["key"] for s in _all(board)])
    return found[0]


def _keys(board: dict[str, Any]) -> set[str]:
    return {s["key"] for s in _all(board)}


def _hrefs_are_app_paths(board: dict[str, Any]) -> None:
    for s in _all(board):
        for href in [s["href"], *(i["href"] for i in s["items"]), *(t["href"] for t in s["stats"])]:
            assert href is None or (href.startswith("/") and not href.startswith("//")), href


# ------------------------------------------------------------------ auditor


async def test_auditor_dashboard(pay_api: PayApi, world: PayWorld, other: PayWorld) -> None:
    api = pay_api
    vendor = await _vendor(api, world)
    ticket = await _open(api, world, vendor)
    called = await _call(api, world, ticket, "confirmed")
    assert called.status_code == 200, called.text

    board = await api.ok("GET", f"{BASE}/auditor", world, "auditor", otp=False)
    assert board["board"] == "auditor"
    _hrefs_are_app_paths(board)
    assert {"integrity", "anchors", "audit_log", "approvals_history", "overrides"} <= _keys(board)
    assert {"ai_decisions", "bank_changes"} <= _keys(board)
    integrity = _section(board, "integrity")
    assert integrity["source"] == "checked_now"
    assert integrity["items"][0]["status"] == "ok"
    assert integrity["count"] == 0
    assert "chain_alert" not in _keys(board)
    assert _section(board, "audit_log")["count"] > 0
    # Anchors are not configured in tests: said, not shown as zero.
    anchors = _section(board, "anchors")
    assert (anchors["count"], anchors["note"]) == (None, "not_configured")

    changes = _section(board, "bank_changes")
    (item,) = [i for i in changes["items"] if i["id"] == ticket["id"]]
    assert item["href"] == f"/pay/bank-changes/{ticket['id']}"
    assert item["detail"]["verified"] is True
    assert item["detail"]["last_call"] == "confirmed"
    last4 = item["detail"]["called_last4"]
    assert isinstance(last4, str)
    assert len(last4) == 4
    assert changes["workflow"] == "pay"
    # Evidence, not the number itself and not the bank details.
    raw = json.dumps(board)
    assert "+971" not in raw
    assert "IBAN" not in raw.upper().replace("IBAN_", "")

    # Another organisation's auditor sees none of it.
    theirs = await api.ok("GET", f"{BASE}/auditor", other, "auditor", otp=False)
    assert ticket["id"] not in json.dumps(theirs)
    assert _section(theirs, "bank_changes")["count"] == 0
    # The board needs core.audit.read.
    refused = await api.call("GET", f"{BASE}/auditor", world, "clerk")
    assert refused.status_code == 403

    # A chain too long to check on every visit shows the latest stored verification
    # (none yet here: said, not assumed fine).
    services = api.app.state.decision_services
    api.app.state.decision_services = dataclasses.replace(services, large_chain_events=0)
    try:
        stored = await api.ok("GET", f"{BASE}/auditor", world, "auditor", otp=False)
    finally:
        api.app.state.decision_services = services
    integrity = _section(stored, "integrity")
    assert integrity["source"] == "latest_stored_verification"
    assert (integrity["count"], integrity["note"], integrity["items"]) == (None, "no_data", [])


# ------------------------------------------------------------------ admin


async def test_admin_dashboard(pay_api: PayApi, world: PayWorld, other: PayWorld) -> None:
    api = pay_api
    created = await api.ok(
        "POST",
        "/v1/invitations",
        world,
        "admin",
        status=201,
        json={"email": f"new-{uuid.uuid4().hex[:6]}@example.test", "role": "auditor"},
    )
    invited = created["invitation"]
    board = await api.ok("GET", f"{BASE}/admin", world, "admin")
    assert board["board"] == "admin"
    _hrefs_are_app_paths(board)
    assert {
        "members",
        "pending_invitations",
        "integrations",
        "approval_rules",
        "policies",
        "templates",
        "budget",
        "retention",
        "data_requests",
        "calendar_integrations",
    } <= _keys(board)
    pending = _section(board, "pending_invitations")
    assert invited["id"] in {i["id"] for i in pending["items"]}
    members = _section(board, "members")
    assert members["count"] == len(world.people)
    integrations = {i["id"]: i["status"] for i in _section(board, "integrations")["items"]}
    assert set(integrations) == {"email", "slack"}
    assert integrations["slack"] == "needs_setup"
    calendars = {i["id"] for i in _section(board, "calendar_integrations")["items"]}
    assert calendars == {"google", "microsoft", "zoom"}
    budget = {s["key"]: s for s in _section(board, "budget")["stats"]}
    assert budget["monthly_budget"]["value"] is not None
    assert _section(board, "budget")["note"] == "no_data"  # no model call this month
    assert _section(board, "retention")["count"] >= 1

    # Without a second factor the Hire settings sections (hire.settings.manage) are left
    # out; the board itself still answers.
    plain = await api.ok("GET", f"{BASE}/admin", world, "admin", otp=False)
    assert not {"data_requests", "calendar_integrations"} & _keys(plain)
    # Tenant isolation: the other organisation's admin does not see this invitation.
    theirs = await api.ok("GET", f"{BASE}/admin", other, "admin")
    assert invited["id"] not in json.dumps(theirs)
    assert _section(theirs, "members")["count"] == len(other.people)
    # Only admins.
    for who in ("auditor", "clerk", "finance"):
        assert (await api.call("GET", f"{BASE}/admin", world, who)).status_code == 403


async def test_a_failing_plugin_section_does_not_break_the_board(
    pay_api: PayApi, world: PayWorld
) -> None:
    async def broken(ctx: SectionContext) -> Section:
        raise RuntimeError("boom")

    class Plugin:
        id = "pay"
        dashboard_sections = (
            DashboardSectionSpec(Board.ADMIN, "broken_part", "pay.invoice.read", broken),
        )

    api = pay_api
    saved = api.app.state.dashboard_sections
    api.app.state.dashboard_sections = build_section_registry([Plugin()])
    try:
        board = await api.ok("GET", f"{BASE}/admin", world, "admin")
    finally:
        api.app.state.dashboard_sections = saved
    broken_part = _section(board, "broken_part")
    assert (broken_part["count"], broken_part["note"]) == (None, "unavailable")
    assert _section(board, "members")["count"] == len(world.people)


# ------------------------------------------------------------------ AI governance


def _evals_dir(root: Path, *, live: bool) -> Path:
    evals = root / "evals"
    (evals / "results").mkdir(parents=True)
    (evals / "thresholds.yaml").write_text(
        "version: 1\nmetrics:\n"
        "  - {id: a.rate, file: a.json, path: rate, min: 0.9, why: x}\n"
        "  - {id: a.flag, file: a.json, path: ok, equals: true, why: x}\n"
        "  - {id: a.model, file: a.json, path: model, threshold: after_first_live_baseline, "
        "why: x}\n"
        "  - {id: b.rate, file: b.json, path: rate, min: 0.5, why: x}\n",
        encoding="utf-8",
    )
    (evals / "results" / "a.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-10-01T10:00:00Z",
                "rate": 0.95,
                "ok": True,
                "model": {"status": "pending_live_run"},
            }
        ),
        encoding="utf-8",
    )
    if live:
        (evals / "results" / "a_live.json").write_text(
            json.dumps({"rate": 0.5, "ok": True, "model": 0.8}), encoding="utf-8"
        )
    return evals


async def test_ai_governance_dashboard(
    pay_api: PayApi, world: PayWorld, other: PayWorld, tmp_path: Path
) -> None:
    api = pay_api
    api.app.state.evals_dir = _evals_dir(tmp_path, live=True)
    try:
        board = await api.ok("GET", f"{BASE}/ai-governance", world, "admin")
        auditor = await api.ok("GET", f"{BASE}/ai-governance", world, "auditor", otp=False)
    finally:
        api.app.state.evals_dir = None
    assert board["board"] == "ai_governance"
    _hrefs_are_app_paths(board)
    assert {
        "evals",
        "model_usage",
        "budget",
        "human_overrides",
        "versions_in_production",
        "pii_redactions",
        "guardrails_pay",
        "guardrails_hire",
        "fairness_runs",
        "calibration",
    } <= _keys(board)
    evals = _section(board, "evals")
    files = {i["id"]: i for i in evals["items"] if i["kind"] == "eval_file"}
    assert set(files) == {"a.json", "a_live.json", "b.json"}
    assert files["a.json"]["detail"] == {
        "mode": "recorded",
        "time_source": "generated_at",
        "pass": 2,
        "fail": 0,
        "pending": 1,
        "info": 0,
    }
    assert files["b.json"]["status"] == "missing"  # a missing file fails its metrics
    assert files["b.json"]["detail"]["fail"] == 1
    assert files["a_live.json"]["detail"]["mode"] == "live"
    assert files["a_live.json"]["detail"]["time_source"] == "file_modified"
    failing = {i["label"]: i["detail"] for i in evals["items"] if i["kind"] == "eval_metric"}
    assert failing["b.rate"]["why"] == "missing_file"
    assert failing["a.rate"]["value"] == 0.5  # the live value, as in the file
    assert failing["a.model"]["why"] == "no_threshold_yet"
    stats = {s["key"]: s for s in evals["stats"]}
    assert stats["gate_recorded"]["text"] == "fail"  # b.json is missing
    assert stats["gate_live"]["text"] == "fail"
    assert "eval_gate_failing" in {s["key"] for s in board["action"]}
    # Nothing counted yet is shown as no data, never as zero.
    pii = _section(board, "pii_redactions")
    assert (pii["count"], pii["note"], pii["stats"][0]["value"]) == (None, "no_data", None)
    assert _section(board, "model_usage")["note"] == "no_data"
    assert board["links"] == [{"key": "model_cards", "href": "docs/model-cards/"}]
    # The auditor sees it too, Hire's guardrail and calibration counts included (they
    # need hire.governance.read, not hire.analytics.read), with no link to the
    # analytics pages and no job title.
    assert {"evals", "fairness_runs", "guardrails_hire", "calibration"} <= _keys(auditor)
    assert {"guardrails_pay", "pii_redactions"} <= _keys(auditor)
    calibration = _section(auditor, "calibration")
    assert calibration["href"] is None
    assert all(i["href"] is None and i["label"] == i["id"] for i in calibration["items"])
    assert all(t["href"] is None for t in calibration["stats"])
    assert _section(board, "calibration")["href"] == "/hire/calibration"
    # Others do not; another organisation's board is its own.
    assert (await api.call("GET", f"{BASE}/ai-governance", world, "clerk")).status_code == 403
    theirs = await api.ok("GET", f"{BASE}/ai-governance", other, "admin")
    assert theirs["board"] == "ai_governance"


def _call_row(world: PayWorld, **extra: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "org_id": world.org_id,
        "chain_id": uuid.uuid4(),
        "attempt": 1,
        "task": "pay_gl_coding",
        "prompt_version": "1",
        "prompt_hash": "a" * 64,
        "provider": "anthropic",
        "model": "claude-test",
        "tier": "small",
        "schema_name": "Coding",
        "schema_hash": "b" * 64,
        "request_hash": "c" * 64,
        "mode": "live",
        "status": "ok",
        "request": {"inputs": {}, "settings": {}},
        "response": {"ok": True},
        "workflow": "pay",
        "input_tokens": 100,
        "output_tokens": 20,
    }
    values.update(extra)
    return values


async def test_reused_answers_are_counted_apart_from_spend(
    pay_api: PayApi, world: PayWorld, other: PayWorld, tmp_path: Path
) -> None:
    """One paid call and one reused answer of it (``reused_from``, zero cost): both
    boards count two calls, one reused, and the cost of the paid one only."""
    api = pay_api
    store = DatabaseCallStore()
    session = await api.in_org(world)
    async with session, session.begin():
        original = await store.insert(session, _call_row(world, cost_micros=2500))
        await store.insert(
            session, _call_row(world, cost_micros=0, latency_ms=0, reused_from=original)
        )
    api.app.state.evals_dir = _evals_dir(tmp_path, live=False)
    try:
        governance = await api.ok("GET", f"{BASE}/ai-governance", world, "admin")
    finally:
        api.app.state.evals_dir = None
    usage = _section(governance, "model_usage")
    assert usage["count"] == 2
    stats = {s["key"]: s for s in usage["stats"]}
    assert stats["model_calls_30d"]["value"] == 2
    assert (stats["reused_calls_30d"]["value"], stats["reused_calls_30d"]["n"]) == (1, 2)
    assert (stats["model_cost_30d"]["value"], stats["model_cost_30d"]["n"]) == (0.0025, 1)
    (item,) = usage["items"]
    assert (item["detail"]["calls"], item["detail"]["reused"]) == (2, 1)
    assert item["detail"]["cost_usd"] == 0.0025

    admin = await api.ok("GET", f"{BASE}/admin", world, "admin")
    budget = _section(admin, "budget")
    got = {s["key"]: s for s in budget["stats"]}
    assert (budget["count"], budget["note"]) == (2, None)
    assert (got["spent_this_month"]["value"], got["spent_this_month"]["n"]) == (0.0025, 1)
    assert got["model_calls_this_month"]["value"] == 2
    assert got["reused_calls_this_month"]["value"] == 1
    # Another organisation counts nothing of it.
    theirs = _section(await api.ok("GET", f"{BASE}/admin", other, "admin"), "budget")
    assert (theirs["count"], theirs["note"]) == (0, "no_data")


async def test_ai_governance_counts_pii_redactions(
    pay_api: PayApi, world: PayWorld, other: PayWorld
) -> None:
    api = pay_api
    sessionmaker = api.app.state.pairly.sessionmaker
    tally = RedactionTally()
    tally.add(world.org_id, 3)
    tally.add(str(world.org_id), 4)
    tally.add(None, 9)  # a platform log: no organisation, not counted
    result = await flush(sessionmaker, tally)
    assert (result.rows, result.redactions, result.failed_orgs) == (1, 7, 0)
    tally.add(world.org_id, 2)  # a second flush adds to the day's row
    assert (await flush(sessionmaker, tally)).redactions == 2
    assert tally.pending() == 0

    for role, otp in (("admin", True), ("auditor", False)):
        board = await api.ok("GET", f"{BASE}/ai-governance", world, role, otp=otp)
        pii = _section(board, "pii_redactions")
        assert (pii["count"], pii["note"], pii["source"]) == (9, None, "pii_redaction_counts")
        assert (pii["stats"][0]["value"], pii["stats"][0]["n"]) == (9.0, 1)
    # Another organisation's count is its own (Row Level Security).
    theirs = _section(
        await api.ok("GET", f"{BASE}/ai-governance", other, "admin"), "pii_redactions"
    )
    assert (theirs["count"], theirs["note"]) == (None, "no_data")


async def test_ai_governance_without_eval_files(
    pay_api: PayApi, world: PayWorld, tmp_path: Path
) -> None:
    api = pay_api
    api.app.state.evals_dir = tmp_path / "nowhere"
    try:
        board = await api.ok("GET", f"{BASE}/ai-governance", world, "admin")
    finally:
        api.app.state.evals_dir = None
    evals = _section(board, "evals")
    assert (evals["count"], evals["note"], evals["items"]) == (None, "no_evals_directory", [])
    assert "eval_gate_failing" not in {s["key"] for s in board["action"]}


# ------------------------------------------------------------------ platform operations


def _system(api: PayApi, org: uuid.UUID | None) -> None:
    ctx = api.app.state.pairly
    settings = ctx.settings.model_copy(update={"system_org_id": org})
    api.app.state.pairly = dataclasses.replace(ctx, settings=settings)


async def test_ops_dashboard_is_for_the_system_organisation_only(
    pay_api: PayApi, world: PayWorld, other: PayWorld
) -> None:
    api = pay_api
    vendor = await _vendor(api, other)  # tenant content in another organisation
    _system(api, None)
    assert (await api.call("GET", "/v1/ops/dashboard", world, "admin")).status_code == 404
    _system(api, world.org_id)
    try:
        board = await api.ok("GET", "/v1/ops/dashboard", world, "admin")
        # An admin of any other organisation: the route does not exist for them.
        assert (await api.call("GET", "/v1/ops/dashboard", other, "admin")).status_code == 404
        assert (await api.call("GET", "/v1/ops/jobs", other, "admin")).status_code == 404
        # In the system organisation, only admins, and only people: not an API key, even
        # one an admin gave this scope.
        assert (
            await api.call("GET", "/v1/ops/dashboard", world, "admin", otp=False)
        ).status_code == 200
        assert (await api.call("GET", "/v1/ops/dashboard", world, "auditor")).status_code == 403
        created = await api.ok(
            "POST",
            "/v1/api-keys",
            world,
            "admin",
            status=201,
            json={"name": "ops", "scopes": ["core.platform.read"]},
        )
        keyed = await api.client.get(
            "/v1/ops/dashboard", headers={"Authorization": f"Bearer {created['key']}"}
        )
        assert keyed.status_code == 404
    finally:
        _system(api, None)
    assert board["board"] == "platform_ops"
    health = {i["id"]: i["status"] for i in _section(board, "service_health")["items"]}
    assert health["api"] == health["database"] == health["object_store"] == "ok"
    # The test queue has no Redis behind it: said, not guessed.
    assert health["queue"] == health["worker"] == "not_configured"
    requests = _section(board, "requests")
    assert requests["source"] == "in_memory_request_counters"
    assert requests["count"] >= 1
    usage = {i["id"]: i for i in _section(board, "tenant_usage")["items"]}
    assert str(other.org_id) in usage or len(usage) == 50
    if str(other.org_id) in usage:
        assert usage[str(other.org_id)]["label"] == other.slug
        assert usage[str(other.org_id)]["detail"]["members"] == len(other.people)
    schedules = {i["id"] for i in _section(board, "schedules")["items"]}
    assert {"batch_url_sweep", "nightly_audit_verify"} <= schedules
    assert {"webhooks", "storage", "jobs", "reconciliation"} <= _keys(board)
    # Numbers only: no organisation name, person, vendor or address.
    raw = json.dumps(board)
    for content in (other.name, world.name, vendor["legal_name"], vendor["trn"], "@example.test"):
        assert content not in raw, content


async def test_ops_jobs_lists_job_runs(pay_api: PayApi, world: PayWorld) -> None:
    api = pay_api
    sessionmaker = create_sessionmaker(api.engine)
    name = f"ops_probe_{uuid.uuid4().hex[:8]}"
    run_id = await start_run(sessionmaker, name)
    await finish_run(sessionmaker, run_id, status="failed", error="TimeoutError")
    _system(api, world.org_id)
    try:
        body = await api.ok("GET", f"/v1/ops/jobs?job_name={name}", world, "admin")
        everything = await api.ok("GET", "/v1/ops/jobs?limit=200", world, "admin")
        bad = await api.call("GET", "/v1/ops/jobs?job_name=Bad-Name", world, "admin")
    finally:
        _system(api, None)
    assert [(r["id"], r["status"], r["error"]) for r in body["items"]] == [
        (str(run_id), "failed", "TimeoutError")
    ]
    assert str(run_id) in {r["id"] for r in everything["items"]}
    assert bad.status_code == 404


def test_every_plugin_section_uses_its_own_permission() -> None:
    registry = build_section_registry(discover_plugins().values())
    for board in Board:
        for workflow, spec in registry.for_board(board):
            assert spec.permission.startswith(f"{workflow}.")
    assert not registry.for_board(Board.PLATFORM_OPS)
