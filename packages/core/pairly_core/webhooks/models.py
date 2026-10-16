# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Webhook tables (migration 0013). All org scoped, RLS forced.

* ``webhook_endpoints``: where to send which event types. The signing secret is
  encrypted (``EncryptedString``). SELECT, INSERT, UPDATE for the application role.
* ``webhook_outbox``: events written in the same transaction as the business change
  (``pairly_core.webhooks.events.publish_event``). The worker fans them out to
  deliveries and sets ``dispatched_at``. SELECT, INSERT, UPDATE (``dispatched_at``),
  DELETE (retention).
* ``webhook_deliveries``: one row per (endpoint, event): attempts, next attempt, status,
  last response code and latency. SELECT, INSERT, UPDATE, DELETE (retention).
* ``inbound_events``: inbound provider events already processed, unique per
  (organisation, provider, event id). SELECT, INSERT, DELETE (retention).

Event payloads hold ids and short facts only (the same rule as audit payloads), never
document contents or secrets.
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

from pairly_core.crypto.types import EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped, Timestamped

_UUID_DEFAULT = text("gen_random_uuid()")
EVENT_TYPE_CHECK = "~ '^[a-z][a-z0-9_]*([.][a-z][a-z0-9_]*){1,4}$'"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # retries exhausted (24 hours)
    CANCELLED = "cancelled"  # the endpoint was disabled


class WebhookEndpoint(Base, OrgScoped, Timestamped):
    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        CheckConstraint("char_length(url) BETWEEN 8 AND 2048", name="url_length"),
        CheckConstraint("cardinality(event_types) BETWEEN 1 AND 100", name="event_types_count"),
        CheckConstraint("consecutive_failures >= 0", name="failures_non_negative"),
        CheckConstraint("active OR disabled_at IS NOT NULL", name="inactive_has_time"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    secret: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    event_types: Mapped[list[str]] = mapped_column(ARRAY(String(100)), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WebhookOutbox(Base, OrgScoped):
    __tablename__ = "webhook_outbox"
    __table_args__ = (
        CheckConstraint(f"event_type {EVENT_TYPE_CHECK}", name="event_type_format"),
        CheckConstraint("jsonb_typeof(data) = 'object'", name="data_object"),
        Index(
            "ix_webhook_outbox_pending",
            "org_id",
            "created_at",
            postgresql_where=text("dispatched_at IS NULL"),
        ),
    )

    # The event id (``evt_<hex>`` in payloads).
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookDelivery(Base, OrgScoped):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("endpoint_id", "event_id", name="uq_webhook_deliveries_endpoint_event"),
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'cancelled')", name="status_valid"
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
        Index(
            "ix_webhook_deliveries_due",
            "org_id",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_webhook_deliveries_endpoint", "org_id", "endpoint_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("webhook_endpoints.id", ondelete="RESTRICT"), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # The event envelope sent (serialised canonically, so every attempt is byte-identical).
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DeliveryStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # A worker that claimed the delivery holds it until then (crash safety).
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InboundEvent(Base, OrgScoped):
    __tablename__ = "inbound_events"
    __table_args__ = (
        UniqueConstraint("org_id", "provider", "event_id", name="uq_inbound_events_provider_event"),
        CheckConstraint("provider ~ '^[a-z][a-z0-9_]{0,31}$'", name="provider_format"),
        CheckConstraint("char_length(event_id) BETWEEN 1 AND 255", name="event_id_length"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
