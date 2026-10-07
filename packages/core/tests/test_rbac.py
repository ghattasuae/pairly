# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""RBAC registry rules, authorize() and the agent role restrictions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from pairly_core.rbac import (
    AGENT_ROLE,
    Actor,
    ActorKind,
    AgentPermissionError,
    PermissionDeniedError,
    PermissionSpec,
    RBACError,
    RoleGrant,
    RoleRegistry,
    SecondFactorRequiredError,
    agent_actor,
    authorize,
    build_registry,
    check_grantable,
    requires_second_factor,
)
from pairly_core.workflow.spec import GraphSpec
from pairly_hire.plugin import plugin as hire_plugin
from pairly_pay.plugin import plugin as pay_plugin

ORG = uuid.uuid4()


@dataclass(frozen=True)
class _Plugin:
    id: str
    name: str
    permissions: tuple[PermissionSpec, ...]
    graphs: tuple[GraphSpec, ...] = ()


@dataclass(frozen=True)
class _Resource:
    org_id: uuid.UUID


OTP = frozenset({"otp"})


@pytest.fixture
def registry() -> RoleRegistry:
    return build_registry([hire_plugin, pay_plugin])


def _actor(registry: RoleRegistry, *grants: RoleGrant, amr: frozenset[str] = OTP) -> Actor:
    """A user actor. Signed in with TOTP by default, so step-up never gets in the way."""
    return Actor(
        kind=ActorKind.USER,
        id=uuid.uuid4(),
        org_id=ORG,
        grants=grants,
        permissions=registry.resolve(grants),
        amr=amr,
    )


@pytest.mark.parametrize("perm", ["pay.invoice.approve", "hire.candidate.decide"])
def test_agent_role_cannot_get_execute_permissions_via_grant(perm: str) -> None:
    registry = RoleRegistry()
    registry.register(PermissionSpec(perm, "x"), namespace=perm.split(".")[0])
    with pytest.raises(AgentPermissionError):
        registry.grant(AGENT_ROLE, perm)


@pytest.mark.parametrize("action", ["approve", "execute", "manage", "delete", "decide"])
def test_plugin_cannot_give_agent_execute_permissions(action: str) -> None:
    bad = _Plugin(
        id="pay",
        name="Pay",
        permissions=(PermissionSpec(f"pay.invoice.{action}", "x", frozenset({AGENT_ROLE})),),
    )
    with pytest.raises(AgentPermissionError):
        build_registry([bad])


def test_agent_only_reads_and_proposes(registry: RoleRegistry) -> None:
    perms = registry.permissions_for_role(AGENT_ROLE)
    assert perms
    assert {p.rsplit(".", 1)[1] for p in perms} <= {"read", "propose"}


def test_authorize_denies_agent_execute_even_if_granted() -> None:
    forged = Actor(
        kind=ActorKind.AGENT,
        id=uuid.uuid4(),
        org_id=ORG,
        grants=(RoleGrant(AGENT_ROLE),),
        permissions=frozenset({"pay.invoice.approve"}),
    )
    with pytest.raises(PermissionDeniedError):
        authorize(forged, "pay.invoice.approve")


def test_agent_actor(registry: RoleRegistry) -> None:
    agent = agent_actor(registry, ORG, uuid.uuid4())
    authorize(agent, "pay.invoice.read")
    authorize(agent, "hire.candidate.propose")
    with pytest.raises(PermissionDeniedError):
        authorize(agent, "pay.invoice.approve")


def test_authorize_missing_permission(registry: RoleRegistry) -> None:
    clerk = _actor(registry, RoleGrant("ap_clerk"))
    authorize(clerk, "pay.invoice.read")
    with pytest.raises(PermissionDeniedError):
        authorize(clerk, "pay.invoice.approve")
    with pytest.raises(PermissionDeniedError):
        authorize(clerk, "core.users.manage")


