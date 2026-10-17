# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Extension points around approvals.

* Decision records (WP1.10): ``approval_decided`` and ``final_action_executed`` carry
  everything a Decision Record needs from this module (who decided what, on which step,
  under which rules version, for which payload hash, and what was executed).
* Notifications (WP1.9): ``step_activated`` and ``step_escalated`` say who must act and
  by when (route to the delegate for people who are out of office).

Hooks run inside the transaction that made the change, so a decision record commits or
rolls back together with the decision. A hook that raises aborts the change: fail closed.
Hooks must not commit or roll back the session.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ApprovalDecided:
    org_id: uuid.UUID
    approval_id: uuid.UUID
    run_id: uuid.UUID
    workflow: str
    approval_type: str
    policy_version_id: uuid.UUID
    payload_hash: str
    step_id: uuid.UUID
    step_name: str
    decision_id: uuid.UUID
    decision: str
    reason: str | None
    actor_user_id: uuid.UUID
    on_behalf_of: uuid.UUID | None
    channel: str
    approval_status: str
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class FinalActionExecuted:
    org_id: uuid.UUID
    approval_id: uuid.UUID
    run_id: uuid.UUID
    workflow: str
    action: str
    payload_hash: str
    policy_version_id: uuid.UUID
    approver_ids: tuple[uuid.UUID, ...]
    result: dict[str, Any]
    executed_at: datetime


@dataclass(frozen=True, slots=True)
class StepActivated:
    org_id: uuid.UUID
    approval_id: uuid.UUID
    step_id: uuid.UUID
    step_name: str
    assignee_ids: tuple[uuid.UUID, ...]
    due_at: datetime


@dataclass(frozen=True, slots=True)
class StepEscalated:
    org_id: uuid.UUID
    approval_id: uuid.UUID
    step_id: uuid.UUID
    step_name: str
    escalation_ids: tuple[uuid.UUID, ...]
    due_at: datetime


class ApprovalHook(Protocol):
    async def approval_decided(self, session: AsyncSession, event: ApprovalDecided) -> None: ...

    async def final_action_executed(
        self, session: AsyncSession, event: FinalActionExecuted
    ) -> None: ...

    async def step_activated(self, session: AsyncSession, event: StepActivated) -> None: ...

    async def step_escalated(self, session: AsyncSession, event: StepEscalated) -> None: ...


class BaseApprovalHook:
    """No-op implementation to subclass: override only what you need."""

    async def approval_decided(self, session: AsyncSession, event: ApprovalDecided) -> None:
        return None

    async def final_action_executed(
        self, session: AsyncSession, event: FinalActionExecuted
    ) -> None:
        return None

    async def step_activated(self, session: AsyncSession, event: StepActivated) -> None:
        return None

    async def step_escalated(self, session: AsyncSession, event: StepEscalated) -> None:
        return None


class ApprovalHooks:
    """Calls every registered hook, in order."""

    def __init__(self, hooks: Iterable[ApprovalHook] = ()) -> None:
        self._hooks: tuple[ApprovalHook, ...] = tuple(hooks)

    async def approval_decided(self, session: AsyncSession, event: ApprovalDecided) -> None:
        for hook in self._hooks:
            await hook.approval_decided(session, event)

    async def final_action_executed(
        self, session: AsyncSession, event: FinalActionExecuted
    ) -> None:
        for hook in self._hooks:
            await hook.final_action_executed(session, event)

    async def step_activated(self, session: AsyncSession, event: StepActivated) -> None:
        for hook in self._hooks:
            await hook.step_activated(session, event)

    async def step_escalated(self, session: AsyncSession, event: StepEscalated) -> None:
        for hook in self._hooks:
            await hook.step_escalated(session, event)


NO_HOOKS = ApprovalHooks()
