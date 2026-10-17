# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The executor: the only way to run a final action (pay, reject, send, delete).

It does not trust the graph, the resume value or the approval's ``status`` column alone.
Before running the action it locks the approval and re-checks, from the database:

1. The approval belongs to this organisation (RLS), this run, this step key and this
   workflow, and is ``approved`` and not executed yet (at most once).
2. The payload the node wants to act on hashes to the approval's ``payload_hash``: the
   action is exactly what the humans approved, not something changed afterwards in the
   run state.
3. The run is executing (``running``), for the graph that owns the action.
4. The decision rows prove the approval, independently of the status columns: every step
   is complete for its mode (``any_of`` / ``all_of`` / escalation), there is no reject or
   change request, every approving decision was made by a person holding the approval's
   permission with a second factor, as an approver, an escalation approver, or a delegate
   whose delegation covered the moment of the decision. No decider is the submitter or a
   preparer, and nobody decided two steps.
5. Every approver is still a member who holds the permission now (a role removed since
   the decision blocks execution).
6. The rules version the approval used still exists.

Only then does it run the action, in the same transaction that marks the approval
executed and writes the audit event, so the action and its record commit together or
not at all. Any failed check raises ``ExecutorRefusedError`` (fail closed) and nothing
runs. A prompt, a forged resume value or an edited run state can therefore never trigger
a final action.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.hooks import NO_HOOKS, ApprovalHooks, FinalActionExecuted
from pairly_core.approvals.models import (
    Approval,
    ApprovalDecision,
    ApprovalDelegation,
    ApprovalStatus,
    ApprovalStep,
    Decision,
)
from pairly_core.approvals.service import (
    InvalidPayloadError,
    canonical_payload,
    db_now,
    excluded_users,
    member_permissions,
    step_complete,
)
from pairly_core.audit import ActorType, append_audit
from pairly_core.db import current_org
from pairly_core.errors import ForbiddenError
from pairly_core.policy_versions import get_policy
from pairly_core.rbac import RoleRegistry
from pairly_core.workflow.models import RunStatus, WorkflowRun
from pairly_core.workflow.spec import FinalActionCall, GraphSpec

ACTION_EXECUTED = "core.workflow.action_executed"
ACTION_REFUSED = "core.workflow.action_refused"
EXECUTOR_ACTOR_ID = "pairly.workflow.executor"


class ExecutorRefusedError(ForbiddenError):
    """A final action was refused: the approval does not cover it."""

    code = "executor_refused"
    title = "Final action refused"

    def __init__(self, reason: str) -> None:
        super().__init__(f"final action refused: {reason}")
        self.reason = reason


async def _decisions_valid(
    session: AsyncSession,
    registry: RoleRegistry,
    approval: Approval,
) -> tuple[uuid.UUID, ...]:
    """Re-derive the approval from its decision rows. Returns the approvers."""
    steps = list(
        await session.scalars(
            select(ApprovalStep)
            .where(ApprovalStep.approval_id == approval.id)
            .order_by(ApprovalStep.position)
        )
    )
    decisions = list(
        await session.scalars(
            select(ApprovalDecision).where(ApprovalDecision.approval_id == approval.id)
        )
    )
    if not steps:
        raise ExecutorRefusedError("approval_has_no_steps")
    if any(d.decision != Decision.APPROVE.value for d in decisions):
        raise ExecutorRefusedError("approval_has_a_negative_decision")
    step_ids = {s.id for s in steps}
    if any(d.step_id not in step_ids for d in decisions):
        raise ExecutorRefusedError("decision_outside_chain")

    excluded = excluded_users(approval)
    seen: dict[uuid.UUID, uuid.UUID] = {}  # person -> step
    approvers: list[uuid.UUID] = []
    principals: list[uuid.UUID] = []
    for step in steps:
        on_step = [d for d in decisions if d.step_id == step.id]
        if not step_complete(step, decisions):
            raise ExecutorRefusedError("step_not_complete")
        for d in on_step:
            if d.permission != approval.permission or not d.second_factor:
                raise ExecutorRefusedError("decision_without_permission_or_second_factor")
            people = {d.actor_user_id} | ({d.on_behalf_of} if d.on_behalf_of else set())
            if people & excluded:
                raise ExecutorRefusedError("segregation_of_duties")
            for person in people:
                if seen.setdefault(person, step.id) != step.id:
                    raise ExecutorRefusedError("one_person_two_steps")
            if d.on_behalf_of is not None:
                if d.on_behalf_of not in step.assignee_ids:
                    raise ExecutorRefusedError("delegate_for_non_approver")
                covered = await session.scalar(
                    select(ApprovalDelegation.id)
                    .where(
                        ApprovalDelegation.delegator_id == d.on_behalf_of,
                        ApprovalDelegation.delegate_id == d.actor_user_id,
                        ApprovalDelegation.starts_at <= d.created_at,
                        ApprovalDelegation.ends_at > d.created_at,
                        (ApprovalDelegation.revoked_at.is_(None))
                        | (ApprovalDelegation.revoked_at > d.created_at),
                        (ApprovalDelegation.workflow.is_(None))
                        | (ApprovalDelegation.workflow == approval.workflow),
                    )
                    .limit(1)
                )
                if covered is None:
                    raise ExecutorRefusedError("delegation_not_covering_decision")
            elif d.via_escalation:
                if d.actor_user_id not in step.escalation_ids:
                    raise ExecutorRefusedError("escalation_decider_not_assigned")
            elif d.actor_user_id not in step.assignee_ids:
                raise ExecutorRefusedError("decider_not_assigned")
            approvers.append(d.actor_user_id)
            if d.on_behalf_of is not None:
                principals.append(d.on_behalf_of)

    # Everyone whose seat counted still holds the permission: the person who decided
    # and, for a delegate, the approver they stood in for (a delegation never outlives
    # its holder's role; security review 2026-10, M-01).
    current = await member_permissions(session, registry, [*approvers, *principals])
    for person in (*approvers, *principals):
        if approval.permission not in current.get(person, frozenset()):
            raise ExecutorRefusedError("approver_lost_permission")
    return tuple(dict.fromkeys(approvers))


