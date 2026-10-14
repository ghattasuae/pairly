# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: closing and cancelling purchase orders.

``pay_purchase_orders`` gains when a person closed or cancelled the PO, who and why
(``closed_at``, ``closed_by_type``, ``closed_by_id``, ``close_reason``). A PO that is not
open must say when and by whom. Existing closed or cancelled rows (none are made by the
application before this revision) get the creation time and creator.

Revision ID: pay_0011
Revises: pay_0010
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "pay_0011"
down_revision: str | None = "pay_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "pay_purchase_orders"


def upgrade() -> None:
    op.add_column(T, sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(T, sa.Column("closed_by_type", sa.String(16), nullable=True))
    op.add_column(T, sa.Column("closed_by_id", sa.String(128), nullable=True))
    op.add_column(T, sa.Column("close_reason", sa.String(500), nullable=True))
    op.execute("ALTER TABLE pay_purchase_orders NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE pay_purchase_orders SET closed_at = created_at, "
        "closed_by_type = created_by_type, closed_by_id = created_by_id "
        "WHERE status <> 'open' AND closed_at IS NULL"
    )
    op.execute("ALTER TABLE pay_purchase_orders FORCE ROW LEVEL SECURITY")
    # The naming convention makes it ck_pay_purchase_orders_closed_has_evidence.
    op.create_check_constraint(
        "closed_has_evidence",
        T,
        "status = 'open' OR (closed_at IS NOT NULL AND closed_by_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("closed_has_evidence", T, type_="check")
    op.drop_column(T, "close_reason")
    op.drop_column(T, "closed_by_id")
    op.drop_column(T, "closed_by_type")
    op.drop_column(T, "closed_at")
