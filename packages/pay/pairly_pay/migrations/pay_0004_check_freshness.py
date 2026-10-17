# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: check freshness and invoice coding.

``pay_invoices`` gains ``checks_digest``: the SHA-256 of the inputs the checks ran on
(``pairly_pay.freshness``). An invoice that now hashes differently has stale results and
can never be approved or paid until the checks run again. Existing rows get NULL, so
every invoice checked before this migration counts as stale (fail closed).

It also gains the coding a person sets, which the approval rules route on:
``department`` and ``category``. No new table, no grant change.

Revision ID: pay_0004
Revises: pay_0003
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "pay_0004"
down_revision: str | None = "pay_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INVOICES = "pay_invoices"


def upgrade() -> None:
    op.add_column(INVOICES, sa.Column("checks_digest", sa.String(64), nullable=True))
    op.add_column(INVOICES, sa.Column("department", sa.String(64), nullable=True))
    op.add_column(INVOICES, sa.Column("category", sa.String(64), nullable=True))
    op.create_check_constraint(
        "ck_pay_invoices_checks_digest_format",
        INVOICES,
        "checks_digest IS NULL OR checks_digest ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_pay_invoices_checks_digest_format", INVOICES, type_="check")
    for column in ("category", "department", "checks_digest"):
        op.drop_column(INVOICES, column)
