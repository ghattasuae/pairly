# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Role based access control.

Permissions are strings ``<namespace>.<resource>.<action>``: ``core.audit.read``,
``hire.candidate.read``, ``pay.invoice.approve``. The namespace is ``core`` or a
workflow plugin id. Core defines the core permissions and the built-in roles; each
workflow plugin contributes its own permissions (and their default role grants) through
``WorkflowPlugin.permissions``.

Rules enforced in code:

* The ``agent`` service role may only ever hold ``read`` and ``propose`` permissions.
  Granting it anything else raises ``AgentPermissionError`` when the registry is built,
  and ``authorize`` denies such a permission to an agent actor even if it got one.
* A membership limited to one workflow grants only that workflow's permissions. Core
  permissions come from memberships that cover every workflow.
* Every check fails closed: unknown roles grant nothing, unknown permissions are held
  by nobody.

Route handlers use ``require(permission)``; agent tools and services call
``authorize(actor, permission, resource)``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Protocol, runtime_checkable

from fastapi import Depends

from pairly_core.errors import ForbiddenError

if TYPE_CHECKING:
    from pairly_core.plugins import WorkflowPlugin

CORE_NAMESPACE = "core"
AGENT_ROLE = "agent"
AGENT_ALLOWED_ACTIONS = frozenset({"read", "propose"})

PERMISSION_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,63}\.[a-z][a-z0-9_]{0,31}$")


class RBACError(Exception):
    """Invalid permission, role or grant definition (a programming error)."""


class AgentPermissionError(RBACError):
    """Attempt to give the agent service role a permission other than read or propose."""


@dataclass(frozen=True, slots=True)
class Denial:
    """What ``authorize`` refused, for the audit log. Never shown to the client.

    ``resource_id`` is only kept when the resource belongs to the actor's organisation, so
    a denial never copies another tenant's identifiers into this tenant's audit log.
    """

    actor: Actor
    permission: str
    reason: str
    resource_type: str | None = None
    resource_id: str | None = None


class PermissionDeniedError(ForbiddenError):
    code = "permission_denied"
    title = "Permission denied"

    def __init__(self, detail: str | None = None, *, denial: Denial | None = None) -> None:
        super().__init__(detail)
        self.denial = denial


def validate_permission_name(name: str) -> str:
    if not isinstance(name, str) or not PERMISSION_RE.fullmatch(name):
        raise RBACError(f"invalid permission name {name!r}: expected namespace.resource.action")
    return name


def permission_action(name: str) -> str:
    return name.rsplit(".", 1)[1]


def permission_namespace(name: str) -> str:
    return name.split(".", 1)[0]


class BuiltinRole(StrEnum):
    ADMIN = "admin"
    RECRUITER = "recruiter"
    HIRING_MANAGER = "hiring_manager"
    INTERVIEWER = "interviewer"
    AP_CLERK = "ap_clerk"
    APPROVER = "approver"
    FINANCE_CONTROLLER = "finance_controller"
    AUDITOR = "auditor"
    READ_ONLY = "read_only"
    AGENT = AGENT_ROLE


# Display names. ``agent`` is a service role for AI agents, never a human membership.
BUILTIN_ROLE_NAMES: Mapping[str, str] = {
    BuiltinRole.ADMIN: "Admin",
    BuiltinRole.RECRUITER: "Recruiter",
    BuiltinRole.HIRING_MANAGER: "Hiring manager",
    BuiltinRole.INTERVIEWER: "Interviewer",
    BuiltinRole.AP_CLERK: "AP clerk",
    BuiltinRole.APPROVER: "Approver",
    BuiltinRole.FINANCE_CONTROLLER: "Finance controller",
    BuiltinRole.AUDITOR: "Auditor",
    BuiltinRole.READ_ONLY: "Read only",
    BuiltinRole.AGENT: "AI agent (service)",
}

HUMAN_ROLES = frozenset(r.value for r in BuiltinRole if r is not BuiltinRole.AGENT)


@dataclass(frozen=True, slots=True)
class PermissionSpec:
    """A permission and the built-in roles that hold it by default."""

    name: str
    description: str
    default_roles: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        validate_permission_name(self.name)
        if not isinstance(self.default_roles, frozenset):
            raise RBACError(f"{self.name}: default_roles must be a frozenset")

    @property
    def namespace(self) -> str:
        return permission_namespace(self.name)

    @property
    def action(self) -> str:
        return permission_action(self.name)


