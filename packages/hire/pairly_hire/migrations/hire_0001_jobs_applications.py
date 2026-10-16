# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire: jobs, rubric drafts, candidates, applications, self-identification,
settings, the acknowledgement templates and the careers page lookup.

The first revision of the ``hire`` branch (``HirePlugin.migrations_dir``). It starts from
no parent and ``depends_on`` core 0016 (the owner-only ``organisations`` read policy
switched on by ``app.admin_slug_lookup``, which the careers lookup reuses).

Tables (all tenant tables, RLS forced, standard tenant policy; the application role
never gets DELETE):

* ``hire_jobs``, ``hire_rubric_drafts``, ``hire_candidates``, ``hire_applications``,
  ``hire_org_settings``: SELECT, INSERT, UPDATE.
* ``hire_self_identification``: SELECT and INSERT only (answers are never changed).

Children point at their parents by ``(org_id, id)``, so no row can reference another
organisation's.

``hire_careers_org(slug)``: SECURITY DEFINER, returns ``(org_id, org_name)`` of an active
organisation whose careers page is visible (its settings say so, or it has no settings
and at least one open job), and nothing else. The public careers routes call it, then
bind the session to that organisation and read under RLS. It switches on, for its own
statement only, the owner-only read policies ``app.admin_slug_lookup`` (organisations,
core 0016) and ``app.hire_careers_lookup`` (``hire_jobs`` and ``hire_org_settings``,
added here); both settings mean nothing to the application role.

``hire.application.received`` templates (email, en and ar) as version 1 shared rows,
skipped when they exist (idempotent).

Revision ID: hire_0001
Revises: (none; branch ``hire``, depends on core 0016)
Create Date: 2026-10-08
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = ("hire",)
depends_on: str | Sequence[str] | None = "0016"

_SYSTEM_ACTOR = "pairly.migrations.hire_0001"
_TEMPLATE_KEY = "hire.application.received"
_RW = "SELECT, INSERT, UPDATE"


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


def _user_fk(table: str, column: str, *, nullable: bool) -> sa.Column[Any]:
    return sa.Column(
        column,
        sa.Uuid(),
        sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{table}_{column}_users"),
        nullable=nullable,
    )


def _policy_fk(table: str, column: str, *, nullable: bool) -> sa.Column[Any]:
    return sa.Column(
        column,
        sa.Uuid(),
        sa.ForeignKey(
            "policy_versions.id", ondelete="RESTRICT", name=f"fk_{table}_{column}_policy_versions"
        ),
        nullable=nullable,
    )


def _jsonb(name: str, default: str = "'[]'::jsonb") -> sa.Column[Any]:
    return sa.Column(name, postgresql.JSONB(), nullable=False, server_default=sa.text(default))


def _org_index(table: str) -> None:
    op.create_index(f"ix_{table}_org_id", table, ["org_id"])


def _rls(table: str, privileges: str = _RW) -> None:
    for stmt in org_rls_statements(table, app_db_role(), privileges=privileges):
        op.execute(stmt)


