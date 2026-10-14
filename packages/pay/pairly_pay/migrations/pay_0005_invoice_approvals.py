# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: invoice approvals (WP4.7).

``pay_invoices``:

* a new status ``awaiting_approval`` (the column widens to 32 characters);
* who sent the invoice for approval and when, the approval run and the engine approval;
* what was approved: when, by whom (``approver_ids``) or by a rule
  (``approved_by_rule``), the amount and currency, the vendor's bank account and the
  keyed digest of its details, and the digest of the checks. A constraint refuses an
  ``approved``, ``scheduled`` or ``paid`` row without that evidence.

``pay_settings`` gains ``payment_settings`` (``pairly_pay.payment_settings``; empty: the
defaults, with approval by a rule off). No new table, no grant change.

Revision ID: pay_0005
Revises: pay_0004
Create Date: 2026-10-14
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "pay_0005"
down_revision: str | None = "pay_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INVOICES = "pay_invoices"
SETTINGS = "pay_settings"
STATUSES_V1 = (
    "'received', 'extracting', 'needs_review', 'ready', 'approved', 'scheduled', 'paid', 'rejected'"
)
STATUSES_V2 = (
    "'received', 'extracting', 'needs_review', 'ready', 'awaiting_approval', 'approved', "
    "'scheduled', 'paid', 'rejected'"
)


def _user(name: str) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.Uuid(),
        sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{INVOICES}_{name}_users"),
        nullable=True,
    )


def upgrade() -> None:
    op.alter_column(INVOICES, "status", type_=sa.String(32), existing_nullable=False)
    op.drop_constraint("ck_pay_invoices_status_valid", INVOICES, type_="check")
    op.create_check_constraint(
        "ck_pay_invoices_status_valid", INVOICES, f"status IN ({STATUSES_V2})"
    )
    op.add_column(INVOICES, _user("approval_submitted_by"))
    op.add_column(
        INVOICES, sa.Column("approval_submitted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        INVOICES,
        sa.Column(
            "approval_run_id",
            sa.Uuid(),
            sa.ForeignKey(
                "workflow_runs.id",
                ondelete="RESTRICT",
                name="fk_pay_invoices_approval_run_id_workflow_runs",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        INVOICES,
        sa.Column(
            "approval_id",
            sa.Uuid(),
            sa.ForeignKey(
                "approvals.id", ondelete="RESTRICT", name="fk_pay_invoices_approval_id_approvals"
            ),
            nullable=True,
        ),
    )
    op.add_column(INVOICES, sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        INVOICES,
        sa.Column("approved_by_rule", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        INVOICES,
        sa.Column(
            "approver_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(INVOICES, sa.Column("approved_amount", sa.Numeric(20, 4), nullable=True))
    op.add_column(INVOICES, sa.Column("approved_currency", sa.String(3), nullable=True))
    op.add_column(INVOICES, sa.Column("approved_account_id", sa.Uuid(), nullable=True))
    op.add_column(INVOICES, sa.Column("approved_bank_digest", sa.String(64), nullable=True))
    op.add_column(INVOICES, sa.Column("approved_checks_digest", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_pay_invoices_approved_account_org",
        INVOICES,
        "pay_vendor_bank_accounts",
        ["org_id", "approved_account_id"],
        ["org_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_pay_invoices_approved_has_evidence",
        INVOICES,
        "status NOT IN ('approved', 'scheduled', 'paid') OR (approved_at IS NOT NULL "
        "AND approved_account_id IS NOT NULL AND approved_bank_digest IS NOT NULL "
        "AND approved_checks_digest IS NOT NULL AND approved_amount IS NOT NULL)",
    )
    op.add_column(
        SETTINGS,
        sa.Column(
            "payment_settings",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column(SETTINGS, "payment_settings")
    op.drop_constraint("ck_pay_invoices_approved_has_evidence", INVOICES, type_="check")
    op.drop_constraint("fk_pay_invoices_approved_account_org", INVOICES, type_="foreignkey")
    for column in (
        "approved_checks_digest",
        "approved_bank_digest",
        "approved_account_id",
        "approved_currency",
        "approved_amount",
        "approver_ids",
        "approved_by_rule",
        "approved_at",
        "approval_id",
        "approval_run_id",
        "approval_submitted_at",
        "approval_submitted_by",
    ):
        op.drop_column(INVOICES, column)
    op.drop_constraint("ck_pay_invoices_status_valid", INVOICES, type_="check")
    op.create_check_constraint(
        "ck_pay_invoices_status_valid", INVOICES, f"status IN ({STATUSES_V1})"
    )
    op.alter_column(INVOICES, "status", type_=sa.String(16), existing_nullable=False)
