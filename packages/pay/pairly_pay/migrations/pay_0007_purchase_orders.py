# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: purchase orders and goods receipts (brief 4.5.1 and 4.5.2).

Tables (see ``pairly_pay.models``), tenant tables with RLS forced and the standard
tenant policy. The application role gets SELECT and INSERT on the lines and receipts
(they are never edited) and SELECT, INSERT and UPDATE on the purchase orders (their
status); never DELETE:

* ``pay_purchase_orders``: one per number (compared without case or separators), the
  vendor, the currency, 2-way or 3-way matching, the status.
* ``pay_purchase_order_lines``: description, quantity, unit price, line total.
* ``pay_goods_receipts`` and ``pay_goods_receipt_lines``: what arrived, per PO line.

``pay_invoices`` gains the PO it cites when it is on file (``po_id``) and how its lines
were allocated to the PO lines (``po_match``).

Revision ID: pay_0007
Revises: pay_0006
Create Date: 2026-10-14
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "pay_0007"
down_revision: str | None = "pay_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POS = "pay_purchase_orders"
PO_LINES = "pay_purchase_order_lines"
RECEIPTS = "pay_goods_receipts"
RECEIPT_LINES = "pay_goods_receipt_lines"
INVOICES = "pay_invoices"


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name=f"fk_{table}_org_id_organisations"
            ),
            nullable=False,
        ),
    ]


def _created() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _org_fk(table: str, column: str, parent: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id", column],
        [f"{parent}.org_id", f"{parent}.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_{name}_org",
    )


def _who() -> list[Any]:
    return [
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.String(128), nullable=False),
    ]


def _pos() -> None:
    t = POS
    op.create_table(
        t,
        *_base(t),
        sa.Column("po_number", sa.String(64), nullable=False),
        sa.Column("number_key", sa.String(64), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("match_type", sa.String(8), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("order_date", sa.Date(), nullable=False),
        sa.Column("department", sa.String(64), nullable=True),
        sa.Column("requester", sa.String(120), nullable=True),
        sa.Column("subtotal", sa.Numeric(20, 4), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        *_who(),
        _created(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_purchase_orders_org_id_id"),
        sa.UniqueConstraint(
            "org_id", "number_key", name="uq_pay_purchase_orders_org_id_number_key"
        ),
        _org_fk(t, "vendor_id", "pay_vendors", "vendor"),
        sa.CheckConstraint(
            "status IN ('open', 'closed', 'cancelled')", name=f"ck_{t}_status_valid"
        ),
        sa.CheckConstraint("match_type IN ('2-way', '3-way')", name=f"ck_{t}_match_type_valid"),
        sa.CheckConstraint("source IN ('api', 'csv')", name=f"ck_{t}_source_valid"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=f"ck_{t}_currency_format"),
        sa.CheckConstraint("subtotal >= 0", name=f"ck_{t}_subtotal_not_negative"),
        sa.CheckConstraint("line_count > 0", name=f"ck_{t}_has_lines"),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_purchase_orders_vendor", t, ["org_id", "vendor_id", "created_at"])


def _po_lines() -> None:
    t = PO_LINES
    op.create_table(
        t,
        *_base(t),
        sa.Column("po_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 4), nullable=False),
        sa.Column("unit_price", sa.Numeric(20, 4), nullable=False),
        sa.Column("line_total", sa.Numeric(20, 4), nullable=False),
        _created(),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_purchase_order_lines_org_id_id"),
        sa.UniqueConstraint("org_id", "po_id", "line_no", name="uq_pay_purchase_order_lines_line"),
        _org_fk(t, "po_id", POS, "po"),
        sa.CheckConstraint("line_no > 0", name=f"ck_{t}_line_no_positive"),
        sa.CheckConstraint("quantity > 0", name=f"ck_{t}_quantity_positive"),
        sa.CheckConstraint("unit_price >= 0", name=f"ck_{t}_unit_price_not_negative"),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])


def _receipts() -> None:
    t = RECEIPTS
    op.create_table(
        t,
        *_base(t),
        sa.Column("grn_number", sa.String(64), nullable=False),
        sa.Column("number_key", sa.String(64), nullable=False),
        sa.Column("po_id", sa.Uuid(), nullable=False),
        sa.Column("received_date", sa.Date(), nullable=False),
        sa.Column("received_by", sa.String(120), nullable=True),
        sa.Column("line_count", sa.Integer(), nullable=False),
        *_who(),
        _created(),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_goods_receipts_org_id_id"),
        sa.UniqueConstraint("org_id", "number_key", name="uq_pay_goods_receipts_org_id_number_key"),
        _org_fk(t, "po_id", POS, "po"),
        sa.CheckConstraint("source IN ('api', 'csv')", name=f"ck_{t}_source_valid"),
        sa.CheckConstraint("line_count > 0", name=f"ck_{t}_has_lines"),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_goods_receipts_po", t, ["org_id", "po_id", "received_date"])


def _receipt_lines() -> None:
    t = RECEIPT_LINES
    op.create_table(
        t,
        *_base(t),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("po_id", sa.Uuid(), nullable=False),
        sa.Column("po_line_no", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 4), nullable=False),
        _created(),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_goods_receipt_lines_org_id_id"),
        sa.UniqueConstraint(
            "org_id", "receipt_id", "po_line_no", name="uq_pay_goods_receipt_lines_line"
        ),
        _org_fk(t, "receipt_id", RECEIPTS, "receipt"),
        _org_fk(t, "po_id", POS, "po"),
        sa.CheckConstraint("po_line_no > 0", name=f"ck_{t}_po_line_no_positive"),
        sa.CheckConstraint("quantity > 0", name=f"ck_{t}_quantity_positive"),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_goods_receipt_lines_po", t, ["org_id", "po_id"])


def upgrade() -> None:
    app_role = app_db_role()
    _pos()
    _po_lines()
    _receipts()
    _receipt_lines()
    op.add_column(INVOICES, sa.Column("po_id", sa.Uuid(), nullable=True))
    op.add_column(
        INVOICES,
        sa.Column(
            "po_match", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )
    op.create_foreign_key(
        "fk_pay_invoices_po_org",
        INVOICES,
        POS,
        ["org_id", "po_id"],
        ["org_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_pay_invoices_po", INVOICES, ["org_id", "po_id", "created_at"])
    for stmt in org_rls_statements(POS, app_role, privileges="SELECT, INSERT, UPDATE"):
        op.execute(stmt)
    for table in (PO_LINES, RECEIPTS, RECEIPT_LINES):
        for stmt in org_rls_statements(table, app_role, privileges="SELECT, INSERT"):
            op.execute(stmt)


def downgrade() -> None:
    op.drop_index("ix_pay_invoices_po", table_name=INVOICES)
    op.drop_constraint("fk_pay_invoices_po_org", INVOICES, type_="foreignkey")
    op.drop_column(INVOICES, "po_match")
    op.drop_column(INVOICES, "po_id")
    op.drop_table(RECEIPT_LINES)
    op.drop_table(RECEIPTS)
    op.drop_table(PO_LINES)
    op.drop_table(POS)
