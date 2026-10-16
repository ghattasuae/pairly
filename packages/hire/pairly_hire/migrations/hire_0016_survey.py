# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire candidate experience survey (M3, brief 3.8.5).

Tables (``pairly_hire.survey.models``), tenant tables, RLS forced, standard tenant
policy, the application role gets SELECT, INSERT and UPDATE (never DELETE), hidden from
a candidate's portal session:

* ``hire_survey_invites``: one per finished application (hash of the link's secret).
* ``hire_survey_responses``: the answers, without any link to the invite; the candidate
  and application only when the candidate chose to attach their name (a check
  constraint holds it). The comment is encrypted.

The ``hire.candidate.survey`` email, English and Arabic, version 1.

Revision ID: hire_0016
Revises: hire_0015
Create Date: 2026-10-13
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0016"
down_revision: str | None = "hire_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SYSTEM_ACTOR = "pairly.migrations.hire_0016"
_TABLES = ("hire_survey_invites", "hire_survey_responses")


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


def upgrade() -> None:
    t = "hire_survey_invites"
    op.create_table(
        t,
        *_base(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("locale", sa.String(2), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_on", sa.Date(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("org_id", "application_id", name="uq_hire_survey_invites_application"),
        sa.ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_survey_invites_application_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "candidate_id"],
            ["hire_candidates.org_id", "hire_candidates.id"],
            ondelete="RESTRICT",
            name="fk_hire_survey_invites_candidate_org",
        ),
        sa.CheckConstraint(
            "outcome IN ('hired', 'rejected', 'withdrawn')", name=f"ck_{t}_outcome_valid"
        ),
        sa.CheckConstraint("status IN ('sent', 'skipped', 'used')", name=f"ck_{t}_status_valid"),
        sa.CheckConstraint("locale IN ('en', 'ar')", name=f"ck_{t}_locale_valid"),
        sa.CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name=f"ck_{t}_secret_hash_format"),
    )

    t = "hire_survey_responses"
    op.create_table(
        t,
        *_base(t),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("locale", sa.String(2), nullable=False),
        sa.Column("answers", postgresql.JSONB(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("named", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("answered_on", sa.Date(), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('hired', 'rejected', 'withdrawn')", name=f"ck_{t}_outcome_valid"
        ),
        sa.CheckConstraint("locale IN ('en', 'ar')", name=f"ck_{t}_locale_valid"),
        sa.CheckConstraint("jsonb_typeof(answers) = 'object'", name=f"ck_{t}_answers_object"),
        sa.CheckConstraint(
            "named OR (candidate_id IS NULL AND application_id IS NULL)", name=f"ck_{t}_anonymous"
        ),
    )

    role = app_db_role()
    for table in _TABLES:
        op.create_index(f"ix_{table}_org_id", table, ["org_id"])
        for stmt in org_rls_statements(table, role, privileges="SELECT, INSERT, UPDATE"):
            op.execute(stmt)
        op.execute(
            f"CREATE POLICY {table}_portal_subject ON {table} AS RESTRICTIVE "
            "USING (NOT hire_portal_active()) WITH CHECK (NOT hire_portal_active())"
        )

    from pairly_hire.interviewing.template_seed import insert_templates
    from pairly_hire.survey.templates import SURVEY_TEMPLATES

    insert_templates(op.get_bind(), SURVEY_TEMPLATES, version=1, actor=_SYSTEM_ACTOR)


def downgrade() -> None:
    from pairly_hire.interviewing.template_seed import remove_templates

    remove_templates(op.get_bind(), actor=_SYSTEM_ACTOR)
    for table in reversed(_TABLES):
        op.drop_table(table)
