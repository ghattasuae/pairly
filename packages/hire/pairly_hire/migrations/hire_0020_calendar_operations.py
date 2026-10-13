# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire external calendar operations (M3): orphan event reconciliation.

* ``hire_calendar_operations`` (``pairly_hire.scheduling.models``): one row per write
  to an external calendar or meeting provider, recorded before the call in its own
  transaction and marked done by the interview's transaction; the reconciliation job
  (``pairly_hire.scheduling.operations``) settles rows left pending. Tenant table, RLS
  forced, standard tenant policy, the application role gets SELECT, INSERT and UPDATE
  (never DELETE), hidden from a candidate's portal session. No foreign key to the
  interview on purpose (the row is written while the interview is locked or not yet
  committed).

Revision ID: hire_0020
Revises: hire_0019
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0020"
down_revision: str | None = "hire_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "hire_calendar_operations"


def upgrade() -> None:
    t = _TABLE
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name=f"fk_{t}_org_id_organisations"
            ),
            nullable=False,
        ),
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        sa.Column(
            "organizer_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{t}_organizer_id_users"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("client_request_id", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(256), nullable=True),
        sa.Column("intended", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("rejected", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(64), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("org_id", "client_request_id", name="uq_hire_calendar_operations_key"),
        sa.CheckConstraint("kind IN ('event', 'meeting')", name=f"ck_{t}_kind_valid"),
        sa.CheckConstraint("action IN ('create', 'update', 'cancel')", name=f"ck_{t}_action_valid"),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'reconciled', 'failed')", name=f"ck_{t}_status_valid"
        ),
        sa.CheckConstraint("client_request_id ~ '^[0-9a-f]{32}$'", name=f"ck_{t}_key_format"),
        sa.CheckConstraint("jsonb_typeof(intended) = 'object'", name=f"ck_{t}_intended_object"),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_hire_calendar_operations_pending", t, ["org_id", "status", "updated_at"])
    for stmt in org_rls_statements(t, app_db_role(), privileges="SELECT, INSERT, UPDATE"):
        op.execute(stmt)
    op.execute(
        f"CREATE POLICY {t}_portal_subject ON {t} AS RESTRICTIVE "
        "USING (NOT hire_portal_active()) WITH CHECK (NOT hire_portal_active())"
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
