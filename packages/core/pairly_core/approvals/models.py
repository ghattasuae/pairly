# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Approval tables. Every table is org scoped with Row Level Security (migration 0006).

* ``approvals``: one request for a human decision on one step of one workflow run.
  ``payload_hash`` is the SHA-256 of the canonical JSON of the action the approval
  covers; the executor recomputes it before any final action. ``state_version`` grows on
  every state change, and signed links are bound to the version they were issued for.
* ``approval_steps``: the chain chosen from the rules, one row per sequential step,
  ``any_of`` or ``all_of`` over ``assignee_ids`` (principals). ``escalation_ids`` are
  added by the SLA job.
* ``approval_decisions``: append only (no UPDATE or DELETE for the application role).
  One decision per person per step. ``on_behalf_of`` is set when a delegate decided for a
  principal.
* ``approval_delegations``: delegation and out-of-office periods.
* ``approval_links``: signed single-use links (``pairly_core.signed_links``). Only the
  SHA-256 of the token is stored.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped

_UUID_DEFAULT = text("gen_random_uuid()")


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    CANCELLED = "cancelled"


DECIDED_STATUSES = frozenset(
    {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.CHANGES_REQUESTED}
)
TERMINAL_STATUSES = DECIDED_STATUSES | {ApprovalStatus.CANCELLED}


class StepStatus(StrEnum):
    WAITING = "waiting"
    ACTIVE = "active"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    CANCELLED = "cancelled"


class StepMode(StrEnum):
    ANY_OF = "any_of"
    ALL_OF = "all_of"


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"


class Channel(StrEnum):
    WEB = "web"
    EMAIL_LINK = "email_link"
    SLACK_LINK = "slack_link"


class DelegationKind(StrEnum):
    DELEGATION = "delegation"
    OUT_OF_OFFICE = "out_of_office"


class Approval(Base, OrgScoped):
    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_org_inbox", "org_id", "permission", "created_at", "id"),
        UniqueConstraint("run_id", "step_key", "revision", name="uq_approvals_run_step_revision"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'changes_requested', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="payload_hash_format"),
        CheckConstraint("submitted_by_type IN ('user', 'agent', 'system')", name="submitter_valid"),
        CheckConstraint("revision >= 1", name="revision_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    workflow: Mapped[str] = mapped_column(String(32), nullable=False)
    approval_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # The workflow permission a decider must hold, for example ``pay.invoice.approve``.
    permission: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # What the approver sees, and the exact action the approval covers.
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    submitted_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    submitted_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # People who prepared the item (submitter included). None of them may decide.
    preparer_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, server_default=text("'{}'::uuid[]")
    )
    policy_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("policy_versions.id", ondelete="RESTRICT"), nullable=False
    )
    chain_name: Mapped[str] = mapped_column(String(64), nullable=False)
    current_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the subject's data was erased: ``context`` and ``payload`` then hold
    # ``{"erased": true}`` (migration 0019).
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovalStep(Base, OrgScoped):
    __tablename__ = "approval_steps"
    __table_args__ = (
        Index("ix_approval_steps_due", "status", "due_at"),
        UniqueConstraint("approval_id", "position", name="uq_approval_steps_position"),
        CheckConstraint("mode IN ('any_of', 'all_of')", name="mode_valid"),
        CheckConstraint(
            "status IN ('waiting', 'active', 'approved', 'rejected', 'changes_requested', "
            "'cancelled')",
            name="status_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    approval_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("approvals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    assignee_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), nullable=False)
    escalation_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, server_default=text("'{}'::uuid[]")
    )
    sla_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    warn_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovalDecision(Base, OrgScoped):
    __tablename__ = "approval_decisions"
    __table_args__ = (
        UniqueConstraint("step_id", "actor_user_id", name="uq_approval_decisions_step_actor"),
        UniqueConstraint(
            "org_id", "actor_user_id", "idempotency_key", name="uq_approval_decisions_idempotency"
        ),
        CheckConstraint(
            "decision IN ('approve', 'reject', 'request_changes')", name="decision_valid"
        ),
        CheckConstraint("channel IN ('web', 'email_link', 'slack_link')", name="channel_valid"),
        CheckConstraint(
            "decision = 'approve' OR (reason IS NOT NULL AND length(btrim(reason)) > 0)",
            name="reason_required",
        ),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="request_hash_format"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    approval_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    step_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("approval_steps.id", ondelete="RESTRICT"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    on_behalf_of: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    # True when the decider was an escalation assignee of the step.
    via_escalation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    permission: Mapped[str] = mapped_column(String(128), nullable=False)
    second_factor: Mapped[bool] = mapped_column(Boolean, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ApprovalDelegation(Base, OrgScoped):
    __tablename__ = "approval_delegations"
    __table_args__ = (
        CheckConstraint("delegator_id <> delegate_id", name="not_self"),
        CheckConstraint("ends_at > starts_at", name="range_valid"),
        CheckConstraint("kind IN ('delegation', 'out_of_office')", name="kind_valid"),
        CheckConstraint(
            "workflow IS NULL OR workflow ~ '^[a-z][a-z0-9_]{0,31}$'", name="workflow_format"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    delegator_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    delegate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # NULL: every workflow.
    workflow: Mapped[str | None] = mapped_column(String(32), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovalLink(Base, OrgScoped):
    __tablename__ = "approval_links"
    __table_args__ = (
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_format"),
        CheckConstraint("action IN ('approve', 'reject', 'request_changes')", name="action_valid"),
        CheckConstraint("channel IN ('email_link', 'slack_link')", name="channel_valid"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint("used_at IS NULL OR invalidated_at IS NULL", name="single_outcome"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    approval_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    step_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("approval_steps.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    approval_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