def _p(name: str, description: str, *roles: str) -> PermissionSpec:
    return PermissionSpec(name, description, frozenset(roles))


_R = BuiltinRole
_EVERY_HUMAN = tuple(HUMAN_ROLES)
_STAFF = (_R.RECRUITER, _R.HIRING_MANAGER, _R.INTERVIEWER, _R.AP_CLERK)
_DECIDERS = (_R.APPROVER, _R.FINANCE_CONTROLLER)

CORE_PERMISSIONS: tuple[PermissionSpec, ...] = (
    _p("core.org.read", "View organisation details", *_EVERY_HUMAN, _R.AGENT),
    _p("core.org.manage", "Change organisation settings", _R.ADMIN),
    _p("core.users.read", "View members and their roles", _R.ADMIN, _R.AUDITOR),
    _p("core.users.manage", "Invite members and change their roles", _R.ADMIN),
    _p("core.audit.read", "Read the audit log", _R.ADMIN, _R.AUDITOR, _R.FINANCE_CONTROLLER),
    _p("core.audit.export", "Export the audit log", _R.ADMIN, _R.AUDITOR),
    _p(
        "core.decision.read",
        "Read decision records",
        _R.ADMIN,
        _R.AUDITOR,
        _R.READ_ONLY,
        *_STAFF,
        *_DECIDERS,
        _R.AGENT,
    ),
    _p("core.decision.propose", "Create proposed decisions", _R.AGENT),
    _p("core.approval.read", "See the approval inbox", _R.ADMIN, _R.AUDITOR, *_STAFF, *_DECIDERS),
    _p("core.approval.decide", "Approve or reject approval requests", _R.ADMIN, *_DECIDERS),
)


@dataclass(frozen=True, slots=True)
class RoleGrant:
    """One membership: a role, optionally limited to one workflow."""

    role: str
    workflow: str | None = None


class RoleRegistry:
    """Maps roles to permissions. Built once at startup, then frozen."""

    def __init__(self, roles: Iterable[str] = BUILTIN_ROLE_NAMES) -> None:
        self._permissions: dict[str, PermissionSpec] = {}
        self._grants: dict[str, set[str]] = {str(role): set() for role in roles}
        self._frozen = False

    @property
    def roles(self) -> frozenset[str]:
        return frozenset(self._grants)

    @property
    def permissions(self) -> Mapping[str, PermissionSpec]:
        return dict(self._permissions)

    def __contains__(self, permission: object) -> bool:
        return permission in self._permissions

    def _check_open(self) -> None:
        if self._frozen:
            raise RBACError("the role registry is frozen")

    def register(self, spec: PermissionSpec, *, namespace: str) -> None:
        """Define a permission owned by ``namespace`` and apply its default grants."""
        self._check_open()
        if spec.namespace != namespace:
            raise RBACError(f"{spec.name!r} is outside the {namespace!r} namespace")
        if spec.name in self._permissions:
            raise RBACError(f"duplicate permission {spec.name!r}")
        for role in spec.default_roles:
            self._check_grant(role, spec.name)
        self._permissions[spec.name] = spec
        for role in spec.default_roles:
            self._grants[role].add(spec.name)

    def grant(self, role: str, permission: str) -> None:
        self._check_open()
        if permission not in self._permissions:
            raise RBACError(f"unknown permission {permission!r}")
        self._check_grant(role, permission)
        self._grants[role].add(permission)

    def _check_grant(self, role: str, permission: str) -> None:
        if role not in self._grants:
            raise RBACError(f"unknown role {role!r}")
        if role == AGENT_ROLE and permission_action(permission) not in AGENT_ALLOWED_ACTIONS:
            raise AgentPermissionError(
                f"the agent role may only hold read or propose permissions, not {permission!r}"
            )

    def freeze(self) -> RoleRegistry:
        self._frozen = True
        return self

    def permissions_for_role(self, role: str) -> frozenset[str]:
        return frozenset(self._grants.get(role, ()))

    def resolve(self, grants: Iterable[RoleGrant]) -> frozenset[str]:
        """Effective permissions of a set of memberships. Unknown roles grant nothing."""
        result: set[str] = set()
        for grant in grants:
            perms = self._grants.get(grant.role, set())
            if grant.workflow is None:
                result |= perms
            else:
                result |= {p for p in perms if permission_namespace(p) == grant.workflow}
        return frozenset(result)


