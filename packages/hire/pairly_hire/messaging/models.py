# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The messaging tables (migration ``hire_0003``). All org scoped, Row Level Security forced.

* ``hire_messages``: every message to or from a candidate, from the first draft to the
  sent email (``MessageStatus``). The values the template is rendered with and the
  rendered text are encrypted. A sensitive message (rejection, offer) carries the
  approval it waits for, and the run that sends it once approved; the sent email is the
  engine notification (``notification_id``).
* ``hire_candidate_prefs``: what a candidate chose about our messages: status updates
  by email on or off (non-transactional only: the outcome of an application and
  interview messages are always sent), and whether we may consider them for future
  roles (talent rediscovery). Only the candidate changes these, through a signed link.
* ``hire_messaging_settings``: per organisation: after how many days without an update
  the anti-ghosting SLA drafts one (default 7).

Statuses of a message (``MessageStatus``)::

    draft --send (non-sensitive)--> queued --(worker)--> sent | failed
    draft --submit (sensitive)--> submitted --(graph)--> awaiting_approval
                                   awaiting_approval --approved--> queued --> sent
                                   awaiting_approval --rejected--> declined
                                   awaiting_approval --changes requested--> changes_requested
    draft | changes_requested --cancel--> cancelled
    draft --send to someone who opted out--> suppressed
    (inbound, candidate portal later) received
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedJSON, Sealed
from pairly_core.db import Base, OrgScoped

MESSAGES = "hire_messages"
CANDIDATE_PREFS = "hire_candidate_prefs"
MESSAGING_SETTINGS = "hire_messaging_settings"

DEFAULT_SLA_DAYS = 7


class MessageStatus(StrEnum):
    DRAFT = "draft"
    # Submitted for approval; the approval is being created (``hire_message`` graph).
    SUBMITTED = "submitted"
    AWAITING_APPROVAL = "awaiting_approval"
    CHANGES_REQUESTED = "changes_requested"
    DECLINED = "declined"
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"
    CANCELLED = "cancelled"
    RECEIVED = "received"


# A draft a person can still edit, send or submit.
EDITABLE = frozenset({MessageStatus.DRAFT.value, MessageStatus.CHANGES_REQUESTED.value})


class MessagePurpose(StrEnum):
    MANUAL = "manual"  # a person picked the template
    REJECTION = "rejection"  # drafted by a move to the rejected stage
    SLA_UPDATE = "sla_update"  # drafted by the anti-ghosting SLA
    BULK = "bulk"  # one of a bulk action's messages
    REPLY = "reply"  # from the candidate (portal, later)


def _in(values: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class HireMessage(Base, OrgScoped):
    __tablename__ = MESSAGES
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_messages_application_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "candidate_id"],
            ["hire_candidates.org_id", "hire_candidates.id"],
            ondelete="RESTRICT",
            name="fk_hire_messages_candidate_org",
        ),
        CheckConstraint(f"status IN ({_in(MessageStatus)})", name="status_valid"),
        CheckConstraint(f"purpose IN ({_in(MessagePurpose)})", name="purpose_valid"),
        CheckConstraint("direction IN ('outbound', 'inbound')", name="direction_valid"),
        CheckConstraint("locale IN ('en', 'ar')", name="locale_valid"),
        CheckConstraint(
            "content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_format"
        ),
        CheckConstraint(
            "direction = 'inbound' OR template_key IS NOT NULL", name="outbound_has_template"
        ),
        CheckConstraint(
            "status <> 'awaiting_approval' OR approval_id IS NOT NULL",
            name="awaiting_has_approval",
        ),
        CheckConstraint("jsonb_typeof(ai_fields) = 'array'", name="ai_fields_array"),
        Index("ix_hire_messages_application", "org_id", "application_id", "created_at"),
        Index("ix_hire_messages_approval", "org_id", "approval_id"),
        Index("ix_hire_messages_run", "org_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    direction: Mapped[str] = mapped_column(
        String(8), nullable=False, default="outbound", server_default="outbound"
    )
    template_key: Mapped[str | None] = mapped_column(String(96), nullable=True)
    locale: Mapped[str] = mapped_column(String(2), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    # Every value the template is rendered with (``pairly_hire.messaging.variables``).
    variables: Mapped[Sealed] = mapped_column(EncryptedJSON(), nullable=False)
    # ``{"subject", "text"}``: the message as last previewed or sent (the timeline).
    rendered: Mapped[Sealed | None] = mapped_column(EncryptedJSON(), nullable=True)
    # Free-text variables an AI draft filled (and a person may have edited since).
    ai_fields: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    model_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # ``rubric_evidence`` when an AI reason came from the screening evidence.
    reason_source: Mapped[str | None] = mapped_column(String(24), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=True
    )
    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("notifications.id", ondelete="RESTRICT"), nullable=True
    )
    # Why it failed or was suppressed (short code), or the approver's outcome.
    status_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The person who submitted a sensitive message for approval (they start its run,
    # and can never approve it).
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HireCandidatePrefs(Base, OrgScoped):
    __tablename__ = CANDIDATE_PREFS
    __table_args__ = (
        UniqueConstraint("org_id", "candidate_id", name="uq_hire_candidate_prefs_candidate"),
        ForeignKeyConstraint(
            ["org_id", "candidate_id"],
            ["hire_candidates.org_id", "hire_candidates.id"],
            ondelete="RESTRICT",
            name="fk_hire_candidate_prefs_candidate_org",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # Status updates by email (non-transactional). Outcomes are always sent.
    updates_email: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Consent to be considered for future roles (talent rediscovery).
    talent_pool: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    talent_pool_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class HireMessagingSettings(Base, OrgScoped):
    __tablename__ = MESSAGING_SETTINGS
    __table_args__ = (
        UniqueConstraint("org_id", name="uq_hire_messaging_settings_org_id"),
        CheckConstraint("sla_days BETWEEN 1 AND 60", name="sla_days_range"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    sla_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_SLA_DAYS, server_default=str(DEFAULT_SLA_DAYS)
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = _created()
