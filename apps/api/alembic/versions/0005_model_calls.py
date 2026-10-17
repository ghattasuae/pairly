# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Model router tables (WP1.7): model_calls, budget reservations, overrides and alerts.

Every table is org-scoped with Row Level Security forced (standard tenant policy).
Application role privileges:

* ``model_calls``: SELECT, INSERT. Append-only: a call record is never changed.
* ``llm_budget_reservations``: SELECT, INSERT, DELETE (reservations are settled or
  expire).
* ``org_llm_budgets``: SELECT, INSERT, UPDATE (admin sets the org's monthly limit;
  the API checks RBAC before writing).
* ``llm_budget_alerts``: SELECT, INSERT (one row per org, month and threshold).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _org_fk(table: str) -> sa.Column[Any]:
    return sa.Column(
        "org_id",
        sa.Uuid(),
        sa.ForeignKey(
            "organisations.id", ondelete="RESTRICT", name=f"fk_{table}_org_id_organisations"
        ),
        nullable=False,
    )


def _id() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _created() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    app_role = app_db_role()

    op.create_table(
        "model_calls",
        _id(),
        _org_fk("model_calls"),
        _created(),
        sa.Column("chain_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.SmallInteger(), nullable=False),
        sa.Column("task", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("schema_name", sa.String(128), nullable=False),
        sa.Column("schema_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("error_kind", sa.String(64), nullable=True),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("response", postgresql.JSONB(), nullable=True),
        sa.Column("output_complete", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("stop_reason", sa.String(64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retries", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("from_recording", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_call_id", sa.Uuid(), nullable=True),
        sa.Column("replay_of", sa.Uuid(), nullable=True),
        sa.Column("provider_request_id", sa.String(128), nullable=True),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column("workflow", sa.String(32), nullable=True),
        sa.Column("entity_type", sa.String(64), nullable=True),
        sa.Column("entity_id", sa.String(128), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.CheckConstraint(
            "mode IN ('live', 'record', 'recorded', 'import')", name="ck_model_calls_mode"
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'invalid_output', 'low_confidence', 'provider_error')",
            name="ck_model_calls_status",
        ),
        sa.CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_model_calls_request_hash"),
        sa.CheckConstraint("prompt_hash ~ '^[0-9a-f]{64}$'", name="ck_model_calls_prompt_hash"),
        sa.CheckConstraint("schema_hash ~ '^[0-9a-f]{64}$'", name="ck_model_calls_schema_hash"),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND cost_micros >= 0 AND latency_ms >= 0",
            name="ck_model_calls_non_negative",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_model_calls_confidence_range",
        ),
    )
    op.create_index("ix_model_calls_org_id", "model_calls", ["org_id"])
    op.create_index("ix_model_calls_org_id_request_hash", "model_calls", ["org_id", "request_hash"])
    op.create_index("ix_model_calls_org_id_created_at", "model_calls", ["org_id", "created_at"])
    op.create_index("ix_model_calls_org_id_chain_id", "model_calls", ["org_id", "chain_id"])

    op.create_table(
        "llm_budget_reservations",
        _id(),
        _org_fk("llm_budget_reservations"),
        sa.Column("amount_micros", sa.BigInteger(), nullable=False),
        _created(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_micros >= 0", name="ck_llm_budget_reservations_amount"),
    )
    op.create_index("ix_llm_budget_reservations_org_id", "llm_budget_reservations", ["org_id"])

    op.create_table(
        "org_llm_budgets",
        _id(),
        _org_fk("org_llm_budgets"),
        sa.Column("monthly_limit_micros", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("org_id", name="uq_org_llm_budgets_org_id"),
        sa.CheckConstraint("monthly_limit_micros >= 0", name="ck_org_llm_budgets_limit"),
    )
    op.create_index("ix_org_llm_budgets_org_id", "org_llm_budgets", ["org_id"])

    op.create_table(
        "llm_budget_alerts",
        _id(),
        _org_fk("llm_budget_alerts"),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("threshold_pct", sa.SmallInteger(), nullable=False),
        sa.Column("spent_micros", sa.BigInteger(), nullable=False),
        sa.Column("limit_micros", sa.BigInteger(), nullable=False),
        _created(),
        sa.UniqueConstraint(
            "org_id", "period", "threshold_pct", name="uq_llm_budget_alerts_org_id"
        ),
        sa.CheckConstraint(
            "threshold_pct > 0 AND threshold_pct <= 100", name="ck_llm_budget_alerts_threshold"
        ),
    )
    op.create_index("ix_llm_budget_alerts_org_id", "llm_budget_alerts", ["org_id"])

    for table, privileges in (
        ("model_calls", "SELECT, INSERT"),
        ("llm_budget_reservations", "SELECT, INSERT, DELETE"),
        ("org_llm_budgets", "SELECT, INSERT, UPDATE"),
        ("llm_budget_alerts", "SELECT, INSERT"),
    ):
        for stmt in org_rls_statements(table, app_role, privileges=privileges):
            op.execute(stmt)


def downgrade() -> None:
    op.drop_table("llm_budget_alerts")
    op.drop_table("org_llm_budgets")
    op.drop_table("llm_budget_reservations")
    op.drop_table("model_calls")