def upgrade() -> None:
    app_role = app_db_role()

    # ---------------------------------------------------------------- hire_jobs
    t = "hire_jobs"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("title_ar", sa.String(160), nullable=False, server_default=""),
        sa.Column("team", sa.String(120), nullable=True),
        sa.Column("location", sa.String(160), nullable=False),
        sa.Column("country", sa.String(2), nullable=True),
        sa.Column("work_model", sa.String(16), nullable=False),
        sa.Column("employment_type", sa.String(16), nullable=False),
        sa.Column("salary_min", sa.Numeric(14, 2), nullable=True),
        sa.Column("salary_max", sa.Numeric(14, 2), nullable=True),
        sa.Column("salary_currency", sa.String(3), nullable=True),
        sa.Column("salary_period", sa.String(8), nullable=True),
        sa.Column("show_salary", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("description_en", sa.Text(), nullable=False, server_default=""),
        sa.Column("description_ar", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False),
        _policy_fk(t, "rubric_policy_id", nullable=True),
        _policy_fk(t, "form_policy_id", nullable=True),
        _user_fk(t, "created_by", nullable=True),
        _ts("published_at", nullable=True),
        _ts("closed_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("org_id", "id", name="uq_hire_jobs_org_id_id"),
        sa.UniqueConstraint("org_id", "slug", name="uq_hire_jobs_org_id_slug"),
        sa.CheckConstraint(
            "status IN ('draft', 'open', 'paused', 'closed')", name="ck_hire_jobs_status_valid"
        ),
        sa.CheckConstraint(
            "work_model IN ('onsite', 'hybrid', 'remote')", name="ck_hire_jobs_work_model_valid"
        ),
        sa.CheckConstraint(
            "employment_type IN ('full_time', 'part_time', 'contract', 'temporary', 'internship')",
            name="ck_hire_jobs_employment_type_valid",
        ),
        sa.CheckConstraint(
            "salary_period IS NULL OR salary_period IN ('year', 'month', 'hour')",
            name="ck_hire_jobs_salary_period_valid",
        ),
        sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{0,79}$'", name="ck_hire_jobs_slug_format"),
        sa.CheckConstraint(
            "salary_currency IS NULL OR salary_currency ~ '^[A-Z]{3}$'",
            name="ck_hire_jobs_currency_format",
        ),
        sa.CheckConstraint(
            "country IS NULL OR country ~ '^[A-Z]{2}$'", name="ck_hire_jobs_country_format"
        ),
        sa.CheckConstraint(
            "(salary_min IS NULL OR salary_min >= 0) AND (salary_max IS NULL OR salary_max >= 0)",
            name="ck_hire_jobs_salary_non_negative",
        ),
        sa.CheckConstraint(
            "salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max",
            name="ck_hire_jobs_salary_range_order",
        ),
        sa.CheckConstraint(
            "NOT show_salary OR (salary_min IS NOT NULL AND salary_max IS NOT NULL "
            "AND salary_currency IS NOT NULL AND salary_period IS NOT NULL)",
            name="ck_hire_jobs_shown_salary_complete",
        ),
        sa.CheckConstraint(
            "status <> 'open' OR (rubric_policy_id IS NOT NULL AND form_policy_id IS NOT NULL)",
            name="ck_hire_jobs_open_needs_rubric_and_form",
        ),
    )
    _org_index(t)
    op.create_index("ix_hire_jobs_status", t, ["org_id", "status", "published_at"])
    _rls(t)

    # ---------------------------------------------------------------- hire_rubric_drafts
    t = "hire_rubric_drafts"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("model_call_id", sa.Uuid(), nullable=True),
        _user_fk(t, "updated_by", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("org_id", "job_id", name="uq_hire_rubric_drafts_job"),
        sa.ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_rubric_drafts_job_org",
        ),
        sa.CheckConstraint("source IN ('ai', 'person')", name="ck_hire_rubric_drafts_source_valid"),
    )
    _org_index(t)
    _rls(t)

    # ---------------------------------------------------------------- hire_candidates
    t = "hire_candidates"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("email_bidx", sa.String(64), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        _jsonb("name_bidx"),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("phone_bidx", sa.String(64), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("profile_url", sa.Text(), nullable=True),
        sa.Column("locale", sa.String(2), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("org_id", "id", name="uq_hire_candidates_org_id_id"),
        sa.UniqueConstraint("org_id", "email_bidx", name="uq_hire_candidates_org_id_email_bidx"),
        sa.CheckConstraint(
            "email_bidx ~ '^[0-9a-f]{64}$'", name="ck_hire_candidates_email_bidx_format"
        ),
        sa.CheckConstraint(
            "phone_bidx IS NULL OR phone_bidx ~ '^[0-9a-f]{64}$'",
            name="ck_hire_candidates_phone_bidx_format",
        ),
        sa.CheckConstraint("locale IN ('en', 'ar')", name="ck_hire_candidates_locale_valid"),
    )
    _org_index(t)
    op.create_index("ix_hire_candidates_phone_bidx", t, ["org_id", "phone_bidx"])
    op.create_index("ix_hire_candidates_name_bidx", t, ["name_bidx"], postgresql_using="gin")
    _rls(t)

    # ---------------------------------------------------------------- hire_applications
    t = "hire_applications"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("reference", sa.String(9), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        _policy_fk(t, "rubric_policy_id", nullable=False),
        _policy_fk(t, "form_policy_id", nullable=False),
        sa.Column("answers", sa.Text(), nullable=False),
        sa.Column("contact", sa.Text(), nullable=False),
        _jsonb("review_reasons"),
        sa.Column("needs_human_review", sa.Boolean(), nullable=False, server_default="false"),
        _jsonb("duplicate_signals"),
        sa.Column("consent_version", sa.String(32), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consent_locale", sa.String(2), nullable=False),
        sa.Column(
            "cv_document_id",
            sa.Uuid(),
            sa.ForeignKey(
                "documents.id",
                ondelete="RESTRICT",
                name="fk_hire_applications_cv_document_id_documents",
            ),
            nullable=True,
        ),
        _jsonb("files", "'{}'::jsonb"),
        sa.Column("upload_token_hash", sa.String(64), nullable=True),
        _ts("upload_token_expires_at", nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="careers"),
        _ts("submitted_at", nullable=True),
        _ts("acknowledged_at", nullable=True),
        _ts("rejected_at", nullable=True),
        _ts("retention_until", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("org_id", "id", name="uq_hire_applications_org_id_id"),
        sa.UniqueConstraint("org_id", "reference", name="uq_hire_applications_org_id_reference"),
        sa.UniqueConstraint("upload_token_hash", name="uq_hire_applications_upload_token_hash"),
        sa.ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_applications_job_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "candidate_id"],
            ["hire_candidates.org_id", "hire_candidates.id"],
            ondelete="RESTRICT",
            name="fk_hire_applications_candidate_org",
        ),
        sa.CheckConstraint(
            "status IN ('incomplete', 'received', 'rejected', 'withdrawn')",
            name="ck_hire_applications_status_valid",
        ),
        sa.CheckConstraint(
            "reference ~ '^[A-Z0-9]{4}-[A-Z0-9]{4}$'", name="ck_hire_applications_reference_format"
        ),
        sa.CheckConstraint(
            "consent_locale IN ('en', 'ar')", name="ck_hire_applications_consent_locale_valid"
        ),
        sa.CheckConstraint(
            "upload_token_hash IS NULL OR upload_token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_hire_applications_upload_token_hash_format",
        ),
        sa.CheckConstraint(
            "status = 'incomplete' OR (cv_document_id IS NOT NULL AND submitted_at IS NOT NULL)",
            name="ck_hire_applications_submitted_complete",
        ),
        sa.CheckConstraint(
            "status <> 'rejected' OR (rejected_at IS NOT NULL AND retention_until IS NOT NULL)",
            name="ck_hire_applications_rejected_has_retention",
        ),
    )
    _org_index(t)
    op.create_index("ix_hire_applications_job", t, ["org_id", "job_id", "status", "submitted_at"])
    op.create_index("ix_hire_applications_candidate", t, ["org_id", "candidate_id"])
    op.create_index("ix_hire_applications_cv", t, ["org_id", "cv_document_id"])
    _rls(t)

    # ---------------------------------------------------------------- self-identification
    t = "hire_self_identification"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("answers", sa.Text(), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("org_id", "application_id", name="uq_hire_self_identification_app"),
        sa.ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="CASCADE",
            name="fk_hire_self_identification_application_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_self_identification_job_org",
        ),
    )
    _org_index(t)
    _rls(t, "SELECT, INSERT")

    # ---------------------------------------------------------------- settings
    t = "hire_org_settings"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("careers_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("rejected_retention_months", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("response_days", sa.Integer(), nullable=False, server_default="10"),
        _user_fk(t, "updated_by", nullable=True),
        _ts("updated_at"),
        sa.UniqueConstraint("org_id", name="uq_hire_org_settings_org_id"),
        sa.CheckConstraint(
            "rejected_retention_months BETWEEN 1 AND 120",
            name="ck_hire_org_settings_retention_months_range",
        ),
        sa.CheckConstraint(
            "response_days BETWEEN 1 AND 90", name="ck_hire_org_settings_response_days_range"
        ),
    )
    _org_index(t)
    _rls(t)

    _careers_lookup(app_role)
    _seed_templates()


def _careers_lookup(app_role: str) -> None:
    for table in ("hire_jobs", "hire_org_settings"):
        op.execute(
            f"CREATE POLICY {table}_careers_lookup ON {table} FOR SELECT TO CURRENT_USER "
            "USING (current_setting('app.hire_careers_lookup', true) = 'on')"
        )
    op.execute(
        """
        CREATE FUNCTION hire_careers_org(p_slug text)
        RETURNS TABLE(org_id uuid, org_name text)
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        #variable_conflict use_column
        DECLARE
            previous_admin text := current_setting('app.admin_slug_lookup', true);
            previous_hire text := current_setting('app.hire_careers_lookup', true);
            found_id uuid;
            found_name text;
        BEGIN
            IF p_slug IS NULL OR p_slug !~ '^[a-z0-9][a-z0-9-]{1,62}$' THEN
                RETURN;
            END IF;
            PERFORM set_config('app.admin_slug_lookup', 'on', true);
            PERFORM set_config('app.hire_careers_lookup', 'on', true);
            SELECT o.id, o.name INTO found_id, found_name
              FROM public.organisations o
             WHERE o.slug = p_slug
               AND o.status = 'active'
               AND (
                    EXISTS (SELECT 1 FROM public.hire_org_settings s
                             WHERE s.org_id = o.id AND s.careers_enabled)
                 OR (NOT EXISTS (SELECT 1 FROM public.hire_org_settings s
                                  WHERE s.org_id = o.id)
                     AND EXISTS (SELECT 1 FROM public.hire_jobs j
                                  WHERE j.org_id = o.id AND j.status = 'open'))
               );
            PERFORM set_config('app.admin_slug_lookup', coalesce(previous_admin, ''), true);
            PERFORM set_config('app.hire_careers_lookup', coalesce(previous_hire, ''), true);
            IF found_id IS NOT NULL THEN
                org_id := found_id;
                org_name := found_name;
                RETURN NEXT;
            END IF;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION hire_careers_org(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION hire_careers_org(text) TO {app_role}")


def _seed_templates() -> None:
    from pairly_core.notifications.templating import (
        TemplateSource,
        declarations_json,
        lint_source,
        parse_declarations,
    )
    from pairly_hire.templates import RECEIVED_TEMPLATES

    bind = op.get_bind()
    op.execute("ALTER TABLE templates NO FORCE ROW LEVEL SECURITY")
    insert = sa.text(
        "INSERT INTO templates (org_id, key, channel, locale, version, category, subject, "
        "body, body_html, variables, requires_approval, created_by_type, created_by_id) "
        "SELECT NULL, CAST(:key AS text), CAST(:channel AS text), CAST(:locale AS text), 1, "
        "CAST(:category AS text), CAST(:subject AS text), CAST(:body AS text), "
        "CAST(:body_html AS text), CAST(:variables AS jsonb), "
        "CAST(:requires_approval AS boolean), 'system', CAST(:actor AS text) "
        "WHERE NOT EXISTS (SELECT 1 FROM templates WHERE org_id IS NULL "
        "AND key = CAST(:key AS text) AND channel = CAST(:channel AS text) "
        "AND locale = CAST(:locale AS text) AND version = 1)"
    )
    for t in RECEIVED_TEMPLATES:
        specs = parse_declarations(t.variables)
        source = TemplateSource(t.channel, t.locale, t.subject, t.body, t.body_html, specs)  # type: ignore[arg-type]
        if any(i.severity == "error" for i in lint_source(source)):
            raise RuntimeError(f"system template {t.key} ({t.channel}, {t.locale}) is invalid")
        bind.execute(
            insert,
            {
                "key": t.key,
                "channel": t.channel,
                "locale": t.locale,
                "category": t.category,
                "subject": t.subject,
                "body": t.body,
                "body_html": t.body_html,
                "variables": json.dumps(declarations_json(specs)),
                "requires_approval": t.requires_approval,
                "actor": _SYSTEM_ACTOR,
            },
        )
    op.execute("ALTER TABLE templates FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS hire_careers_org(text)")
    for table in (
        "hire_self_identification",
        "hire_applications",
        "hire_candidates",
        "hire_rubric_drafts",
        "hire_org_settings",
        "hire_jobs",
    ):
        op.drop_table(table)
    op.execute("ALTER TABLE templates NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notifications NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates DISABLE TRIGGER templates_immutable")
    op.execute(
        sa.text(
            "DELETE FROM templates t WHERE t.org_id IS NULL AND t.key = :key "
            "AND t.created_by_id = :actor "
            "AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.template_id = t.id)"
        ).bindparams(key=_TEMPLATE_KEY, actor=_SYSTEM_ACTOR)
    )
    op.execute("ALTER TABLE templates ENABLE TRIGGER templates_immutable")
    op.execute("ALTER TABLE notifications FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates FORCE ROW LEVEL SECURITY")
