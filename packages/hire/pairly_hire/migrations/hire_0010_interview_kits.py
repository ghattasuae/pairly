# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire interview kits (M3, brief 3.7.1).

``hire_interview_kits``: a generated kit per application, stage, language and inputs
hash (``pairly_hire.interviewing.models``). It holds no candidate text. Tenant table,
RLS forced, standard tenant policy; the application role gets SELECT, INSERT and
UPDATE, never DELETE. Hidden from a candidate's portal session. Points at its
application by ``(org_id, id)``.

The interviewer briefing (``hire.interviewer.briefing``) version 2, English and Arabic:
the interview kit in place of the M2 placeholder and the one-click scorecard link
(``pairly_hire.interviewing.templates.BRIEFING_V2``).

Revision ID: hire_0010
Revises: hire_0009
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0010"
down_revision: str | None = "hire_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_T = "hire_interview_kits"
_SYSTEM_ACTOR = "pairly.migrations.hire_0010"


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
        sa.Column("locale", sa.String(2), nullable=False),
        sa.Column("inputs_hash", sa.String(64), nullable=False),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column(
            "model_call_ids",
            postgresql.ARRAY(sa.Uuid()),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("from_recording", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "org_id",
            "application_id",
            "stage_key",
            "locale",
            "inputs_hash",
            name="uq_hire_interview_kits_inputs",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_interview_kits_application_org",
        ),
        sa.CheckConstraint("locale IN ('en', 'ar')", name=f"ck_{_T}_locale_valid"),
        sa.CheckConstraint("jsonb_typeof(items) = 'array'", name=f"ck_{_T}_items_array"),
        sa.CheckConstraint("inputs_hash ~ '^[0-9a-f]{64}$'", name=f"ck_{_T}_inputs_hash_format"),
    )
    op.create_index(f"ix_{_T}_org_id", _T, ["org_id"])
    for stmt in org_rls_statements(_T, app_db_role(), privileges="SELECT, INSERT, UPDATE"):
        op.execute(stmt)
    op.execute(
        f"CREATE POLICY {_T}_portal_subject ON {_T} AS RESTRICTIVE "
        "USING (NOT hire_portal_active()) WITH CHECK (NOT hire_portal_active())"
    )
    from pairly_hire.interviewing.template_seed import insert_templates
    from pairly_hire.interviewing.templates import BRIEFING_V2

    insert_templates(op.get_bind(), BRIEFING_V2, version=2, actor=_SYSTEM_ACTOR)


def downgrade() -> None:
    from pairly_hire.interviewing.template_seed import remove_templates

    remove_templates(op.get_bind(), actor=_SYSTEM_ACTOR)
    op.drop_table(_T)