def test_authorize_checks_resource_org(registry: RoleRegistry) -> None:
    approver = _actor(registry, RoleGrant("approver"))
    authorize(approver, "pay.invoice.approve", _Resource(ORG))
    with pytest.raises(PermissionDeniedError):
        authorize(approver, "pay.invoice.approve", _Resource(uuid.uuid4()))
    with pytest.raises(PermissionDeniedError):
        authorize(approver, "pay.invoice.approve", object())


def test_workflow_scoped_membership(registry: RoleRegistry) -> None:
    pay_only = _actor(registry, RoleGrant("approver", workflow="pay"))
    assert pay_only.has("pay.invoice.approve")
    assert not pay_only.has("core.approval.decide")
    everywhere = _actor(registry, RoleGrant("approver"))
    assert everywhere.has("core.approval.decide")


def test_unknown_role_grants_nothing(registry: RoleRegistry) -> None:
    assert registry.resolve([RoleGrant("superuser")]) == frozenset()


def test_registry_rules() -> None:
    registry = RoleRegistry()
    spec = PermissionSpec("core.thing.read", "x", frozenset({"auditor"}))
    with pytest.raises(RBACError, match="outside"):
        registry.register(spec, namespace="hire")
    registry.register(spec, namespace="core")
    with pytest.raises(RBACError, match="duplicate"):
        registry.register(spec, namespace="core")
    with pytest.raises(RBACError, match="unknown role"):
        registry.grant("nobody", "core.thing.read")
    with pytest.raises(RBACError, match="unknown permission"):
        registry.grant("auditor", "core.other.read")
    registry.freeze()
    with pytest.raises(RBACError, match="frozen"):
        registry.grant("admin", "core.thing.read")


def test_plugin_cannot_claim_core_namespace() -> None:
    with pytest.raises(RBACError):
        build_registry([_Plugin(id="core", name="Core", permissions=())])


@pytest.mark.parametrize("name", ["core.audit", "Core.audit.read", "core..read", "a.b.c.d", ""])
def test_invalid_permission_names(name: str) -> None:
    with pytest.raises(RBACError):
        PermissionSpec(name, "x")


def test_plugins_contribute_permissions(registry: RoleRegistry) -> None:
    assert "hire.candidate.read" in registry
    assert "pay.invoice.approve" in registry
    assert "core.audit.read" in registry
    assert "pay.invoice.approve" not in registry.permissions_for_role("admin")


# ------------------------------------------------------------------ step-up (TOTP)

STEP_UP = [
    ("admin", "core.users.manage"),
    ("admin", "core.org.manage"),
    ("admin", "core.audit.export"),
    ("admin", "core.approval.decide"),
    ("approver", "pay.invoice.approve"),
    ("finance_controller", "pay.invoice.approve"),
    ("recruiter", "hire.candidate.decide"),
    ("auditor", "core.audit.export"),
]


@pytest.mark.parametrize(("role", "perm"), STEP_UP)
@pytest.mark.parametrize("amr", [frozenset(), frozenset({"email"}), frozenset({"sso"})])
def test_step_up_permissions_need_otp(
    registry: RoleRegistry, role: str, perm: str, amr: frozenset[str]
) -> None:
    """A magic link or SSO sign-in without TOTP cannot approve, decide, manage or export."""
    actor = _actor(registry, RoleGrant(role), amr=amr)
    assert actor.has(perm)
    with pytest.raises(SecondFactorRequiredError) as caught:
        authorize(actor, perm)
    assert caught.value.code == "second_factor_required"
    assert caught.value.denial is not None
    assert caught.value.denial.reason == "second_factor_required"


@pytest.mark.parametrize(("role", "perm"), STEP_UP)
def test_step_up_permissions_allowed_with_otp(registry: RoleRegistry, role: str, perm: str) -> None:
    authorize(_actor(registry, RoleGrant(role), amr=frozenset({"email", "otp"})), perm)


