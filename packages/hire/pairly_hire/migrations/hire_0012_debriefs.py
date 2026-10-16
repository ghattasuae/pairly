# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire debrief summaries (M3, brief 3.7.4).

``hire_debriefs``: the summary of an application's submitted scorecards at a stage
(keyed by their hash) and the human decision taken on it, with its reason and decision
record id (``pairly_hire.interviewing.models``). Tenant table, RLS forced, standard
tenant policy; the application role gets SELECT, INSERT and UPDATE, never DELETE.
Hidden from a candidate's portal session. Points at its application by ``(org_id, id)``.

Revision ID: hire_0012
Revises: hire_0011
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0012"
down_revision: str | None = "hire_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_T = "hire_debriefs"


def upgrade() -> None:
    op.create_table(
        _T,
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name=f"fk_{_T}_org_id_organisations"
            ),
            nullable=False,
        ),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("stage_key", sa.String(32), nullable=False),
        sa.Column("scorecards_hash", sa.String(64), nullable=False),
        sa.Column("scorecard_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("model_call_id", sa.Uuid(), nullable=True),
        sa.Column("from_recording", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column(
            "decided_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{_T}_decided_by_users"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_record_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "org_id",
            "application_id",
            "stage_key",
            "scorecards_hash",
            name="uq_hire_debriefs_inputs",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_debriefs_application_org",
        ),
        sa.CheckConstraint("jsonb_typeof(content) = 'object'", name=f"ck_{_T}_content_object"),
        sa.CheckConstraint(
            "scorecards_hash ~ '^[0-9a-f]{64}$'", name=f"ck_{_T}_scorecards_hash_format"
        ),
        sa.CheckConstraint("source IN ('model', 'template')", name=f"ck_{_T}_source_valid"),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('advance', 'hold', 'reject')",
            name=f"ck_{_T}_decision_valid",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR (decided_by IS NOT NULL AND decided_at IS NOT NULL "
            "AND char_length(decision_reason) >= 3)",
            name=f"ck_{_T}_decision_complete",
        ),
    )
    op.create_index(f"ix_{_T}_org_id", _T, ["org_id"])
    for stmt in org_rls_statements(_T, app_db_role(), privileges="SELECT, INSERT, UPDATE"):
        op.execute(stmt)
    op.execute(
        f"CREATE POLICY {_T}_portal_subject ON {_T} AS RESTRICTIVE "
        "USING (NOT hire_portal_active()) WITH CHECK (NOT hire_portal_active())"
    )


def downgrade() -> None:
    op.drop_table(_T)
