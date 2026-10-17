# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: drafted messages about invoice exceptions (brief 4.5.4).

``pay_messages`` (see ``pairly_pay.models``), a tenant table with RLS forced and the
standard tenant policy; the application role gets SELECT, INSERT and UPDATE (its status),
never DELETE. The recipient's name and address and the message text are encrypted. A
message is sent only after a person approved its exact text (``pay.message`` approval
and the engine's sensitive template ``pay.vendor.message``).

Revision ID: pay_0010
Revises: pay_0009
Create Date: 2026-10-14
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "pay_0010"
down_revision: str | None = "pay_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "pay_messages"
STATUSES = "'submitted', 'awaiting_approval', 'queued', 'declined', 'changes_requested', 'failed'"


def _fk(name: str, target: str, *, nullable: bool = True) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.Uuid(),
        sa.ForeignKey(f"{target}.id", ondelete="RESTRICT", name=f"fk_{T}_{name}_{target}"),
        nullable=nullable,
    )


def _now(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


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
        sa.Column("reference", sa.String(16), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("template_id", sa.String(32), nullable=False),
        sa.Column("recipient_kind", sa.String(16), nullable=False),
        _fk("recipient_user_id", "users"),
        sa.Column("recipient_name", sa.Text(), nullable=False),
        sa.Column("recipient_email", sa.Text(), nullable=True),
        sa.Column("locale", sa.String(2), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("ai_note", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("model_call_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("status_reason", sa.String(64), nullable=True),
        _fk("run_id", "workflow_runs"),
        _fk("approval_id", "approvals"),
        sa.Column("notification_id", sa.Uuid(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        _fk("submitted_by", "users", nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_messages_org_id_id"),
        sa.UniqueConstraint("org_id", "reference", name="uq_pay_messages_org_id_reference"),
        sa.ForeignKeyConstraint(
            ["org_id", "invoice_id"],
            ["pay_invoices.org_id", "pay_invoices.id"],
            ondelete="RESTRICT",
            name="fk_pay_messages_invoice_org",
        ),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=f"ck_{T}_status_valid"),
        sa.CheckConstraint(
            "recipient_kind IN ('vendor', 'requester')", name=f"ck_{T}_recipient_valid"
        ),
        sa.CheckConstraint("locale IN ('en', 'ar')", name=f"ck_{T}_locale_valid"),
        sa.CheckConstraint(
            "recipient_kind <> 'requester' OR recipient_user_id IS NOT NULL",
            name=f"ck_{T}_requester_is_member",
        ),
        sa.CheckConstraint(
            "recipient_kind <> 'vendor' OR recipient_email IS NOT NULL",
            name=f"ck_{T}_vendor_has_email",
        ),
        sa.CheckConstraint(
            "status <> 'queued' OR (approval_id IS NOT NULL AND notification_id IS NOT NULL)",
            name=f"ck_{T}_queued_has_approval",
        ),
    )
    op.create_index(f"ix_{T}_org_id", T, ["org_id"])
    op.create_index("ix_pay_messages_invoice", T, ["org_id", "invoice_id", "created_at"])
    for stmt in org_rls_statements(T, app_db_role(), privileges="SELECT, INSERT, UPDATE"):
        op.execute(stmt)


def downgrade() -> None:
    op.drop_table(T)
