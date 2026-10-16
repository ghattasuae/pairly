# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire counterfactual fairness runner (M3, brief 3.3.4).

Tables (``pairly_hire.analytics.models``), tenant tables, RLS forced, standard tenant
policy, the application role gets SELECT, INSERT and UPDATE (never DELETE), hidden from
a candidate's portal session:

* ``hire_fairness_runs``: one run over a sample of screened applications.
* ``hire_fairness_results``: one counterfactual copy (synthetic values only).

Revision ID: hire_0017
Revises: hire_0016
Create Date: 2026-10-13
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0017"
down_revision: str | None = "hire_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("hire_fairness_runs", "hire_fairness_results")


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name=f"fk_{table}_org_id_organisations"
            ),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    t = "hire_fairness_runs"
    op.create_table(
        t,
        *_base(t),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("threshold", sa.Numeric(5, 2), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("applications", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("copies", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("flagged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "summary", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("started_by", sa.String(128), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(200), nullable=True),
        sa.UniqueConstraint("org_id", "id", name="uq_hire_fairness_runs_org_id_id"),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')", name=f"ck_{t}_status_valid"
        ),
        sa.CheckConstraint("trigger IN ('scheduled', 'manual')", name=f"ck_{t}_trigger_valid"),
        sa.CheckConstraint("mode IN ('live', 'recorded')", name=f"ck_{t}_mode_valid"),
        sa.CheckConstraint("jsonb_typeof(summary) = 'object'", name=f"ck_{t}_summary_object"),
    )
    op.create_index("ix_hire_fairness_runs_started", t, ["org_id", "started_at"])

    t = "hire_fairness_results"
    op.create_table(
        t,
        *_base(t),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("screening_id", sa.Uuid(), nullable=False),
        sa.Column("dimension", sa.String(16), nullable=False),
        sa.Column("inserted", postgresql.JSONB(), nullable=False),
        sa.Column("leaked", postgresql.JSONB(), nullable=False),
        sa.Column("redaction_complete", sa.Boolean(), nullable=False),
        sa.Column("input_identical", sa.Boolean(), nullable=False),
        sa.Column("blind", sa.Boolean(), nullable=False),
        sa.Column("model_status", sa.String(48), nullable=False),
        sa.Column("control_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("copy_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("delta", sa.Numeric(5, 2), nullable=True),
        sa.Column("flagged", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "run_id"],
            ["hire_fairness_runs.org_id", "hire_fairness_runs.id"],
            ondelete="RESTRICT",
            name="fk_hire_fairness_results_run_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_fairness_results_application_org",
        ),
        sa.CheckConstraint(
            "dimension IN ('name', 'gender', 'nationality')", name=f"ck_{t}_dimension_valid"
        ),
        sa.CheckConstraint("jsonb_typeof(inserted) = 'array'", name=f"ck_{t}_inserted_array"),
        sa.CheckConstraint("jsonb_typeof(leaked) = 'array'", name=f"ck_{t}_leaked_array"),
    )
    op.create_index("ix_hire_fairness_results_run", t, ["org_id", "run_id"])

    role = app_db_role()
    for table in _TABLES:
        op.create_index(f"ix_{table}_org_id", table, ["org_id"])
        for stmt in org_rls_statements(table, role, privileges="SELECT, INSERT, UPDATE"):
            op.execute(stmt)
        op.execute(
            f"CREATE POLICY {table}_portal_subject ON {table} AS RESTRICTIVE "
            "USING (NOT hire_portal_active()) WITH CHECK (NOT hire_portal_active())"
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
