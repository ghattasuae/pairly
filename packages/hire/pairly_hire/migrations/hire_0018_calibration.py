# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire calibration mode (M3, brief 3.3.11).

Table (``pairly_hire.analytics.models``), tenant table, RLS forced, standard tenant
policy, the application role gets SELECT, INSERT and UPDATE (never DELETE), hidden from
a candidate's portal session:

* ``hire_calibration_labels``: a person's 0 to 4 score for one criterion of one screened
  application, one per person, screening and criterion.

Revision ID: hire_0018
Revises: hire_0017
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0018"
down_revision: str | None = "hire_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "hire_calibration_labels"


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
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("screening_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("rubric_policy_id", sa.Uuid(), nullable=False),
        sa.Column("criterion_id", sa.String(48), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column(
            "labelled_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{t}_labelled_by_users"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "org_id",
            "screening_id",
            "criterion_id",
            "labelled_by",
            name="uq_hire_calibration_labels_person",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_calibration_labels_application_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_calibration_labels_job_org",
        ),
        sa.CheckConstraint("score BETWEEN 0 AND 4", name=f"ck_{t}_score_range"),
        sa.CheckConstraint(
            "criterion_id ~ '^[a-z][a-z0-9_]{0,47}$'", name=f"ck_{t}_criterion_format"
        ),
    )
    op.create_index("ix_hire_calibration_labels_job", t, ["org_id", "job_id"])
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    role = app_db_role()
    for stmt in org_rls_statements(t, role, privileges="SELECT, INSERT, UPDATE"):
        op.execute(stmt)
    op.execute(
        f"CREATE POLICY {t}_portal_subject ON {t} AS RESTRICTIVE "
        "USING (NOT hire_portal_active()) WITH CHECK (NOT hire_portal_active())"
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
