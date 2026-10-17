# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Document batches for the batch API (M6).

``document_batches`` and ``document_batch_items`` are org scoped with Row Level
Security forced and closed to portal sessions. The application role may SELECT,
INSERT and UPDATE (outcomes, completion), never DELETE.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-16
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements, portal_guard_statements

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _org_fk(table: str) -> sa.Column[uuid.UUID]:
    return sa.Column(
        "org_id",
        sa.Uuid(),
        sa.ForeignKey(
            "organisations.id", ondelete="RESTRICT", name=f"fk_{table}_org_id_organisations"
        ),
        nullable=False,
    )


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    role = app_db_role()
    op.create_table(
        "document_batches",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _org_fk("document_batches"),
        sa.Column("workflow", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.String(64), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.CheckConstraint(
            "status IN ('processing', 'completed')", name="ck_document_batches_status_valid"
        ),
        sa.CheckConstraint(
            "workflow ~ '^[a-z][a-z0-9_]{0,31}$'", name="ck_document_batches_workflow_format"
        ),
        sa.CheckConstraint(
            "item_count BETWEEN 1 AND 100", name="ck_document_batches_item_count_range"
        ),
        sa.CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)",
            name="ck_document_batches_completed_pair",
        ),
        sa.CheckConstraint(
            "created_by_type IN ('user', 'agent', 'api_key')",
            name="ck_document_batches_created_by_type_valid",
        ),
    )
    op.create_index("ix_document_batches_org_id", "document_batches", ["org_id"])

    op.create_table(
        "document_batch_items",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _org_fk("document_batch_items"),
        sa.Column(
            "batch_id",
            sa.Uuid(),
            sa.ForeignKey(
                "document_batches.id",
                ondelete="RESTRICT",
                name="fk_document_batch_items_batch_id_document_batches",
            ),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey(
                "documents.id",
                ondelete="RESTRICT",
                name="fk_document_batch_items_document_id_documents",
            ),
            nullable=True,
        ),
        sa.Column("entity_type", sa.String(64), nullable=True),
        sa.Column("entity_id", sa.String(128), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("batch_id", "position", name="uq_document_batch_items_batch_position"),
        sa.CheckConstraint(
            "source IN ('file', 'url')", name="ck_document_batch_items_source_valid"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'duplicate', 'rejected', 'failed')",
            name="ck_document_batch_items_status_valid",
        ),
        sa.CheckConstraint(
            "position BETWEEN 0 AND 99", name="ck_document_batch_items_position_range"
        ),
        sa.CheckConstraint(
            "(source = 'url') = (url IS NOT NULL)", name="ck_document_batch_items_url_only_for_urls"
        ),
        sa.CheckConstraint(
            "url IS NULL OR char_length(url) <= 2048", name="ck_document_batch_items_url_length"
        ),
        sa.CheckConstraint(
            "(status = 'pending') = (completed_at IS NULL)",
            name="ck_document_batch_items_completed_unless_pending",
        ),
    )
    op.create_index("ix_document_batch_items_org_id", "document_batch_items", ["org_id"])
    op.create_index("ix_document_batch_items_batch_id", "document_batch_items", ["batch_id"])

    for table in ("document_batches", "document_batch_items"):
        for statement in org_rls_statements(table, role, privileges="SELECT, INSERT, UPDATE"):
            op.execute(statement)
        for statement in portal_guard_statements(table):
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("document_batch_items")
    op.drop_table("document_batches")
