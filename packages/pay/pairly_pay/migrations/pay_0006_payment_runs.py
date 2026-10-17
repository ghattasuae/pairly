# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: payment runs (WP4.7, brief 4.6.3). No bank connection, no money moves.

Tables (see ``pairly_pay.models``), tenant tables with RLS forced and the standard
tenant policy; the application role gets SELECT, INSERT and UPDATE (never DELETE):

* ``pay_payment_runs``: a proposal to pay approved invoices (currency, cash limit,
  execution date), its approval, the digest of every bank file given out, and how it
  ended. Its total can never exceed its cash limit.
* ``pay_payment_run_items``: one invoice of a run with the bank account and the keyed
  digest of its details, and the check digest, it was proposed and approved with. An
  invoice is in at most one run at a time (partial unique index).

``pay_invoices`` gains the run that pays it and who marked it paid, when, with which
bank reference; constraints refuse a ``scheduled`` or ``paid`` row without a run and a
``paid`` row without who and when.

Revision ID: pay_0006
Revises: pay_0005
Create Date: 2026-10-14
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "pay_0006"
down_revision: str | None = "pay_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNS = "pay_payment_runs"
ITEMS = "pay_payment_run_items"
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


def _ts(name: str, *, nullable: bool = True) -> sa.Column[Any]:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _jsonb_list(name: str) -> sa.Column[Any]:
    return sa.Column(
        name, postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
    )


def _fk(table: str, name: str, target: str, *, nullable: bool = True) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.Uuid(),
        sa.ForeignKey(f"{target}.id", ondelete="RESTRICT", name=f"fk_{table}_{name}_{target}"),
        nullable=nullable,
    )


def _org_fk(table: str, column: str, parent: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id", column],
        [f"{parent}.org_id", f"{parent}.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_{name}_org",
    )


def _runs() -> None:
    t = RUNS
    op.create_table(
        t,
        *_base(t),
        sa.Column("reference", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("cash_limit", sa.Numeric(20, 4), nullable=False),
        sa.Column("total", sa.Numeric(20, 4), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("execution_date", sa.Date(), nullable=False),
        sa.Column("due_on_or_before", sa.Date(), nullable=True),
        _jsonb_list("excluded"),
        _fk(t, "proposed_by", "users", nullable=False),
        _fk(t, "run_id", "workflow_runs"),
        _fk(t, "approval_id", "approvals"),
        _jsonb_list("approver_ids"),
        _ts("approved_at"),
        sa.Column("export_count", sa.Integer(), nullable=False, server_default="0"),
        _jsonb_list("exports"),
        _ts("exported_at"),
        _ts("paid_at"),
        _ts("closed_at"),
        sa.Column("close_reason", sa.String(120), nullable=True),
        _jsonb_list("decision_ids"),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_payment_runs_org_id_id"),
        sa.UniqueConstraint("org_id", "reference", name="uq_pay_payment_runs_org_id_reference"),
        sa.CheckConstraint(
            "status IN ('awaiting_approval', 'approved', 'exported', 'paid', 'rejected', "
            "'cancelled', 'failed')",
            name=f"ck_{t}_status_valid",
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=f"ck_{t}_currency_format"),
        sa.CheckConstraint("cash_limit > 0", name=f"ck_{t}_cash_limit_positive"),
        sa.CheckConstraint("total >= 0 AND total <= cash_limit", name=f"ck_{t}_total_within_limit"),
        sa.CheckConstraint(
            "status NOT IN ('approved', 'exported', 'paid') OR approved_at IS NOT NULL",
            name=f"ck_{t}_approved_has_time",
        ),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_payment_runs_status", t, ["org_id", "status", "created_at"])


def _items() -> None:
    t = ITEMS
    op.create_table(
        t,
        *_base(t),
        sa.Column("payment_run_id", sa.Uuid(), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("discount", sa.Numeric(20, 4), nullable=True),
        sa.Column("bank_account_id", sa.Uuid(), nullable=False),
        sa.Column("bank_details_digest", sa.String(64), nullable=False),
        sa.Column("checks_digest", sa.String(64), nullable=False),
        sa.Column("end_to_end_id", sa.String(35), nullable=False),
        _ts("paid_at"),
        _fk(t, "paid_by", "users"),
        sa.Column("payment_reference", sa.String(64), nullable=True),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_payment_run_items_org_id_id"),
        _org_fk(t, "payment_run_id", RUNS, "run"),
        _org_fk(t, "invoice_id", INVOICES, "invoice"),
        _org_fk(t, "vendor_id", "pay_vendors", "vendor"),
        _org_fk(t, "bank_account_id", "pay_vendor_bank_accounts", "account"),
        sa.CheckConstraint(
            "status IN ('included', 'paid', 'released')", name=f"ck_{t}_status_valid"
        ),
        sa.CheckConstraint("amount > 0", name=f"ck_{t}_amount_positive"),
        sa.CheckConstraint(
            "status <> 'paid' OR (paid_at IS NOT NULL AND paid_by IS NOT NULL)",
            name=f"ck_{t}_paid_has_evidence",
        ),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_payment_run_items_run", t, ["org_id", "payment_run_id", "position"])
    op.create_index(
        "uq_pay_payment_run_items_one_active",
        t,
        ["org_id", "invoice_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'released'"),
    )


def upgrade() -> None:
    app_role = app_db_role()
    _runs()
    _items()
    op.add_column(INVOICES, sa.Column("payment_run_id", sa.Uuid(), nullable=True))
    op.add_column(INVOICES, sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(INVOICES, _fk(INVOICES, "paid_by", "users"))
    op.add_column(INVOICES, sa.Column("payment_reference", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_pay_invoices_payment_run_org",
        INVOICES,
        RUNS,
        ["org_id", "payment_run_id"],
        ["org_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_pay_invoices_scheduled_has_run",
        INVOICES,
        "status NOT IN ('scheduled', 'paid') OR payment_run_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_pay_invoices_paid_has_evidence",
        INVOICES,
        "status <> 'paid' OR (paid_at IS NOT NULL AND paid_by IS NOT NULL)",
    )
    for table in (RUNS, ITEMS):
        for stmt in org_rls_statements(table, app_role, privileges="SELECT, INSERT, UPDATE"):
            op.execute(stmt)


def downgrade() -> None:
    op.drop_constraint("ck_pay_invoices_paid_has_evidence", INVOICES, type_="check")
    op.drop_constraint("ck_pay_invoices_scheduled_has_run", INVOICES, type_="check")
    op.drop_constraint("fk_pay_invoices_payment_run_org", INVOICES, type_="foreignkey")
    for column in ("payment_reference", "paid_by", "paid_at", "payment_run_id"):
        op.drop_column(INVOICES, column)
    op.drop_table(ITEMS)
    op.drop_table(RUNS)
