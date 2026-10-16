# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Proposals: notes and suggested actions from agents and API clients (M6).

``proposals`` is org scoped with Row Level Security forced and closed to portal
sessions. Append only: the application role may SELECT and INSERT, nothing else.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import app_db_role, org_rls_statements, portal_guard_statements

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    role = app_db_role()
    op.create_table(
        "proposals",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name="fk_proposals_org_id_organisations"
            ),
            nullable=False,
        ),
        sa.Column("workflow", sa.String(32), nullable=True),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("action", sa.String(64), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("proposed_by_type", sa.String(16), nullable=False),
        sa.Column("proposed_by_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("kind IN ('note', 'action')", name="ck_proposals_kind_valid"),
        sa.CheckConstraint(
            "(kind = 'action') = (action IS NOT NULL)", name="ck_proposals_action_only_for_actions"
        ),
        sa.CheckConstraint(
            "action IS NULL OR action ~ '^[a-z][a-z0-9_]{0,63}$'", name="ck_proposals_action_format"
        ),
        sa.CheckConstraint(
            "entity_type ~ '^[a-z][a-z0-9_]*([.][a-z][a-z0-9_]*){0,3}$' "
            "AND char_length(entity_type) <= 64",
            name="ck_proposals_entity_type_format",
        ),
        sa.CheckConstraint(
            "char_length(entity_id) BETWEEN 1 AND 128", name="ck_proposals_entity_id_length"
        ),
        sa.CheckConstraint("char_length(body) BETWEEN 1 AND 4000", name="ck_proposals_body_length"),
        sa.CheckConstraint(
            "workflow IS NULL OR workflow ~ '^[a-z][a-z0-9_]{0,31}$'",
            name="ck_proposals_workflow_format",
        ),
        sa.CheckConstraint(
            "proposed_by_type IN ('user', 'agent', 'api_key')",
            name="ck_proposals_proposed_by_type_valid",
        ),
    )
    op.create_index("ix_proposals_org_id", "proposals", ["org_id"])
    op.create_index(
        "ix_proposals_entity", "proposals", ["org_id", "entity_type", "entity_id", "created_at"]
    )
    for statement in org_rls_statements("proposals", role, privileges="SELECT, INSERT"):
        op.execute(statement)
    for statement in portal_guard_statements("proposals"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("proposals")
