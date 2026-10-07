# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Integration tables (migration 0011). All under Row Level Security.

* ``integrations``: one row per organisation and provider (Slack today). The provider's
  credential (the Slack bot token) is encrypted with ``pairly_core.crypto``, bound to
  the row. ``external_id`` is the provider's workspace id (Slack team id); it maps to
  exactly one organisation, so an incoming Slack request routes to one tenant.
* ``slack_user_links``: a verified link between a Slack user and a Pairly user, made by
  the signed connect flow (``pairly_core.integrations.slack.service``). Without one, no
  Slack click acts for anybody.
* ``slack_link_requests``: pending connect requests. Only the token's SHA-256 is kept.
* ``slack_interactions``: every handled Slack action, unique by Slack's ``action_ts``,
  so a retried delivery is answered without acting twice.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped

_UUID_DEFAULT = text("gen_random_uuid()")


class Integration(Base, OrgScoped):
    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("org_id", "provider", name="uq_integrations_provider"),
        UniqueConstraint("provider", "external_id", name="uq_integrations_external"),
        CheckConstraint("provider IN ('slack')", name="provider_valid"),
        CheckConstraint("status IN ('active', 'disabled')", name="status_valid"),
        CheckConstraint("jsonb_typeof(settings) = 'object'", name="settings_object"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    credential: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    # Slack: {"channel_mappings": {"<category>": "<channel id>"}}
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SlackUserLink(Base, OrgScoped):
    __tablename__ = "slack_user_links"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_slack_user_links_user"),
        UniqueConstraint("org_id", "team_id", "slack_user_id", name="uq_slack_user_links_slack"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    team_id: Mapped[str] = mapped_column(String(32), nullable=False)
    slack_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SlackLinkRequest(Base, OrgScoped):
    __tablename__ = "slack_link_requests"
    __table_args__ = (
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_format"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    team_id: Mapped[str] = mapped_column(String(32), nullable=False)
    slack_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )


class SlackInteraction(Base, OrgScoped):
    __tablename__ = "slack_interactions"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "team_id", "slack_user_id", "action_ts", name="uq_slack_interactions_action"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    team_id: Mapped[str] = mapped_column(String(32), nullable=False)
    slack_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action_ts: Mapped[str] = mapped_column(String(32), nullable=False)
    action_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