async def execute_final_action(
    session: AsyncSession,
    registry: RoleRegistry,
    spec: GraphSpec,
    *,
    action: str,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    step_key: str,
    approval_id: uuid.UUID,
    payload: Mapping[str, Any],
    hooks: ApprovalHooks = NO_HOOKS,
) -> dict[str, Any]:
    """Re-check the approval, then run ``spec.final_actions[action]``. See module docstring.

    Runs in the caller's transaction; the caller commits. On refusal nothing was written.
    """
    if current_org(session) != org_id:
        raise ExecutorRefusedError("tenant_mismatch")
    final_action = spec.final_actions.get(action)
    if final_action is None:
        raise ExecutorRefusedError("unknown_action")
    approval = await session.scalar(
        select(Approval).where(Approval.id == approval_id).with_for_update()
    )
    if approval is None or approval.org_id != org_id:
        raise ExecutorRefusedError("approval_not_found")
    if (
        approval.run_id != run_id
        or approval.step_key != step_key
        or approval.workflow != spec.workflow
    ):
        raise ExecutorRefusedError("approval_for_another_run_or_step")
    if approval.status != ApprovalStatus.APPROVED.value:
        raise ExecutorRefusedError("approval_not_approved")
    if approval.executed_at is not None:
        raise ExecutorRefusedError("already_executed")
    try:
        clean_payload, digest = canonical_payload(payload)
    except InvalidPayloadError:
        raise ExecutorRefusedError("payload_invalid") from None
    if digest != approval.payload_hash:
        raise ExecutorRefusedError("payload_hash_mismatch")

    run = await session.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id))
    if run is None or run.status != RunStatus.RUNNING.value:
        raise ExecutorRefusedError("run_not_executing")
    if run.workflow != spec.workflow or run.graph_name != spec.name:
        raise ExecutorRefusedError("run_of_another_graph")
    if await get_policy(session, approval.policy_version_id) is None:
        raise ExecutorRefusedError("rules_version_missing")
    approvers = await _decisions_valid(session, registry, approval)

    result = dict(
        await final_action(
            session,
            FinalActionCall(
                org_id=org_id,
                run_id=run_id,
                approval_id=approval.id,
                step_key=step_key,
                payload=clean_payload,
                approver_ids=approvers,
            ),
        )
    )
    now = await db_now(session)
    approval.executed_at = now
    await session.flush()
    await append_audit(
        session,
        action=ACTION_EXECUTED,
        actor_type=ActorType.SYSTEM,
        actor_id=EXECUTOR_ACTOR_ID,
        entity_type="workflow_run",
        entity_id=run_id,
        workflow=spec.workflow,
        payload={
            "action": action,
            "approval_id": str(approval.id),
            "payload_hash": digest,
            "policy_version_id": str(approval.policy_version_id),
            "approvers": [str(a) for a in approvers],
        },
    )
    await hooks.final_action_executed(
        session,
        FinalActionExecuted(
            org_id=org_id,
            approval_id=approval.id,
            run_id=run_id,
            workflow=spec.workflow,
            action=action,
            payload_hash=digest,
            policy_version_id=approval.policy_version_id,
            approver_ids=approvers,
            result=result,
            executed_at=now,
        ),
    )
    return result


async def record_refusal(
    session: AsyncSession,
    *,
    workflow: str,
    run_id: uuid.UUID,
    approval_id: uuid.UUID | None,
    action: str,
    reason: str,
) -> None:
    """Audit a refused final action. Call in a fresh transaction after the rollback."""
    await append_audit(
        session,
        action=ACTION_REFUSED,
        actor_type=ActorType.SYSTEM,
        actor_id=EXECUTOR_ACTOR_ID,
        entity_type="workflow_run",
        entity_id=run_id,
        workflow=workflow,
        payload={
            "action": action,
            "approval_id": str(approval_id) if approval_id else None,
            "reason": reason,
        },
    )
