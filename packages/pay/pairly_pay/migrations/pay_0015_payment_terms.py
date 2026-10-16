# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: payment terms on invoices and vendors (early-payment discounts).

``pay_invoices.payment_terms``: the terms printed on the invoice, as read.
``pay_vendors.payment_terms``: the vendor's agreed terms as AP records them, used when
an invoice prints none. ``pairly_pay.discounts`` reads a discount from either (for
example "2/10 net 30"). ``pay_payment_run_items.discount`` (until now a placeholder)
holds the discount a run takes.

Revision ID: pay_0015
Revises: pay_0014
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "pay_0015"
down_revision: str | None = "pay_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pay_invoices", sa.Column("payment_terms", sa.String(120), nullable=True))
    op.add_column("pay_vendors", sa.Column("payment_terms", sa.String(120), nullable=True))
    # The naming convention prefixes ``ck_<table>_``.
    op.create_check_constraint(
        "discount_not_negative",
        "pay_payment_run_items",
        "discount IS NULL OR discount >= 0",
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE pay_payment_run_items "
        "DROP CONSTRAINT ck_pay_payment_run_items_discount_not_negative"
    )
    op.drop_column("pay_vendors", "payment_terms")
    op.drop_column("pay_invoices", "payment_terms")
