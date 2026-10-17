# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Notification tables (migration 0011). All under Row Level Security.

* ``templates``: immutable template versions. ``org_id`` NULL rows are the shared system
  defaults (seeded by migrations, readable by every organisation, writable by none);
  organisation rows are that organisation's own versions. A new version is a new row:
  the application role may only SELECT and INSERT, and a trigger refuses UPDATE and
  DELETE for everyone.
* ``notifications``: one row per recipient and channel. ``content`` (subject, text, HTML,
  Slack blocks) is encrypted: it can hold signed approval link tokens and personal data.
  ``content_hash`` is the SHA-256 of the canonical rendered message, the value a
  sensitive template's approval is checked against. ``dedupe_key`` is unique per
  organisation, so retrying a ``notify`` call never sends twice.
* ``notification_preferences``: one row per person and organisation.
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.blind_index import blind_index_info
from pairly_core.crypto.types import EncryptedJSON, EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped, OrgScopedOrShared

_UUID_DEFAULT = text("gen_random_uuid()")


class NotificationStatus(StrEnum):
    QUEUED = "queued"  # waiting for the delivery job (possibly deferred)
    DIGEST = "digest"  # held for the user's daily digest
    SENT = "sent"
    FAILED = "failed"  # gave up after retries, or a permanent provider error
    SUPPRESSED = "suppressed"  # never sent: opted out, unlinked, rate limited


class Urgency(StrEnum):
    NORMAL = "normal"
    URGENT = "urgent"


class Template(Base, OrgScopedOrShared):
    __tablename__ = "templates"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "key",
            "channel",
            "locale",
            "version",
            name="uq_templates_version",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_templates_lookup", "key", "channel", "locale", "version"),
        CheckConstraint("channel IN ('email', 'slack', 'in_app')", name="channel_valid"),
        CheckConstraint("locale IN ('en', 'ar')", name="locale_valid"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(r"key ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,4}$'", name="key_format"),
        CheckConstraint("category ~ '^[a-z][a-z0-9_]{0,31}$'", name="category_format"),
        CheckConstraint("jsonb_typeof(variables) = 'object'", name="variables_object"),
        CheckConstraint("created_by_type IN ('user', 'system')", name="created_by_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    key: Mapped[str] = mapped_column(String(96), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    locale: Mapped[str] = mapped_column(String(8), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    variables: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Sensitive (rejection, offer, vendor messages): never sent without an approval
    # whose payload hash matches the rendered message.
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Notification(Base, OrgScoped):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("org_id", "dedupe_key", name="uq_notifications_dedupe"),
        Index("ix_notifications_due", "status", "next_attempt_at"),
        Index("ix_notifications_inbox", "recipient_user_id", "channel", "created_at"),
        Index("ix_notifications_recipient_bidx", "recipient_bidx", "category", "created_at"),
        Index(
            "uq_notifications_approval",
            "approval_id",
            unique=True,
            postgresql_where=text("approval_id IS NOT NULL"),
        ),
        CheckConstraint("channel IN ('email', 'slack', 'in_app')", name="channel_valid"),
        CheckConstraint(
            "status IN ('queued', 'digest', 'sent', 'failed', 'suppressed')", name="status_valid"
        ),
        CheckConstraint("urgency IN ('normal', 'urgent')", name="urgency_valid"),
        CheckConstraint("locale IN ('en', 'ar')", name="locale_valid"),
        CheckConstraint("attempts >= 0", name="attempts_positive"),
        CheckConstraint(
            "recipient_user_id IS NOT NULL OR recipient_address IS NOT NULL",
            name="has_recipient",
        ),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_format"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    # Email address or Slack user id the message goes to (encrypted).
    recipient_address: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    # Blind index of an external email address: rate limits and lookups without plaintext.
    recipient_bidx: Mapped[str | None] = mapped_column(
        String(64), nullable=True, info=blind_index_info("email", "recipient_address")
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    urgency: Mapped[str] = mapped_column(String(8), nullable=False)
    template_key: Mapped[str] = mapped_column(String(96), nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("templates.id", ondelete="RESTRICT"), nullable=True
    )
    locale: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[Sealed | None] = mapped_column(EncryptedJSON(), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=True
    )
    workflow: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # The digest notification that delivered this one (status ``sent`` via digest).
    digest_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the recipient's or the subject's data was erased (``content`` NULL).
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationPreference(Base, OrgScoped):
    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_notification_preferences_user"),
        CheckConstraint("locale IN ('en', 'ar')", name="locale_valid"),
        CheckConstraint("jsonb_typeof(channels) = 'object'", name="channels_object"),
        CheckConstraint("jsonb_typeof(digest) = 'object'", name="digest_object"),
        CheckConstraint("(quiet_start IS NULL) = (quiet_end IS NULL)", name="quiet_hours_complete"),
        CheckConstraint(
            "quiet_start IS NULL OR (quiet_start BETWEEN 0 AND 1439 "
            "AND quiet_end BETWEEN 0 AND 1439)",
            name="quiet_hours_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    locale: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    # {category: [channel, ...]}: the channels enabled per category (missing: defaults).
    channels: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # {category: "realtime" | "daily"}
    digest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Quiet hours in the user's time zone, minutes after midnight. May wrap midnight.
    quiet_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quiet_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
