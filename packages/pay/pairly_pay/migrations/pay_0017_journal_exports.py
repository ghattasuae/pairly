# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: a record of every journal export (``pairly_pay.journal``).

``pay_journal_exports``: the period, the format (``generic`` for now), the file's SHA-256,
its entry and line counts, who downloaded it and when. The file itself is not kept.
Tenant table with RLS forced; the application role gets SELECT and INSERT only (a
record is never changed). Closed to portal sessions.

Revision ID: pay_0017
Revises: pay_0016
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import (
    app_db_role,
    drop_portal_guard_statements,
    org_rls_statements,
    portal_guard_statements,
)

revision: str = "pay_0017"
down_revision: str | None = "pay_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "pay_journal_exports"


def upgrade() -> None:
    op.create_table(
        T,
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name=f"fk_{T}_org_id_organisations"
            ),
            nullable=False,
        ),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column(
            "exported_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{T}_exported_by_users"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("org_id", "id", name=f"uq_{T}_org_id_id"),
        sa.CheckConstraint("period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'", name=f"ck_{T}_period_format"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{T}_sha256_format"),
        sa.CheckConstraint(
            "entry_count >= 0 AND line_count >= 0", name=f"ck_{T}_counts_not_negative"
        ),
    )
    op.create_index(f"ix_{T}_org_id", T, ["org_id"])
    op.create_index(f"ix_{T}_period", T, ["org_id", "period", "created_at"])
    for stmt in org_rls_statements(T, app_db_role(), privileges="SELECT, INSERT"):
        op.execute(stmt)
    for stmt in portal_guard_statements(T):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in drop_portal_guard_statements(T):
        op.execute(stmt)
    op.drop_table(T)
