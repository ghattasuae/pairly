# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The Pay plugin without a database: discovery, permissions and role grants, the
second factor on approve and verify, the dual approval rules, the graph, the job, and
the onboarding link format."""

from __future__ import annotations

import uuid

import pytest

from pairly_core.approvals.rules import parse_rules
from pairly_core.plugins import discover_plugins, plugin_routers
from pairly_core.rbac import (
    AGENT_ALLOWED_ACTIONS,
    BuiltinRole,
    RoleGrant,
    build_registry,
    permission_action,
    requires_second_factor,
)
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_pay.onboarding import INVITE_PREFIX, SESSION_PREFIX, parse
from pairly_pay.permissions import (
    BANK_CHANGE_APPROVE,
    BANK_CHANGE_CREATE,
    BANK_CHANGE_READ,
    BANK_CHANGE_VERIFY,
    HOLD_MANAGE,
    PERMISSIONS,
    SETTINGS_MANAGE,
    VENDOR_READ,
    VENDOR_WRITE,
)
from pairly_pay.plugin import BANK_CHANGE_RULES_V1, SCHEDULED_JOBS, plugin


def test_discovered_through_the_entry_point() -> None:
    plugins = discover_plugins()
    assert plugins["pay"] is plugin
    assert [pid for pid, _ in plugin_routers([plugin])] == ["pay"]
    registry = build_registry(list(plugins.values()))
    runtime = WorkflowRuntime.from_plugins(
        [plugin],
        sessionmaker=None,
        checkpointer=None,
        registry=registry,  # type: ignore[arg-type]
    )
    spec = runtime.spec("pay", "pay_bank_change")
    assert spec.start_permission == BANK_CHANGE_CREATE
    assert set(spec.final_actions) == {"approve_bank_change"}
    assert runtime.approval_type("pay.bank_change") is not None
    spec.build().compile()


def _holders(permission: str) -> set[str]:
    registry = build_registry([plugin])
    return {r.value for r in BuiltinRole if permission in registry.permissions_for_role(r)}


def test_role_grants() -> None:
    assert all(p.name.startswith("pay.") for p in PERMISSIONS)
    assert _holders(VENDOR_READ) == {
        "admin",
        "ap_clerk",
        "approver",
        "finance_controller",
        "auditor",
        "agent",
    }
    assert _holders(VENDOR_WRITE) == {"admin", "ap_clerk", "finance_controller"}
    assert _holders(BANK_CHANGE_READ) == {
        "admin",
        "ap_clerk",
        "approver",
        "finance_controller",
        "auditor",
    }
    assert _holders(BANK_CHANGE_CREATE) == {"admin", "ap_clerk", "finance_controller"}
    # Everyone who verifies may start the approval run (the run's start permission).
    assert _holders(BANK_CHANGE_VERIFY) <= _holders(BANK_CHANGE_CREATE)
    assert _holders(BANK_CHANGE_VERIFY) == {"ap_clerk", "finance_controller"}
    # Admins do not approve by default; auditors only read.
    assert _holders(BANK_CHANGE_APPROVE) == {"approver", "finance_controller"}
    assert _holders(HOLD_MANAGE) == {"finance_controller"}
    assert _holders(SETTINGS_MANAGE) == {"admin", "finance_controller"}
    registry = build_registry([plugin])
    agent = {p for p in registry.permissions_for_role(BuiltinRole.AGENT) if p.startswith("pay.")}
    assert agent
    assert {permission_action(p) for p in agent} <= AGENT_ALLOWED_ACTIONS
    auditor = registry.permissions_for_role(BuiltinRole.AUDITOR)
    assert {permission_action(p) for p in auditor if p.startswith("pay.")} == {"read"}
    # A membership limited to Pay grants Pay permissions only.
    scoped = registry.resolve([RoleGrant("ap_clerk", "pay")])
    assert BANK_CHANGE_VERIFY in scoped
    assert all(p.startswith("pay.") for p in scoped)


@pytest.mark.parametrize(
    "permission", [BANK_CHANGE_APPROVE, BANK_CHANGE_VERIFY, HOLD_MANAGE, SETTINGS_MANAGE]
)
def test_second_factor_permissions(permission: str) -> None:
    assert requires_second_factor(permission)


@pytest.mark.parametrize("permission", [VENDOR_READ, VENDOR_WRITE, BANK_CHANGE_CREATE])
def test_everyday_permissions_need_no_second_factor(permission: str) -> None:
    assert not requires_second_factor(permission)


def test_dual_approval_rules() -> None:
    rules = parse_rules(BANK_CHANGE_RULES_V1)
    assert rules.approval_type == "pay.bank_change"
    chain = rules.select_chain({})
    assert [s.name for s in chain.steps] == ["first_approval", "second_approval"]
    assert all(s.mode == "any_of" for s in chain.steps)
    assert len(rules.chains) == 1


def test_scheduled_jobs_declared() -> None:
    activation, intake, drop = SCHEDULED_JOBS
    assert activation.name == "pay_bank_change_activation"
    assert activation.every_minutes == 5
    assert activation.run is not None
    assert intake.name == "pay_invoice_intake_start"
    assert intake.every_minutes == 1
    assert intake.run is not None
    assert drop.name == "pay_inbound_drop"
    assert drop.every_minutes == 1
    assert drop.run is not None


def test_onboarding_link_format() -> None:
    org, row = uuid.uuid4(), uuid.uuid4()
    secret = "a" * 43
    claim = parse(INVITE_PREFIX, f"{INVITE_PREFIX}.{org.hex}.{row.hex}.{secret}")
    assert claim is not None
    assert claim.org_id == org
    assert claim.row_id == row
    # A session token is not an invite link, and anything garbled reads as nothing.
    assert parse(INVITE_PREFIX, f"{SESSION_PREFIX}.{org.hex}.{row.hex}.{secret}") is None
    for bad in ("", "vi1", f"vi1.{org.hex}.{row.hex}", f"vi1.{org.hex}.{row.hex}.short", "x" * 300):
        assert parse(INVITE_PREFIX, bad) is None
    assert parse(INVITE_PREFIX, f"vi1.{org.hex}.{row.hex}.{'!' * 43}") is None
