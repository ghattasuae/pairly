# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire pre-employment checklist (M3, brief 3.8.4).

Tables (``pairly_hire.onboarding.models``), tenant tables, RLS forced, standard tenant
policy, the application role gets SELECT, INSERT and UPDATE (never DELETE). A
candidate's portal session never sees the configs, and only its own items (to attach
an upload):

* ``hire_checklist_configs``: per job, the documents to collect and the retention days.
* ``hire_checklist_items``: one document for one application; the file is stored
  encrypted (object key and encrypted name here).

``hire_document_requests`` gains ``checklist_item_id``: a checklist document is asked
for through the portal's file requests, and a received one may have no shared document
(its file lives encrypted on the item).

Revision ID: hire_0015
Revises: hire_0014
Create Date: 2026-10-13
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0015"
down_revision: str | None = "hire_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("hire_checklist_configs", "hire_checklist_items")
_REQ = "hire_document_requests"
_OLD = "status <> 'received' OR document_id IS NOT NULL"
_NEW = "status <> 'received' OR document_id IS NOT NULL OR checklist_item_id IS NOT NULL"


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


def _user(table: str, column: str, *, nullable: bool) -> sa.Column[Any]:
    return sa.Column(
        column,
        sa.Uuid(),
        sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{table}_{column}_users"),
        nullable=nullable,
    )


def _ts(name: str, *, nullable: bool = True) -> sa.Column[Any]:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    t = "hire_checklist_configs"
    op.create_table(
        t,
        *_base(t),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="90"),
        _user(t, "updated_by", nullable=False),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("org_id", "job_id", name="uq_hire_checklist_configs_job"),
        sa.ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_checklist_configs_job_org",
        ),
        sa.CheckConstraint("jsonb_typeof(items) = 'array'", name=f"ck_{t}_items_array"),
        sa.CheckConstraint("retention_days BETWEEN 1 AND 365", name=f"ck_{t}_retention_days_range"),
    )

    t = "hire_checklist_items"
    op.create_table(
        t,
        *_base(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(32), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("label_ar", sa.String(120), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="requested"),
        sa.Column("document_request_id", sa.Uuid(), nullable=True),
        sa.Column("object_key", sa.String(200), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="90"),
        _user(t, "requested_by", nullable=False),
        _ts("received_at"),
        _user(t, "reviewed_by", nullable=True),
        _ts("reviewed_at"),
        _ts("retention_until"),
        _ts("purged_at"),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("org_id", "id", name="uq_hire_checklist_items_org_id_id"),
        sa.UniqueConstraint("org_id", "application_id", "key", name="uq_hire_checklist_items_key"),
        sa.ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_checklist_items_application_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "candidate_id"],
            ["hire_candidates.org_id", "hire_candidates.id"],
            ondelete="RESTRICT",
            name="fk_hire_checklist_items_candidate_org",
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'received', 'verified', 'waived')",
            name=f"ck_{t}_status_valid",
        ),
        sa.CheckConstraint("key ~ '^[a-z][a-z0-9_]{0,31}$'", name=f"ck_{t}_key_format"),
        sa.CheckConstraint(
            "status NOT IN ('received', 'verified') OR object_key IS NOT NULL "
            "OR purged_at IS NOT NULL",
            name=f"ck_{t}_received_has_file",
        ),
        sa.CheckConstraint("retention_days BETWEEN 1 AND 365", name=f"ck_{t}_retention_days_range"),
    )

    role = app_db_role()
    for table in _TABLES:
        op.create_index(f"ix_{table}_org_id", table, ["org_id"])
        for stmt in org_rls_statements(table, role, privileges="SELECT, INSERT, UPDATE"):
            op.execute(stmt)
    op.execute(
        "CREATE POLICY hire_checklist_configs_portal_subject ON hire_checklist_configs "
        "AS RESTRICTIVE USING (NOT hire_portal_active()) WITH CHECK (NOT hire_portal_active())"
    )
    # A candidate's portal session reaches their own items only (to attach an upload).
    own = "NOT hire_portal_active() OR candidate_id = hire_portal_candidate()"
    op.execute(
        "CREATE POLICY hire_checklist_items_portal_subject ON hire_checklist_items "
        f"AS RESTRICTIVE USING ({own}) WITH CHECK ({own})"
    )

    op.add_column(_REQ, sa.Column("checklist_item_id", sa.Uuid(), nullable=True))
    op.drop_constraint(f"ck_{_REQ}_received_has_document", _REQ, type_="check")
    op.create_check_constraint(f"ck_{_REQ}_received_has_document", _REQ, _NEW)


def downgrade() -> None:
    op.drop_constraint(f"ck_{_REQ}_received_has_document", _REQ, type_="check")
    op.create_check_constraint(f"ck_{_REQ}_received_has_document", _REQ, _OLD)
    op.drop_column(_REQ, "checklist_item_id")
    for table in reversed(_TABLES):
        op.drop_table(table)
