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
    agent_actor,
    authorize,
    build_registry,
)
from pairly_hire.plugin import plugin as hire_plugin
from pairly_pay.plugin import plugin as pay_plugin

ORG = uuid.uuid4()


@dataclass(frozen=True)
class _Plugin:
    id: str
    name: str
    permissions: tuple[PermissionSpec, ...]


@dataclass(frozen=True)
class _Resource:
    org_id: uuid.UUID


@pytest.fixture
def registry() -> RoleRegistry:
    return build_registry([hire_plugin, pay_plugin])


def _actor(registry: RoleRegistry, *grants: RoleGrant) -> Actor:
    return Actor(
        kind=ActorKind.USER,
        id=uuid.uuid4(),
        org_id=ORG,
        grants=grants,
        permissions=registry.resolve(grants),
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
