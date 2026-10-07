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
* Step-up (second factor) rule, brief 6.1: a person may use a permission whose action is
  ``approve``, ``decide``, ``manage`` or ``export`` only when the sign-in that minted the
  token included a TOTP code (``amr`` contains ``otp``). The rule is keyed on the action,
  not on role names, so custom roles and future workflows inherit it, and the people it
  hits are exactly those who hold such permissions (admins, approvers, finance
  controllers, and anyone who decides candidates). A token without ``amr`` fails closed.
* Granting rule (invitations): see ``check_grantable``.

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
# Actions that need a second factor (TOTP) in the current sign-in. See the module docstring.
STEP_UP_ACTIONS = frozenset({"approve", "decide", "manage", "export"})
# The ``amr`` value (RFC 8176 "one-time password") the web app puts in the token after a
# successful TOTP verification.
SECOND_FACTOR_AMR = "otp"

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


class SecondFactorRequiredError(PermissionDeniedError):
    """The actor holds the permission but did not sign in with a second factor."""

    code = "second_factor_required"
    title = "Two-factor sign-in required"


def validate_permission_name(name: str) -> str:
    if not isinstance(name, str) or not PERMISSION_RE.fullmatch(name):
        raise RBACError(f"invalid permission name {name!r}: expected namespace.resource.action")
    return name


def permission_action(name: str) -> str:
    return name.rsplit(".", 1)[1]


def permission_namespace(name: str) -> str:
    return name.split(".", 1)[0]


def requires_second_factor(permission: str) -> bool:
    """Whether using ``permission`` needs a TOTP sign-in (``amr`` contains ``otp``)."""
    return permission_action(permission) in STEP_UP_ACTIONS


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

    @property
    def workflows(self) -> frozenset[str]:
        """Namespaces other than core that define at least one permission."""
        return frozenset(
            ns for ns in (p.namespace for p in self._permissions.values()) if ns != CORE_NAMESPACE
        )

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


# Core modules that define more ``core.*`` permissions in a module-level ``PERMISSIONS``
# tuple. Imported lazily by ``build_registry`` (they import this module).
CORE_PERMISSION_MODULES: tuple[str, ...] = (
    "pairly_core.documents.permissions",
    "pairly_core.extraction.permissions",
    "pairly_core.policy.permissions",
    "pairly_core.notifications.permissions",
    "pairly_core.decisions.permissions",
)


def core_permissions() -> tuple[PermissionSpec, ...]:
    """``CORE_PERMISSIONS`` plus those of every module in ``CORE_PERMISSION_MODULES``."""
    import importlib

    extra: list[PermissionSpec] = []
    for name in CORE_PERMISSION_MODULES:
        extra.extend(importlib.import_module(name).PERMISSIONS)
    return (*CORE_PERMISSIONS, *extra)


def build_registry(plugins: Iterable[WorkflowPlugin] = ()) -> RoleRegistry:
    """Core permissions plus every plugin's permissions, frozen."""
    registry = RoleRegistry()
    for spec in core_permissions():
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
    # Authentication methods of the sign-in behind the token (JWT ``amr``). Empty when the
    # token had none, which fails the step-up rule closed.
    amr: frozenset[str] = frozenset()

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    @property
    def has_second_factor(self) -> bool:
        return SECOND_FACTOR_AMR in self.amr


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
    elif (
        actor.kind is ActorKind.USER
        and requires_second_factor(permission)
        and not actor.has_second_factor
    ):
        reason = "second_factor_required"
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
        if reason == "second_factor_required":
            raise SecondFactorRequiredError(
                f"{permission} needs a sign-in with two-factor authentication", denial=denial
            )
        raise PermissionDeniedError(f"missing permission {permission}", denial=denial)


def check_grantable(
    registry: RoleRegistry,
    granter: Actor,
    role: str,
    workflow: str | None,
) -> frozenset[str]:
    """Raise ``PermissionDeniedError`` unless ``granter`` may give ``role`` (in ``workflow``).

    Returns the permissions the grant would confer. The rule, in order:

    1. The granter must hold ``core.users.manage`` (the route checks it too).
    2. The ``agent`` service role is never granted to a person, and a role must be one the
       registry knows (an unknown role would grant nothing, but inviting someone to it is
       a mistake worth refusing).
    3. A workflow scope must name a registered workflow.
    4. No escalation of core (administrative) power: every ``core.*`` permission the grant
       confers must already be held by the granter. Workflow permissions (for example
       ``pay.invoice.approve``) are exempt on purpose: assigning workflow duties is what
       user managers do, and segregation of duties wants admins to assign approvers
       without approving themselves (admin does not hold approve permissions).
    """
    reason: str | None = None
    conferred: frozenset[str] = frozenset()
    if not granter.has("core.users.manage"):
        reason = "missing_permission"
    elif role == AGENT_ROLE or role not in registry.roles:
        reason = "role_not_grantable"
    elif workflow is not None and workflow not in registry.workflows:
        reason = "unknown_workflow"
    else:
        conferred = registry.resolve((RoleGrant(role, workflow),))
        core_needed = {p for p in conferred if permission_namespace(p) == CORE_NAMESPACE}
        if not core_needed <= granter.permissions:
            reason = "privilege_escalation"
    if reason is not None:
        denial = Denial(actor=granter, permission="core.users.manage", reason=reason)
        raise PermissionDeniedError(f"you cannot grant the role {role!r}", denial=denial)
    return conferred


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
