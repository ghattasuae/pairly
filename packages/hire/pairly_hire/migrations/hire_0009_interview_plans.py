# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire interview plans (M3, brief 3.1.5).

``hire_interview_plans``: one per job, the plan as JSON and its version
(``pairly_hire.interviewing.models``). Tenant table, RLS forced, standard tenant policy;
the application role gets SELECT, INSERT and UPDATE, never DELETE. Hidden from a
candidate's portal session. Points at its job by ``(org_id, id)``.

Revision ID: hire_0009
Revises: hire_0008
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0009"
down_revision: str | None = "hire_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_T = "hire_interview_plans"


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
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{_T}_updated_by_users"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("org_id", "job_id", name="uq_hire_interview_plans_job"),
        sa.ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_interview_plans_job_org",
        ),
        sa.CheckConstraint("jsonb_typeof(plan) = 'object'", name=f"ck_{_T}_plan_object"),
        sa.CheckConstraint("version >= 1", name=f"ck_{_T}_version_positive"),
    )
    op.create_index(f"ix_{_T}_org_id", _T, ["org_id"])
    for stmt in org_rls_statements(_T, app_db_role(), privileges="SELECT, INSERT, UPDATE"):
        op.execute(stmt)
    op.execute(
        f"CREATE POLICY {_T}_portal_subject ON {_T} AS RESTRICTIVE "
        "USING (NOT hire_portal_active()) WITH CHECK (NOT hire_portal_active())"
    )


def downgrade() -> None:
    op.drop_table(_T)
