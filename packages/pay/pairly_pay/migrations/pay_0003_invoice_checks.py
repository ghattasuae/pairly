# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: invoice validation and fraud checks (WP4.6).

``pay_invoices`` gains what later invoices are compared with (``number_key``: the
invoice number without separators; ``lines_key``: a digest of the line items;
``fingerprint``: the template fingerprint of the document), the result of every check
(``checks``), the risk score (``risk``) and when the checks ran (``checked_at``), plus
an index for the duplicate lookups. ``pay_settings`` gains ``check_settings`` (empty:
the defaults of ``pairly_pay.checks.settings``). No new table, no grant change: the
application role already has SELECT, INSERT and UPDATE on both tables, under RLS.

Revision ID: pay_0003
Revises: pay_0002
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "pay_0003"
down_revision: str | None = "pay_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INVOICES = "pay_invoices"
SETTINGS = "pay_settings"


def upgrade() -> None:
    op.add_column(INVOICES, sa.Column("number_key", sa.String(64), nullable=True))
    op.add_column(INVOICES, sa.Column("lines_key", sa.String(64), nullable=True))
    op.add_column(INVOICES, sa.Column("fingerprint", postgresql.JSONB(), nullable=True))
    op.add_column(
        INVOICES,
        sa.Column(
            "checks", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    op.add_column(
        INVOICES,
        sa.Column(
            "risk", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )
    op.add_column(INVOICES, sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_pay_invoices_number_key", INVOICES, ["org_id", "vendor_id", "number_key"])
    op.add_column(
        SETTINGS,
        sa.Column(
            "check_settings",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column(SETTINGS, "check_settings")
    op.drop_index("ix_pay_invoices_number_key", table_name=INVOICES)
    for column in ("checked_at", "risk", "checks", "fingerprint", "lines_key", "number_key"):
        op.drop_column(INVOICES, column)
