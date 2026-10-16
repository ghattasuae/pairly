# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire recruiter assignment.

``hire_jobs`` gains ``owner_id`` (the owner recruiter, a user; backfilled from
``created_by``) and ``co_recruiter_ids`` (optional co-recruiters, an array of user ids,
empty by default). Candidate replies, uploads and SLA alerts go to the owner and the
co-recruiters (``pairly_hire.recruiters``). The application layer checks every id is a
member of the organisation who can read candidates.

Revision ID: hire_0006
Revises: hire_0005
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "hire_0006"
down_revision: str | None = "hire_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "hire_jobs",
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name="fk_hire_jobs_owner_id_users"),
            nullable=True,
        ),
    )
    op.add_column(
        "hire_jobs",
        sa.Column(
            "co_recruiter_ids",
            postgresql.ARRAY(sa.Uuid()),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
    )
    op.create_check_constraint(
        "ck_hire_jobs_co_recruiters_limit",
        "hire_jobs",
        "cardinality(co_recruiter_ids) <= 10",
    )
    op.execute("UPDATE hire_jobs SET owner_id = created_by WHERE owner_id IS NULL")


def downgrade() -> None:
    op.drop_constraint("ck_hire_jobs_co_recruiters_limit", "hire_jobs", type_="check")
    op.drop_column("hire_jobs", "co_recruiter_ids")
    op.drop_column("hire_jobs", "owner_id")
