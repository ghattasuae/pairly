# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""SLA escalation for approval steps (run by the worker cron, one organisation at a time).

A step is overdue when its ``due_at`` has passed while it is still active. The first
time the job sees it overdue, it adds escalation approvers to the step: members holding
one of the rules' ``escalation.roles`` (in the approval's workflow) and the approval
type's permission, minus the submitter, the preparers, anyone who already decided
another step of the chain and the step's own approvers. An escalation approver's
approval completes the step on its own (a senior override); segregation of duties still
applies to them. The step is marked escalated even when nobody qualifies (the inbox
shows it overdue and the audit event says no target was found), so the job never loops.

One level of escalation in M1; repeated escalation is a later refinement.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.hooks import NO_HOOKS, ApprovalHooks, StepEscalated
from pairly_core.approvals.models import (
    Approval,
    ApprovalDecision,
    ApprovalStatus,
    ApprovalStep,
    StepStatus,
)
from pairly_core.approvals.service import (
    SYSTEM_ACTOR_ID,
    Actions,
    db_now,
    excluded_users,
    member_permissions,
    role_members,
    rules_of,
)
from pairly_core.audit import ActorType, append_audit
from pairly_core.rbac import RoleRegistry


@dataclass(frozen=True, slots=True)
class EscalationResult:
    approval_id: uuid.UUID
    step_id: uuid.UUID
    added: tuple[uuid.UUID, ...]


async def escalate_overdue(
    session: AsyncSession,
    registry: RoleRegistry,
    *,
    hooks: ApprovalHooks = NO_HOOKS,
    limit: int = 200,
) -> list[EscalationResult]:
    """Escalate the overdue, not yet escalated steps of the session's organisation."""
    now = await db_now(session)
    rows = (
        await session.execute(
            select(ApprovalStep, Approval)
            .join(Approval, Approval.id == ApprovalStep.approval_id)
            .where(
                ApprovalStep.status == StepStatus.ACTIVE.value,
                ApprovalStep.due_at < now,
                ApprovalStep.escalated_at.is_(None),
                Approval.status == ApprovalStatus.PENDING.value,
            )
            .order_by(ApprovalStep.due_at)
            .limit(limit)
            .with_for_update(of=ApprovalStep, skip_locked=True)
        )
    ).all()
    results: list[EscalationResult] = []
    for step, approval in rows:
        rules = await rules_of(session, approval)
        candidates: set[uuid.UUID] = set()
        if rules.escalation is not None:
            candidates = await role_members(session, rules.escalation.roles, approval.workflow)
        decided_elsewhere = set(
            await session.scalars(
                select(ApprovalDecision.actor_user_id).where(
                    ApprovalDecision.approval_id == approval.id,
                    ApprovalDecision.step_id != step.id,
                )
            )
        ) | {
            p
            for p in await session.scalars(
                select(ApprovalDecision.on_behalf_of).where(
                    ApprovalDecision.approval_id == approval.id,
                    ApprovalDecision.step_id != step.id,
                )
            )
            if p is not None
        }
        perms = await member_permissions(session, registry, candidates)
        added = sorted(
            (
                user
                for user in candidates
                if approval.permission in perms.get(user, frozenset())
                and user not in excluded_users(approval)
                and user not in decided_elsewhere
                and user not in step.assignee_ids
            ),
            key=str,
        )
        step.escalation_ids = sorted(set(step.escalation_ids) | set(added), key=str)
        step.escalated_at = now
        step.escalation_level += 1
        await session.flush()
        assert step.due_at is not None  # noqa: S101 (active steps always have one)
        await append_audit(
            session,
            action=Actions.ESCALATED,
            actor_type=ActorType.SYSTEM,
            actor_id=SYSTEM_ACTOR_ID,
            entity_type="approval",
            entity_id=approval.id,
            workflow=approval.workflow,
            payload={
                "step": step.name,
                "level": step.escalation_level,
                "added": [str(u) for u in added],
                "no_target": not added,
                "overdue_seconds": int((now - step.due_at).total_seconds()),
            },
        )
        await hooks.step_escalated(
            session,
            StepEscalated(
                org_id=approval.org_id,
                approval_id=approval.id,
                step_id=step.id,
                step_name=step.name,
                escalation_ids=tuple(step.escalation_ids),
                due_at=step.due_at,
            ),
        )
        results.append(EscalationResult(approval.id, step.id, tuple(added)))
    return results
