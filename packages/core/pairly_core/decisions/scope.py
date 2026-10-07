# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Which workflows' decision records, replays and appeals an actor may see or decide.

Mirrors the approval inbox (``pairly_core.approvals.service.list_inbox``): a membership
limited to one workflow grants only that workflow's permissions, so access follows the
workflow permissions the actor holds, never one core permission for everything.

* Read (records, receipts, replays, appeals): every workflow where the actor holds a
  ``<workflow>.<resource>.read`` permission. A core-wide grant (``core.decision.read``
  for records, ``core.appeal.read`` for appeals; admin and auditor by default) covers
  every workflow, ``core`` included.
* Replay: ``core.decision.replay`` and read access to the record's workflow.
* Deciding an appeal: a decide-type permission (action ``decide`` or ``approve``) of the
  appealed decision's workflow, or the core-wide ``core.appeal.decide`` (admin by
  default). Both are step-up actions: ``authorize`` checks the second factor.

A record or appeal outside the actor's scope is reported as not found (404), like
another organisation's; an actor with no scope at all gets an audited 403.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import ColumnElement, true

from pairly_core.decisions.permissions import APPEAL_DECIDE
from pairly_core.rbac import (
    CORE_NAMESPACE,
    Actor,
    Denial,
    PermissionDeniedError,
    authorize,
    permission_action,
    permission_namespace,
)

DECIDE_ACTIONS: Final = frozenset({"decide", "approve"})


@dataclass(frozen=True, slots=True)
class WorkflowScope:
    """``everything`` (a core-wide grant) or the listed workflows."""

    everything: bool
    workflows: frozenset[str]

    @property
    def empty(self) -> bool:
        return not self.everything and not self.workflows

    def allows(self, workflow: str) -> bool:
        return self.everything or workflow in self.workflows

    def clause(self, column: Any) -> ColumnElement[bool]:
        """A SQL filter on a ``workflow`` column."""
        if self.everything:
            return true()
        return column.in_(sorted(self.workflows))  # type: ignore[no-any-return]

    def union(self, other: WorkflowScope) -> WorkflowScope:
        return WorkflowScope(self.everything or other.everything, self.workflows | other.workflows)


def _workflow_actions(actor: Actor, actions: frozenset[str]) -> frozenset[str]:
    return frozenset(
        permission_namespace(p)
        for p in actor.permissions
        if permission_namespace(p) != CORE_NAMESPACE and permission_action(p) in actions
    )


def read_scope(actor: Actor, core_permission: str) -> WorkflowScope:
    """Workflows whose records ``actor`` may read; ``core_permission`` covers them all."""
    return WorkflowScope(
        everything=actor.has(core_permission),
        workflows=_workflow_actions(actor, frozenset({"read"})),
    )


def decide_scope(actor: Actor) -> WorkflowScope:
    """Workflows whose appeals ``actor`` holds a permission to decide (step-up not checked)."""
    return WorkflowScope(
        everything=actor.has(APPEAL_DECIDE),
        workflows=_workflow_actions(actor, DECIDE_ACTIONS),
    )


def require_scope(actor: Actor, scope: WorkflowScope, permission: str) -> WorkflowScope:
    """``scope``, or an audited denial when it covers no workflow at all."""
    if scope.empty:
        raise PermissionDeniedError(
            denial=Denial(actor=actor, permission=permission, reason="no_workflow_access")
        )
    return scope


def decide_permission(actor: Actor, workflow: str) -> str | None:
    """The permission that lets ``actor`` decide an appeal in ``workflow``, if any."""
    if actor.has(APPEAL_DECIDE):
        return APPEAL_DECIDE
    own = sorted(
        p
        for p in actor.permissions
        if permission_namespace(p) == workflow and permission_action(p) in DECIDE_ACTIONS
    )
    return own[0] if own else None


def authorize_appeal_decision(actor: Actor, workflow: str) -> str:
    """Raise unless ``actor`` may decide an appeal in ``workflow`` (step-up included)."""
    permission = decide_permission(actor, workflow)
    if permission is None:
        raise PermissionDeniedError(
            "you cannot resolve this appeal",
            denial=Denial(actor=actor, permission=APPEAL_DECIDE, reason="no_workflow_access"),
        )
    authorize(actor, permission)
    return permission