def build_registry(plugins: Iterable[WorkflowPlugin] = ()) -> RoleRegistry:
    """Core permissions plus every plugin's permissions, frozen."""
    registry = RoleRegistry()
    for spec in CORE_PERMISSIONS:
        registry.register(spec, namespace=CORE_NAMESPACE)
    for plugin in plugins:
        if plugin.id == CORE_NAMESPACE:
            raise RBACError("a workflow plugin cannot use the core namespace")
        for spec in plugin.permissions:
            registry.register(spec, namespace=plugin.id)
    return registry.freeze()


# ------------------------------------------------------------------ actors


class ActorKind(StrEnum):
    USER = "user"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is acting, in which organisation, with which effective permissions."""

    kind: ActorKind
    id: uuid.UUID
    org_id: uuid.UUID
    grants: tuple[RoleGrant, ...]
    permissions: frozenset[str]
    subject: str | None = None
    email: str | None = None

    def has(self, permission: str) -> bool:
        return permission in self.permissions


def agent_actor(registry: RoleRegistry, org_id: uuid.UUID, agent_id: uuid.UUID) -> Actor:
    """The service actor AI agents run as: only the ``agent`` role, in one org."""
    grants = (RoleGrant(AGENT_ROLE),)
    return Actor(
        kind=ActorKind.AGENT,
        id=agent_id,
        org_id=org_id,
        grants=grants,
        permissions=registry.resolve(grants),
    )


@runtime_checkable
class OrgOwned(Protocol):
    """Any resource that belongs to an organisation."""

    @property
    def org_id(self) -> uuid.UUID | None: ...


def authorize(actor: Actor, permission: str, resource: object | None = None) -> None:
    """Raise ``PermissionDeniedError`` unless ``actor`` may perform ``permission`` on ``resource``.

    ``resource``, when given, must belong to the actor's organisation.
    """
    validate_permission_name(permission)
    reason: str | None = None
    if not actor.has(permission):
        reason = "missing_permission"
    elif (
        actor.kind is ActorKind.AGENT and permission_action(permission) not in AGENT_ALLOWED_ACTIONS
    ):
        reason = "agent_action_not_allowed"
    same_org = isinstance(resource, OrgOwned) and resource.org_id == actor.org_id
    if resource is not None and not same_org:
        reason = "resource_outside_org"
    if reason is not None:
        # The audit hook: the exception carries the denial; ``pairly_core.identity`` (HTTP)
        # or ``pairly_core.audit.record_denial`` (agents, jobs) writes it to the audit log
        # in its own transaction, after the denied work has been rolled back.
        resource_id = getattr(resource, "id", None) if same_org else None
        denial = Denial(
            actor=actor,
            permission=permission,
            reason=reason,
            resource_type=type(resource).__name__ if resource is not None else None,
            resource_id=str(resource_id) if resource_id is not None else None,
        )
        raise PermissionDeniedError(f"missing permission {permission}", denial=denial)


def require(permission: str) -> Callable[..., Awaitable[Actor]]:
    """FastAPI dependency: the authenticated actor, if it holds ``permission``.

    Usage: ``actor: Annotated[Actor, Depends(require("core.audit.read"))]`` or
    ``dependencies=[Depends(require("core.audit.read"))]``.
    """
    validate_permission_name(permission)
    # Imported here: identity depends on this module for Actor and the registry.
    from pairly_core.identity import current_actor

    async def dependency(actor: Actor) -> Actor:
        authorize(actor, permission)
        return actor

    # Set the real annotation object: ``current_actor`` is a local name, so FastAPI could
    # not resolve it from a postponed (string) annotation.
    dependency.__annotations__["actor"] = Annotated[Actor, Depends(current_actor)]
    dependency.__name__ = "require_" + permission.replace(".", "_")
    return dependency
