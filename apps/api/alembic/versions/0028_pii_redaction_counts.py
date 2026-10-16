# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""PII redaction counts (M6, the AI governance board).

``pii_redaction_counts``: how many personal data values were redacted from logs and
traces, one row per organisation and UTC day (``pairly_core.guardrails.redaction_counts``).
Numbers only. Org scoped with Row Level Security forced and closed to portal sessions.
The application role may SELECT, INSERT and UPDATE (the flush adds to the day's row),
never DELETE.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import app_db_role, org_rls_statements, portal_guard_statements

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "pii_redaction_counts"


def upgrade() -> None:
    role = app_db_role()
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name=f"fk_{TABLE}_org_id_organisations"
            ),
            nullable=False,
        ),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("redactions", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("org_id", "day", name="uq_pii_redaction_counts_org_day"),
        sa.CheckConstraint("redactions >= 0", name="ck_pii_redaction_counts_not_negative"),
    )
    op.create_index("ix_pii_redaction_counts_org_id", TABLE, ["org_id"])
    for statement in org_rls_statements(TABLE, role, privileges="SELECT, INSERT, UPDATE"):
        op.execute(statement)
    for statement in portal_guard_statements(TABLE):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table(TABLE)
