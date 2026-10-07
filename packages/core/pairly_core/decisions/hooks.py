# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The approval hook that writes decision records (``pairly_core.approvals.hooks``).

* ``approval_decided``: a person approved, rejected or asked for changes on one step.
  The record (kind ``approval``) supersedes the newest record of the approval's entity
  (for example the AI risk score of the invoice), so "AI said, then a person decided"
  is one line of records.
* ``final_action_executed``: the executor ran the approved final action. The record
  (kind ``final_action``) supersedes the last approval record.

Both run inside the transaction that made the change; a failure aborts it (fail closed).
"""

from __future__ import annotations

import re
import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.hooks import ApprovalDecided, BaseApprovalHook, FinalActionExecuted
from pairly_core.approvals.models import Approval
from pairly_core.audit import ActorType
from pairly_core.decisions.models import DecisionKind
from pairly_core.decisions.service import DecisionDraft, PolicyRef, record_decision
from pairly_core.models.audit import AuditEvent
from pairly_core.policy_versions import get_policy

APPROVAL_DECIDED_ACTION: Final = "core.approval.decided"
ACTION_EXECUTED_ACTION: Final = "core.workflow.action_executed"
EXECUTOR_ACTOR_ID: Final = "pairly.workflow.executor"
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_:.-]{0,63}$")


async def _approval(session: AsyncSession, approval_id: uuid.UUID) -> Approval:
    row = await session.scalar(select(Approval).where(Approval.id == approval_id))
    if row is None:  # pragma: no cover - the hook runs right after the approval changed
        raise LookupError("approval not visible to its own hook")
    return row


def _subject(approval: Approval) -> tuple[str, str]:
    if approval.entity_type and approval.entity_id:
        return approval.entity_type, approval.entity_id
    return "approval", str(approval.id)


async def _policy_refs(session: AsyncSession, policy_version_id: uuid.UUID) -> list[PolicyRef]:
    policy = await get_policy(session, policy_version_id)
    if policy is None:
        return []
    return [PolicyRef(policy.id, policy.kind, policy.name, policy.version, policy.content_hash)]


async def _event_id(
    session: AsyncSession, *, action: str, entity_type: str, entity_id: str, key: str, value: str
) -> uuid.UUID | None:
    """The audit event the approvals module just wrote for this change."""
    return await session.scalar(
        select(AuditEvent.id)
        .where(
            AuditEvent.action == action,
            AuditEvent.entity_type == entity_type,
            AuditEvent.entity_id == entity_id,
            AuditEvent.payload[key].astext == value,
        )
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    )


def _executed(action: str) -> str:
    value = f"executed:{action}"
    return value if _ACTION_RE.fullmatch(value) else "executed"


class DecisionRecordHook(BaseApprovalHook):
    """Writes a decision record for every human approval decision and final action."""

    async def approval_decided(self, session: AsyncSession, event: ApprovalDecided) -> None:
        approval = await _approval(session, event.approval_id)
        entity_type, entity_id = _subject(approval)
        audit_id = await _event_id(
            session,
            action=APPROVAL_DECIDED_ACTION,
            entity_type="approval",
            entity_id=str(event.approval_id),
            key="decision_id",
            value=str(event.decision_id),
        )
        await record_decision(
            session,
            DecisionDraft(
                kind=DecisionKind.APPROVAL,
                workflow=event.workflow,
                entity_type=entity_type,
                entity_id=entity_id,
                actor_type=ActorType.USER,
                actor_id=str(event.actor_user_id),
                inputs={
                    "approval_id": str(event.approval_id),
                    "run_id": str(event.run_id),
                    "approval_type": event.approval_type,
                    "payload_hash": event.payload_hash,
                    "step": event.step_name,
                    "step_id": str(event.step_id),
                    "approval_decision_id": str(event.decision_id),
                    "channel": event.channel,
                },
                policy_versions=await _policy_refs(session, event.policy_version_id),
                output={
                    "decision": event.decision,
                    "approval_status": event.approval_status,
                    "on_behalf_of": str(event.on_behalf_of) if event.on_behalf_of else None,
                },
                final_action=event.decision,
                reason=event.reason,
                decided_at=event.decided_at,
                audit_event_ids=(audit_id,) if audit_id else (),
                approval_ids=(event.approval_id,),
            ),
        )

    async def final_action_executed(
        self, session: AsyncSession, event: FinalActionExecuted
    ) -> None:
        approval = await _approval(session, event.approval_id)
        entity_type, entity_id = _subject(approval)
        audit_id = await _event_id(
            session,
            action=ACTION_EXECUTED_ACTION,
            entity_type="workflow_run",
            entity_id=str(event.run_id),
            key="approval_id",
            value=str(event.approval_id),
        )
        await record_decision(
            session,
            DecisionDraft(
                kind=DecisionKind.FINAL_ACTION,
                workflow=event.workflow,
                entity_type=entity_type,
                entity_id=entity_id,
                actor_type=ActorType.SYSTEM,
                actor_id=EXECUTOR_ACTOR_ID,
                inputs={
                    "approval_id": str(event.approval_id),
                    "run_id": str(event.run_id),
                    "payload_hash": event.payload_hash,
                    "action": event.action,
                },
                policy_versions=await _policy_refs(session, event.policy_version_id),
                output={
                    "action": event.action,
                    "approver_ids": [str(a) for a in event.approver_ids],
                    "result_keys": sorted(str(k) for k in event.result)[:50],
                },
                final_action=_executed(event.action),
                # The action's result can hold business or personal data.
                sensitive={"result": event.result},
                decided_at=event.executed_at,
                audit_event_ids=(audit_id,) if audit_id else (),
                approval_ids=(event.approval_id,),
            ),
        )