@pytest.mark.parametrize(
    ("role", "perm"),
    [
        ("approver", "pay.invoice.read"),
        ("admin", "core.users.read"),
        ("auditor", "core.audit.read"),
    ],
)
def test_reads_need_no_second_factor(registry: RoleRegistry, role: str, perm: str) -> None:
    authorize(_actor(registry, RoleGrant(role), amr=frozenset()), perm)


def test_missing_permission_wins_over_second_factor(registry: RoleRegistry) -> None:
    clerk = _actor(registry, RoleGrant("ap_clerk"), amr=frozenset())
    with pytest.raises(PermissionDeniedError) as caught:
        authorize(clerk, "pay.invoice.approve")
    assert not isinstance(caught.value, SecondFactorRequiredError)


def test_requires_second_factor() -> None:
    assert requires_second_factor("pay.invoice.approve")
    assert requires_second_factor("core.users.manage")
    assert not requires_second_factor("pay.invoice.read")
    assert not requires_second_factor("core.decision.propose")


# ------------------------------------------------------------------ granting roles


def _manager(registry: RoleRegistry, *extra: str) -> Actor:
    """A user manager holding core.users.manage plus ``extra`` (not a built-in role)."""
    return Actor(
        kind=ActorKind.USER,
        id=uuid.uuid4(),
        org_id=ORG,
        grants=(),
        permissions=frozenset({"core.users.manage", *extra}),
        amr=OTP,
    )


@pytest.mark.parametrize(
    "role", ["admin", "approver", "recruiter", "auditor", "read_only", "finance_controller"]
)
def test_admin_can_grant_every_human_role(registry: RoleRegistry, role: str) -> None:
    admin = _actor(registry, RoleGrant("admin"))
    check_grantable(registry, admin, role, None)
    check_grantable(registry, admin, role, "pay")


def test_admin_can_grant_approver_without_holding_approve(registry: RoleRegistry) -> None:
    admin = _actor(registry, RoleGrant("admin"))
    assert not admin.has("pay.invoice.approve")
    conferred = check_grantable(registry, admin, "approver", None)
    assert "pay.invoice.approve" in conferred


@pytest.mark.parametrize("role", ["agent", "superuser", "AGENT"])
def test_agent_and_unknown_roles_are_never_grantable(registry: RoleRegistry, role: str) -> None:
    admin = _actor(registry, RoleGrant("admin"))
    with pytest.raises(PermissionDeniedError) as caught:
        check_grantable(registry, admin, role, None)
    assert caught.value.denial is not None
    assert caught.value.denial.reason == "role_not_grantable"


def test_unknown_workflow_is_refused(registry: RoleRegistry) -> None:
    admin = _actor(registry, RoleGrant("admin"))
    with pytest.raises(PermissionDeniedError) as caught:
        check_grantable(registry, admin, "approver", "payroll")
    assert caught.value.denial is not None
    assert caught.value.denial.reason == "unknown_workflow"


@pytest.mark.parametrize("role", ["admin", "auditor", "approver"])
def test_no_escalation_of_core_permissions(registry: RoleRegistry, role: str) -> None:
    """A manager without admin's core powers cannot hand them out."""
    manager = _manager(registry, "core.org.read")
    with pytest.raises(PermissionDeniedError) as caught:
        check_grantable(registry, manager, role, None)
    assert caught.value.denial is not None
    assert caught.value.denial.reason == "privilege_escalation"


def test_workflow_scope_limits_what_must_be_held(registry: RoleRegistry) -> None:
    # approver scoped to pay confers only pay.* permissions: nothing core to escalate.
    manager = _manager(registry)
    assert check_grantable(registry, manager, "approver", "pay") == {
        "pay.invoice.read",
        "pay.invoice.approve",
    }


def test_granting_needs_users_manage(registry: RoleRegistry) -> None:
    auditor = _actor(registry, RoleGrant("auditor"))
    with pytest.raises(PermissionDeniedError):
        check_grantable(registry, auditor, "read_only", None)
