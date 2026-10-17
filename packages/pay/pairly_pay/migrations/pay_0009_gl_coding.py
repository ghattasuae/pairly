# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: GL coding and cost centres (brief 4.5.3).

Tables (see ``pairly_pay.models``), tenant tables with RLS forced and the standard
tenant policy, never DELETE:

* ``pay_coding_lists``: one version of the chart of accounts or of the cost centres
  (SELECT, INSERT: a change is a new version).
* ``pay_coding_entries``: the codes of a version (SELECT, INSERT).
* ``pay_invoice_line_codings``: the GL account and cost centre of an invoice line
  (SELECT, INSERT, UPDATE: a person may code a line again).
* ``pay_coding_corrections``: a person chose another code than the suggestion
  (SELECT, INSERT).

Revision ID: pay_0009
Revises: pay_0008
Create Date: 2026-10-14
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "pay_0009"
down_revision: str | None = "pay_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LISTS = "pay_coding_lists"
ENTRIES = "pay_coding_entries"
CODINGS = "pay_invoice_line_codings"
CORRECTIONS = "pay_coding_corrections"
KINDS = "'gl_account', 'cost_centre'"


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


def _now(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _org_fk(table: str, column: str, parent: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id", column],
        [f"{parent}.org_id", f"{parent}.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_{name}_org",
    )


def _lists() -> None:
    t = LISTS
    op.create_table(
        t,
        *_base(t),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.String(128), nullable=False),
        _now("created_at"),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_coding_lists_org_id_id"),
        sa.UniqueConstraint("org_id", "kind", "version", name="uq_pay_coding_lists_version"),
        sa.CheckConstraint(f"kind IN ({KINDS})", name=f"ck_{t}_kind_valid"),
        sa.CheckConstraint("source IN ('api', 'csv')", name=f"ck_{t}_source_valid"),
        sa.CheckConstraint("version > 0", name=f"ck_{t}_version_positive"),
        sa.CheckConstraint("entry_count >= 0", name=f"ck_{t}_entry_count_not_negative"),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])


def _entries() -> None:
    t = ENTRIES
    op.create_table(
        t,
        *_base(t),
        sa.Column("list_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("name_ar", sa.String(160), nullable=True),
        _now("created_at"),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_coding_entries_org_id_id"),
        sa.UniqueConstraint("org_id", "list_id", "code", name="uq_pay_coding_entries_code"),
        _org_fk(t, "list_id", LISTS, "list"),
        sa.CheckConstraint(f"kind IN ({KINDS})", name=f"ck_{t}_kind_valid"),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])


def _codings() -> None:
    t = CODINGS
    op.create_table(
        t,
        *_base(t),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("line_index", sa.Integer(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("gl_code", sa.String(32), nullable=True),
        sa.Column("cost_centre", sa.String(32), nullable=True),
        sa.Column("gl_list_id", sa.Uuid(), nullable=True),
        sa.Column("cc_list_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column(
            "suggestion", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("corrected", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("coded_by_type", sa.String(16), nullable=False),
        sa.Column("coded_by_id", sa.String(128), nullable=False),
        _now("coded_at"),
        _now("created_at"),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_invoice_line_codings_org_id_id"),
        sa.UniqueConstraint(
            "org_id", "invoice_id", "line_index", name="uq_pay_invoice_line_codings_line"
        ),
        _org_fk(t, "invoice_id", "pay_invoices", "invoice"),
        _org_fk(t, "gl_list_id", LISTS, "gl_list"),
        _org_fk(t, "cc_list_id", LISTS, "cc_list"),
        sa.CheckConstraint("source IN ('person', 'auto')", name=f"ck_{t}_source_valid"),
        sa.CheckConstraint(
            "line_index >= 0 AND line_index < 1000", name=f"ck_{t}_line_index_range"
        ),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_invoice_line_codings_vendor", t, ["org_id", "vendor_id", "coded_at"])


def _corrections() -> None:
    t = CORRECTIONS
    op.create_table(
        t,
        *_base(t),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("line_index", sa.Integer(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("suggested", postgresql.JSONB(), nullable=False),
        sa.Column("chosen_gl", sa.String(32), nullable=True),
        sa.Column("chosen_cost_centre", sa.String(32), nullable=True),
        sa.Column(
            "corrected_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{t}_corrected_by_users"),
            nullable=False,
        ),
        _now("created_at"),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_coding_corrections_org_id_id"),
        _org_fk(t, "invoice_id", "pay_invoices", "invoice"),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_coding_corrections_invoice", t, ["org_id", "invoice_id", "created_at"])


def upgrade() -> None:
    app_role = app_db_role()
    _lists()
    _entries()
    _codings()
    _corrections()
    for table in (LISTS, ENTRIES, CORRECTIONS):
        for stmt in org_rls_statements(table, app_role, privileges="SELECT, INSERT"):
            op.execute(stmt)
    for stmt in org_rls_statements(CODINGS, app_role, privileges="SELECT, INSERT, UPDATE"):
        op.execute(stmt)


def downgrade() -> None:
    op.drop_table(CORRECTIONS)
    op.drop_table(CODINGS)
    op.drop_table(ENTRIES)
    op.drop_table(LISTS)
