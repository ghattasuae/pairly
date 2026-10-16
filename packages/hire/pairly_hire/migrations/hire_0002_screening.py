# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire screening: ``hire_screenings`` and ``hire_screening_settings`` (WP2.4).

* ``hire_screenings``: one row per screening run of an application (the
  ``hire_screening`` graph). Ids, statuses, digests, original offsets and the overall
  score in plain columns; what was read from the CV (guardrail report, parsed profile,
  criterion scores with quotes, knockout answers, flags) in ``details``, encrypted
  (``pairly_core.crypto``). SELECT, INSERT, UPDATE for the application role.
* ``hire_screening_settings``: blind mode, university names, the gap a claim check
  calls unexplained, the injection classifier mode. SELECT, INSERT, UPDATE.

Both are tenant tables with Row Level Security forced and the standard tenant policy.
Children point at their parents by ``(org_id, id)``.

``hire_screening_pending_org_ids()``: SECURITY DEFINER, returns the ids of organisations
with a submitted application (last 30 days, still ``received``) that has no screening
yet, and nothing else. The worker job ``hire_screening_start`` calls it, then works in
each organisation under Row Level Security as the application role. It switches on, for
its own statement only, the owner-only read policies ``app.hire_screening_scan`` added
here on ``hire_applications`` and ``hire_screenings`` (the setting means nothing to the
application role).

Revision ID: hire_0002
Revises: hire_0001
Create Date: 2026-10-09
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0002"
down_revision: str | None = "hire_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RW = "SELECT, INSERT, UPDATE"
_STATUSES = (
    "'processing', 'awaiting_review', 'shortlisted', 'not_shortlisted', 'returned', 'failed'"
)


def _uuid_pk() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _org(table: str) -> sa.Column[Any]:
    return sa.Column(
        "org_id",
        sa.Uuid(),
        sa.ForeignKey(
            "organisations.id", ondelete="RESTRICT", name=f"fk_{table}_org_id_organisations"
        ),
        nullable=False,
    )


def _ts(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _jsonb(name: str, default: str = "'[]'::jsonb") -> sa.Column[Any]:
    return sa.Column(name, postgresql.JSONB(), nullable=False, server_default=sa.text(default))


def _rls(table: str) -> None:
    for stmt in org_rls_statements(table, app_db_role(), privileges=_RW):
        op.execute(stmt)


def upgrade() -> None:
    t = "hire_screenings"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey(
                "documents.id", ondelete="RESTRICT", name=f"fk_{t}_document_id_documents"
            ),
            nullable=False,
        ),
        sa.Column(
            "rubric_policy_id",
            sa.Uuid(),
            sa.ForeignKey(
                "policy_versions.id",
                ondelete="RESTRICT",
                name=f"fk_{t}_rubric_policy_id_policy_versions",
            ),
            nullable=False,
        ),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("blind", sa.Boolean(), nullable=False),
        _jsonb("blind_config", "'{}'::jsonb"),
        _jsonb("review_reasons"),
        sa.Column("sanitized_sha256", sa.String(64), nullable=True),
        sa.Column("blinded_sha256", sa.String(64), nullable=True),
        _jsonb("removed_spans"),
        _jsonb("blind_spans"),
        sa.Column("extraction_id", sa.Uuid(), nullable=True),
        sa.Column("overall_score", sa.Numeric(5, 2), nullable=True),
        _jsonb("model_info", "'{}'::jsonb"),
        _jsonb("flag_codes"),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("score_decision_id", sa.String(30), nullable=True),
        _jsonb("decision_ids"),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("failure_reason", sa.String(200), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("decided_at", nullable=True),
        _ts("revealed_at", nullable=True),
        sa.UniqueConstraint("org_id", "id", name="uq_hire_screenings_org_id_id"),
        sa.ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_screenings_application_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_screenings_job_org",
        ),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="ck_hire_screenings_status_valid"),
        sa.CheckConstraint(
            "overall_score IS NULL OR (overall_score >= 0 AND overall_score <= 100)",
            name="ck_hire_screenings_overall_range",
        ),
        sa.CheckConstraint(
            "sanitized_sha256 IS NULL OR sanitized_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_hire_screenings_sanitized_sha256_format",
        ),
        sa.CheckConstraint(
            "blinded_sha256 IS NULL OR blinded_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_hire_screenings_blinded_sha256_format",
        ),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_hire_screenings_application", t, ["org_id", "application_id", "created_at"])
    op.create_index("ix_hire_screenings_status", t, ["org_id", "status", "updated_at"])
    _rls(t)

    t = "hire_screening_settings"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("blind_mode", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("blind_universities", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("gap_months", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("classifier_mode", sa.String(8), nullable=False, server_default="always"),
        sa.Column(
            "updated_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{t}_updated_by_users"),
            nullable=True,
        ),
        _ts("updated_at"),
        sa.UniqueConstraint("org_id", name="uq_hire_screening_settings_org_id"),
        sa.CheckConstraint(
            "gap_months BETWEEN 1 AND 60", name="ck_hire_screening_settings_gap_months_range"
        ),
        sa.CheckConstraint(
            "classifier_mode IN ('off', 'sampled', 'always')",
            name="ck_hire_screening_settings_classifier_mode_valid",
        ),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    _rls(t)
    _pending_org_finder(app_db_role())


def _pending_org_finder(app_role: str) -> None:
    for table in ("hire_applications", "hire_screenings"):
        op.execute(
            f"CREATE POLICY {table}_screening_scan ON {table} FOR SELECT TO CURRENT_USER "
            "USING (current_setting('app.hire_screening_scan', true) = 'on')"
        )
    op.execute(
        """
        CREATE FUNCTION hire_screening_pending_org_ids() RETURNS SETOF uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.hire_screening_scan', true);
            ids uuid[];
        BEGIN
            PERFORM set_config('app.hire_screening_scan', 'on', true);
            SELECT array_agg(DISTINCT a.org_id ORDER BY a.org_id) INTO ids
              FROM public.hire_applications a
             WHERE a.status = 'received'
               AND a.submitted_at IS NOT NULL
               AND a.submitted_at > now() - interval '30 days'
               AND NOT EXISTS (
                   SELECT 1 FROM public.hire_screenings s
                    WHERE s.org_id = a.org_id AND s.application_id = a.id
               );
            PERFORM set_config('app.hire_screening_scan', coalesce(previous, ''), true);
            RETURN QUERY SELECT unnest(coalesce(ids, '{}'::uuid[]));
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION hire_screening_pending_org_ids() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION hire_screening_pending_org_ids() TO {app_role}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS hire_screening_pending_org_ids()")
    op.execute("DROP POLICY IF EXISTS hire_applications_screening_scan ON hire_applications")
    op.drop_table("hire_screening_settings")
    op.drop_table("hire_screenings")
