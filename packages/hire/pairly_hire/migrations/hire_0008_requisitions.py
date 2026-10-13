# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire requisitions (M3, brief 3.1.1).

* ``hire_requisitions``: a requested role (title, team, budget, headcount, location,
  work model, start date) and its approval (``pairly_hire.requisitions.models``).
  Tenant table, RLS forced, standard tenant policy; the application role gets SELECT,
  INSERT and UPDATE, never DELETE. Hidden from a candidate's portal session.
* ``hire_jobs.requisition_id``: the approved requisition a job was opened from, by
  ``(org_id, id)``, so a job can never point at another organisation's requisition.

Revision ID: hire_0008
Revises: hire_0007
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0008"
down_revision: str | None = "hire_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_T = "hire_requisitions"
_STATUSES = "'pending_approval', 'approved', 'rejected', 'changes_requested', 'cancelled', 'failed'"


def upgrade() -> None:
    op.create_table(
        _T,
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name=f"fk_{_T}_org_id_organisations"
            ),
            nullable=False,
        ),
        sa.Column("reference", sa.String(16), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("title_ar", sa.String(160), nullable=False, server_default=""),
        sa.Column("team", sa.String(120), nullable=False),
        sa.Column("budget", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("headcount", sa.Integer(), nullable=False),
        sa.Column("location", sa.String(160), nullable=False),
        sa.Column("work_model", sa.String(16), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("justification", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("status_reason", sa.String(64), nullable=True),
        sa.Column(
            "requested_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{_T}_requested_by_users"),
            nullable=False,
        ),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("org_id", "id", name="uq_hire_requisitions_org_id_id"),
        sa.UniqueConstraint("org_id", "reference", name="uq_hire_requisitions_reference"),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name=f"ck_{_T}_status_valid"),
        sa.CheckConstraint("headcount BETWEEN 1 AND 100", name=f"ck_{_T}_headcount_range"),
        sa.CheckConstraint("budget > 0", name=f"ck_{_T}_budget_positive"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=f"ck_{_T}_currency_format"),
        sa.CheckConstraint(
            "work_model IN ('onsite', 'hybrid', 'remote')", name=f"ck_{_T}_work_model_valid"
        ),
    )
    op.create_index("ix_hire_requisitions_org_id", _T, ["org_id"])
    op.create_index("ix_hire_requisitions_status", _T, ["org_id", "status", "created_at"])
    for stmt in org_rls_statements(_T, app_db_role(), privileges="SELECT, INSERT, UPDATE"):
        op.execute(stmt)
    op.execute(
        f"CREATE POLICY {_T}_portal_subject ON {_T} AS RESTRICTIVE "
        "USING (NOT hire_portal_active()) WITH CHECK (NOT hire_portal_active())"
    )

    op.add_column("hire_jobs", sa.Column("requisition_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_hire_jobs_requisition_org",
        "hire_jobs",
        _T,
        ["org_id", "requisition_id"],
        ["org_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_hire_jobs_requisition",
        "hire_jobs",
        ["org_id", "requisition_id"],
        unique=True,
        postgresql_where=sa.text("requisition_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_hire_jobs_requisition", table_name="hire_jobs")
    op.drop_constraint("fk_hire_jobs_requisition_org", "hire_jobs", type_="foreignkey")
    op.drop_column("hire_jobs", "requisition_id")
    op.drop_table(_T)
