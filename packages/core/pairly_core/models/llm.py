# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Model router tables (migration 0005_model_calls). All org-scoped with RLS forced.

* ``model_calls``: one row per attempt (sent request, or replayed recording). Metadata,
  the exact request (inputs and settings) and the model output, for cost reporting,
  decision records and replay. Append-only for the application role. ``request`` and
  ``response`` are encrypted (``EncryptedJSON``, migration 0008): write them with
  ``seal`` and read them with ``unseal``.
* ``llm_budget_reservations``: in-flight worst-case cost of calls that were admitted
  by the budget check but have not settled yet. Rows expire on their own.
* ``org_llm_budgets``: per-org override of the default monthly budget.
* ``llm_budget_alerts``: one row per (org, month, threshold) so an alert fires once.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedJSON, Sealed
from pairly_core.db import Base, OrgScoped


class ModelCall(Base, OrgScoped):
    __tablename__ = "model_calls"
    __table_args__ = (
        Index("ix_model_calls_org_id_request_hash", "org_id", "request_hash"),
        Index("ix_model_calls_org_id_created_at", "org_id", "created_at"),
        Index("ix_model_calls_org_id_chain_id", "org_id", "chain_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    chain_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    task: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    schema_name: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Encrypted (WP1.14): inputs and model output hold document content and personal data.
    request: Mapped[Sealed] = mapped_column(EncryptedJSON(), nullable=False)
    response: Mapped[Sealed | None] = mapped_column(EncryptedJSON(), nullable=True)
    # False when the provider cut the output off or refused (max tokens, refusal).
    output_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retries: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    from_recording: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    replay_of: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workflow: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Set when the person's data was erased: ``request`` then holds ``{"erased": true}``
    # and ``response`` is NULL; hashes, tokens and cost stay (migration 0019).
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LLMBudgetReservation(Base, OrgScoped):
    __tablename__ = "llm_budget_reservations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    amount_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrgLLMBudget(Base, OrgScoped):
    __tablename__ = "org_llm_budgets"
    __table_args__ = (UniqueConstraint("org_id", name="uq_org_llm_budgets_org_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    monthly_limit_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LLMBudgetAlert(Base, OrgScoped):
    __tablename__ = "llm_budget_alerts"
    __table_args__ = (
        UniqueConstraint("org_id", "period", "threshold_pct", name="uq_llm_budget_alerts_org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    period: Mapped[date] = mapped_column(Date, nullable=False)
    threshold_pct: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    spent_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    limit_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
