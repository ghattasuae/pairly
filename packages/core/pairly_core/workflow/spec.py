# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""What a workflow plugin declares to the runtime.

A plugin lists ``GraphSpec`` objects in ``WorkflowPlugin.graphs``. Each spec names a
LangGraph graph (``build`` returns an uncompiled ``StateGraph``; the runtime compiles it
with the tenant-bound checkpointer), the permissions to start and read its runs, the
approval types its nodes may request, and the final actions its nodes may run.

Final actions are the only code allowed to make a change that needs a human decision
(pay, reject, send). They are never called directly: a node asks the executor
(``pairly_core.approvals.executor``) to run one by name, and the executor re-checks the
approval in the database first.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pairly_core.rbac import (
    RBACError,
    permission_action,
    permission_namespace,
    validate_permission_name,
)

if TYPE_CHECKING:
    from langgraph.graph import StateGraph
    from sqlalchemy.ext.asyncio import AsyncSession

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_APPROVAL_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}$")
# Actions a run starter may hold. Agents hold ``propose`` only, so they can start runs
# (proposals) and nothing else. ``submit`` is a person handing an item in for processing
# (an expense note, a request); like ``create`` it is never an agent or API key action.
START_ACTIONS = frozenset({"propose", "create", "submit"})


class SpecError(ValueError):
    """A graph spec is invalid (a programming error in a plugin)."""


@dataclass(frozen=True, slots=True)
class ApprovalType:
    """A kind of approval a workflow requests, for example ``pay.invoice``.

    ``permission`` is the workflow permission a decider must hold (``pay.invoice.approve``).
    Its action must be ``approve``, so the TOTP step-up rule always applies to deciding.
    ``default_rules`` is the YAML published as version 1 of the rules the first time an
    organisation uses the type; admins publish later versions.
    """

    key: str
    permission: str
    default_rules: str
    title: str = ""

    def __post_init__(self) -> None:
        if not _APPROVAL_TYPE_RE.fullmatch(self.key):
            raise SpecError(f"invalid approval type {self.key!r}: expected workflow.name")
        try:
            validate_permission_name(self.permission)
        except RBACError as exc:
            raise SpecError(str(exc)) from None
        if permission_action(self.permission) != "approve":
            raise SpecError(f"{self.key}: the decision permission must be an approve permission")
        if permission_namespace(self.permission) != self.workflow:
            raise SpecError(f"{self.key}: the permission must belong to its workflow")

    @property
    def workflow(self) -> str:
        return self.key.split(".", 1)[0]


@dataclass(frozen=True, slots=True)
class FinalActionCall:
    """What the executor hands a final action after every check passed."""

    org_id: uuid.UUID
    run_id: uuid.UUID
    approval_id: uuid.UUID
    step_key: str
    payload: Mapping[str, Any]
    approver_ids: tuple[uuid.UUID, ...]


# A final action runs inside the executor's transaction (the same one that marks the
# approval executed), so its own writes commit or roll back with that mark.
FinalAction = Callable[["AsyncSession", FinalActionCall], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class GraphSpec:
    workflow: str
    name: str
    version: int
    build: Callable[[], StateGraph[Any, Any, Any, Any]]
    start_permission: str
    read_permission: str
    approval_types: Sequence[ApprovalType] = ()
    final_actions: Mapping[str, FinalAction] = field(default_factory=dict)
    description: str = ""

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.name):
            raise SpecError(f"invalid graph name {self.name!r}")
        if not isinstance(self.version, int) or self.version < 1:
            raise SpecError(f"{self.name}: version must be a positive integer")
        for perm in (self.start_permission, self.read_permission):
            try:
                validate_permission_name(perm)
            except RBACError as exc:
                raise SpecError(str(exc)) from None
            if permission_namespace(perm) != self.workflow:
                raise SpecError(f"{self.name}: {perm!r} is outside the {self.workflow!r} workflow")
        if permission_action(self.start_permission) not in START_ACTIONS:
            raise SpecError(
                f"{self.name}: start_permission must be a propose, create or submit action"
            )
        if permission_action(self.read_permission) != "read":
            raise SpecError(f"{self.name}: read_permission must be a read action")
        for approval_type in self.approval_types:
            if approval_type.workflow != self.workflow:
                raise SpecError(f"{self.name}: approval type {approval_type.key!r} is foreign")
        for action in self.final_actions:
            if not _NAME_RE.fullmatch(action):
                raise SpecError(f"{self.name}: invalid final action name {action!r}")

    @property
    def key(self) -> tuple[str, str]:
        return (self.workflow, self.name)

    def approval_type(self, key: str) -> ApprovalType | None:
        return next((a for a in self.approval_types if a.key == key), None)
