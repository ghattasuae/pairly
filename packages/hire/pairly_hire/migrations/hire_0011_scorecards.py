# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire independent scorecards (M3, brief 3.7.2 and 3.7.3).

Tables (``pairly_hire.interviewing.models``), all tenant tables, RLS forced, standard
tenant policy, the application role gets SELECT, INSERT and UPDATE (never DELETE),
hidden from a candidate's portal session, pointing at their interview by
``(org_id, id)``:

* ``hire_scorecards``: one per interview and interviewer. A trigger refuses any change
  to a submitted scorecard's ratings, recommendation, status or seat, and any change to
  its notes except clearing them (erasure).
* ``hire_scorecard_links``: single use one-click links (hash of the secret only).
* ``hire_scorecard_sessions``: the short session a used link opens, for one interview.

The ``hire.interviewer.scorecard_reminder`` email, English and Arabic, version 1.

Revision ID: hire_0011
Revises: hire_0010
Create Date: 2026-10-13
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0011"
down_revision: str | None = "hire_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SYSTEM_ACTOR = "pairly.migrations.hire_0011"
_TABLES = ("hire_scorecards", "hire_scorecard_links", "hire_scorecard_sessions")


def _common(table: str) -> list[Any]:
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
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id", "interview_id"],
            ["hire_interviews.org_id", "hire_interviews.id"],
            ondelete="RESTRICT",
            name=f"fk_{table}_interview_org",
        ),
    ]


def _user(table: str, column: str) -> sa.Column[Any]:
    return sa.Column(
        column,
        sa.Uuid(),
        sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{table}_{column}_users"),
        nullable=False,
    )


def _ts(name: str, *, nullable: bool = True) -> sa.Column[Any]:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    t = "hire_scorecards"
    op.create_table(
        t,
        *_common(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("stage_key", sa.String(32), nullable=False),
        _user(t, "interviewer_id"),
        sa.Column("criteria", postgresql.JSONB(), nullable=False),
        sa.Column(
            "ratings", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("recommendation", sa.String(16), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        _ts("submitted_at"),
        _ts("reminded_2h_at"),
        _ts("reminded_24h_at"),
        _ts("snoozed_until"),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint(
            "org_id", "interview_id", "interviewer_id", name="uq_hire_scorecards_seat"
        ),
        sa.UniqueConstraint("org_id", "id", name="uq_hire_scorecards_org_id_id"),
        sa.CheckConstraint("status IN ('draft', 'submitted')", name=f"ck_{t}_status_valid"),
        sa.CheckConstraint(
            "recommendation IS NULL OR recommendation IN ('strong_no', 'no', 'yes', 'strong_yes')",
            name=f"ck_{t}_recommendation_valid",
        ),
        sa.CheckConstraint("jsonb_typeof(ratings) = 'object'", name=f"ck_{t}_ratings_object"),
        sa.CheckConstraint("jsonb_typeof(criteria) = 'array'", name=f"ck_{t}_criteria_array"),
        sa.CheckConstraint("char_length(notes) <= 2000", name=f"ck_{t}_notes_length"),
        sa.CheckConstraint(
            "status = 'draft' OR (submitted_at IS NOT NULL AND recommendation IS NOT NULL)",
            name=f"ck_{t}_submitted_complete",
        ),
    )

    t = "hire_scorecard_links"
    op.create_table(
        t,
        *_common(t),
        _user(t, "user_id"),
        sa.Column("purpose", sa.String(8), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _ts("used_at"),
        _ts("created_at", nullable=False),
        sa.CheckConstraint("purpose IN ('open', 'later')", name=f"ck_{t}_purpose_valid"),
        sa.CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name=f"ck_{t}_secret_hash_format"),
    )

    t = "hire_scorecard_sessions"
    op.create_table(
        t,
        *_common(t),
        _user(t, "user_id"),
        sa.Column("link_id", sa.Uuid(), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _ts("revoked_at"),
        _ts("created_at", nullable=False),
        sa.CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name=f"ck_{t}_secret_hash_format"),
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

    op.execute(
        """
        CREATE FUNCTION hire_scorecards_freeze() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF OLD.status = 'submitted' AND (
                NEW.ratings IS DISTINCT FROM OLD.ratings
                OR NEW.recommendation IS DISTINCT FROM OLD.recommendation
                OR NEW.status IS DISTINCT FROM OLD.status
                OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at
                OR NEW.interview_id IS DISTINCT FROM OLD.interview_id
                OR NEW.interviewer_id IS DISTINCT FROM OLD.interviewer_id
                OR NEW.criteria IS DISTINCT FROM OLD.criteria
                OR (NEW.notes IS DISTINCT FROM OLD.notes AND NEW.notes <> '')
            ) THEN
                RAISE EXCEPTION 'a submitted scorecard cannot change'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER hire_scorecards_freeze BEFORE UPDATE ON hire_scorecards "
        "FOR EACH ROW EXECUTE FUNCTION hire_scorecards_freeze()"
    )

    from pairly_hire.interviewing.template_seed import insert_templates
    from pairly_hire.interviewing.templates import SCORECARD_REMINDERS

    insert_templates(op.get_bind(), SCORECARD_REMINDERS, version=1, actor=_SYSTEM_ACTOR)


def downgrade() -> None:
    from pairly_hire.interviewing.template_seed import remove_templates

    remove_templates(op.get_bind(), actor=_SYSTEM_ACTOR)
    op.execute("DROP TRIGGER IF EXISTS hire_scorecards_freeze ON hire_scorecards")
    op.execute("DROP FUNCTION IF EXISTS hire_scorecards_freeze()")
    for table in reversed(_TABLES):
        op.drop_table(table)
