# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire pipeline (WP2.5) and messaging (WP2.7).

Tables (all tenant tables, RLS forced, standard tenant policy; the application role
never gets DELETE):

* ``hire_pipeline_configs``, ``hire_pipeline_entries``, ``hire_referral_links``,
  ``hire_messages``, ``hire_candidate_prefs``, ``hire_messaging_settings``: SELECT,
  INSERT, UPDATE.
* ``hire_stage_moves``, ``hire_notes``, ``hire_referrals``: SELECT and INSERT only
  (history is never changed; an undo is a new move).

Children point at their parents by ``(org_id, id)``, so no row can reference another
organisation's.

``hire_pipeline_org_ids()``: SECURITY DEFINER, returns the ids of organisations with
submitted applications and nothing else (the pattern of migration 0006). The worker's
Hire jobs (anti-ghosting drafts, the rediscovery index) use it to know which
organisations to visit, then work in each under normal RLS as the application role. It
switches on, for its own statement only, the owner-only read policy
``app.hire_pipeline_scan`` on ``hire_applications``.

System templates of ``pairly_hire.messaging.library.SYSTEM_TEMPLATES`` (candidate
messages, the mention and SLA alerts, English and Arabic) as version 1 shared rows,
skipped when they exist (idempotent).

Revision ID: hire_0003
Revises: hire_0001
Create Date: 2026-10-09
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0003"
down_revision: str | None = "hire_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SYSTEM_ACTOR = "pairly.migrations.hire_0003"
_RW = "SELECT, INSERT, UPDATE"
_RO = "SELECT, INSERT"
_STAGE_KEY = "'^[a-z][a-z0-9_]{0,31}$'"


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


def _fk(table: str, column: str, target: str, *, nullable: bool) -> sa.Column[Any]:
    return sa.Column(
        column,
        sa.Uuid(),
        sa.ForeignKey(f"{target}.id", ondelete="RESTRICT", name=f"fk_{table}_{column}_{target}"),
        nullable=nullable,
    )


def _jsonb(name: str, default: str = "'[]'::jsonb") -> sa.Column[Any]:
    return sa.Column(name, postgresql.JSONB(), nullable=False, server_default=sa.text(default))


def _job_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id", "job_id"],
        ["hire_jobs.org_id", "hire_jobs.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_job_org",
    )


def _application_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id", "application_id"],
        ["hire_applications.org_id", "hire_applications.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_application_org",
    )


def _candidate_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id", "candidate_id"],
        ["hire_candidates.org_id", "hire_candidates.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_candidate_org",
    )


def _finish(table: str, privileges: str = _RW) -> None:
    op.create_index(f"ix_{table}_org_id", table, ["org_id"])
    for stmt in org_rls_statements(table, app_db_role(), privileges=privileges):
        op.execute(stmt)


def upgrade() -> None:
    app_role = app_db_role()

    t = "hire_pipeline_configs"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("stages", postgresql.JSONB(), nullable=False),
        _fk(t, "updated_by", "users", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("org_id", "job_id", name="uq_hire_pipeline_configs_job"),
        _job_fk(t),
        sa.CheckConstraint(
            "jsonb_typeof(stages) = 'array'", name="ck_hire_pipeline_configs_stages_array"
        ),
    )
    _finish(t)

    t = "hire_pipeline_entries"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("stage_key", sa.String(32), nullable=False),
        _ts("stage_entered_at"),
        sa.Column("revealed", sa.Boolean(), nullable=False, server_default="false"),
        _jsonb("tags"),
        _ts("last_update_at", nullable=True),
        _ts("sla_drafted_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint(
            "org_id", "application_id", name="uq_hire_pipeline_entries_application"
        ),
        _application_fk(t),
        _job_fk(t),
        sa.CheckConstraint(
            f"stage_key ~ {_STAGE_KEY}", name="ck_hire_pipeline_entries_stage_key_format"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(tags) = 'array'", name="ck_hire_pipeline_entries_tags_array"
        ),
    )
    op.create_index("ix_hire_pipeline_entries_job_stage", t, ["org_id", "job_id", "stage_key"])
    op.create_index("ix_hire_pipeline_entries_waiting", t, ["org_id", "last_update_at"])
    _finish(t)

    t = "hire_stage_moves"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("from_stage", sa.String(32), nullable=False),
        sa.Column("to_stage", sa.String(32), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        _fk(t, "undo_of", "hire_stage_moves", nullable=True),
        _ts("created_at"),
        _application_fk(t),
        _job_fk(t),
        sa.CheckConstraint(f"to_stage ~ {_STAGE_KEY}", name="ck_hire_stage_moves_to_stage_format"),
        sa.CheckConstraint(
            "actor_type IN ('user', 'agent', 'api_key', 'system')",
            name="ck_hire_stage_moves_actor_type_valid",
        ),
    )
    op.create_index(
        "ix_hire_stage_moves_application", t, ["org_id", "application_id", "created_at"]
    )
    op.create_index("ix_hire_stage_moves_batch", t, ["org_id", "batch_id"])
    op.create_index(
        "uq_hire_stage_moves_undo",
        t,
        ["undo_of"],
        unique=True,
        postgresql_where=sa.text("undo_of IS NOT NULL"),
    )
    _finish(t, _RO)

    t = "hire_notes"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        _fk(t, "author_id", "users", nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        _jsonb("mentions"),
        _ts("created_at"),
        _application_fk(t),
        sa.CheckConstraint("jsonb_typeof(mentions) = 'array'", name="ck_hire_notes_mentions_array"),
    )
    op.create_index("ix_hire_notes_application", t, ["org_id", "application_id", "created_at"])
    _finish(t, _RO)

    t = "hire_referral_links"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        _fk(t, "referrer_id", "users", nullable=False),
        _ts("created_at"),
        _ts("revoked_at", nullable=True),
        sa.UniqueConstraint("org_id", "id", name="uq_hire_referral_links_org_id_id"),
        sa.UniqueConstraint(
            "org_id", "job_id", "referrer_id", name="uq_hire_referral_links_job_referrer"
        ),
        _job_fk(t),
    )
    _finish(t)

    t = "hire_referrals"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("link_id", sa.Uuid(), nullable=False),
        _fk(t, "referrer_id", "users", nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("org_id", "application_id", name="uq_hire_referrals_application"),
        _application_fk(t),
        _job_fk(t),
        sa.ForeignKeyConstraint(
            ["org_id", "link_id"],
            ["hire_referral_links.org_id", "hire_referral_links.id"],
            ondelete="RESTRICT",
            name="fk_hire_referrals_link_org",
        ),
    )
    op.create_index("ix_hire_referrals_referrer", t, ["org_id", "referrer_id", "created_at"])
    _finish(t, _RO)

    t = "hire_messages"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False, server_default="outbound"),
        sa.Column("template_key", sa.String(96), nullable=True),
        sa.Column("locale", sa.String(2), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("sensitive", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("variables", sa.Text(), nullable=False),
        sa.Column("rendered", sa.Text(), nullable=True),
        _jsonb("ai_fields"),
        sa.Column("model_call_id", sa.Uuid(), nullable=True),
        sa.Column("reason_source", sa.String(24), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        _fk(t, "approval_id", "approvals", nullable=True),
        _fk(t, "run_id", "workflow_runs", nullable=True),
        _fk(t, "notification_id", "notifications", nullable=True),
        sa.Column("status_reason", sa.String(64), nullable=True),
        _fk(t, "submitted_by", "users", nullable=True),
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.String(128), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("sent_at", nullable=True),
        _application_fk(t),
        _candidate_fk(t),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'awaiting_approval', 'changes_requested', "
            "'declined', 'queued', 'sent', 'failed', 'suppressed', 'cancelled', 'received')",
            name="ck_hire_messages_status_valid",
        ),
        sa.CheckConstraint(
            "purpose IN ('manual', 'rejection', 'sla_update', 'bulk', 'reply')",
            name="ck_hire_messages_purpose_valid",
        ),
        sa.CheckConstraint(
            "direction IN ('outbound', 'inbound')", name="ck_hire_messages_direction_valid"
        ),
        sa.CheckConstraint("locale IN ('en', 'ar')", name="ck_hire_messages_locale_valid"),
        sa.CheckConstraint(
            "content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_hire_messages_content_hash_format",
        ),
        sa.CheckConstraint(
            "direction = 'inbound' OR template_key IS NOT NULL",
            name="ck_hire_messages_outbound_has_template",
        ),
        sa.CheckConstraint(
            "status <> 'awaiting_approval' OR approval_id IS NOT NULL",
            name="ck_hire_messages_awaiting_has_approval",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(ai_fields) = 'array'", name="ck_hire_messages_ai_fields_array"
        ),
    )
    op.create_index("ix_hire_messages_application", t, ["org_id", "application_id", "created_at"])
    op.create_index("ix_hire_messages_approval", t, ["org_id", "approval_id"])
    op.create_index("ix_hire_messages_run", t, ["org_id", "run_id"])
    _finish(t)

    t = "hire_candidate_prefs"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("updates_email", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("talent_pool", sa.Boolean(), nullable=False, server_default="false"),
        _ts("talent_pool_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("org_id", "candidate_id", name="uq_hire_candidate_prefs_candidate"),
        _candidate_fk(t),
    )
    _finish(t)

    t = "hire_messaging_settings"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("sla_days", sa.Integer(), nullable=False, server_default="7"),
        _fk(t, "updated_by", "users", nullable=True),
        _ts("updated_at"),
        sa.UniqueConstraint("org_id", name="uq_hire_messaging_settings_org_id"),
        sa.CheckConstraint(
            "sla_days BETWEEN 1 AND 60", name="ck_hire_messaging_settings_sla_days_range"
        ),
    )
    _finish(t)

    _org_finder(app_role)
    _seed_templates()


def _org_finder(app_role: str) -> None:
    op.execute(
        "CREATE POLICY hire_applications_pipeline_scan ON hire_applications FOR SELECT "
        "TO CURRENT_USER USING (current_setting('app.hire_pipeline_scan', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION hire_pipeline_org_ids() RETURNS SETOF uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.hire_pipeline_scan', true);
            ids uuid[];
        BEGIN
            PERFORM set_config('app.hire_pipeline_scan', 'on', true);
            SELECT array_agg(DISTINCT a.org_id ORDER BY a.org_id) INTO ids
              FROM public.hire_applications a
             WHERE a.status <> 'incomplete';
            PERFORM set_config('app.hire_pipeline_scan', coalesce(previous, ''), true);
            RETURN QUERY SELECT unnest(coalesce(ids, '{}'::uuid[]));
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION hire_pipeline_org_ids() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION hire_pipeline_org_ids() TO {app_role}")


def _seed_templates() -> None:
    from pairly_core.notifications.templating import (
        TemplateSource,
        declarations_json,
        lint_source,
        parse_declarations,
    )
    from pairly_hire.messaging.library import SYSTEM_TEMPLATES

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
    for t in SYSTEM_TEMPLATES:
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
    op.execute("DROP FUNCTION IF EXISTS hire_pipeline_org_ids()")
    op.execute("DROP POLICY IF EXISTS hire_applications_pipeline_scan ON hire_applications")
    for table in (
        "hire_messaging_settings",
        "hire_candidate_prefs",
        "hire_messages",
        "hire_referrals",
        "hire_referral_links",
        "hire_notes",
        "hire_stage_moves",
        "hire_pipeline_entries",
        "hire_pipeline_configs",
    ):
        op.drop_table(table)
    op.execute("ALTER TABLE templates NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notifications NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates DISABLE TRIGGER templates_immutable")
    op.execute(
        sa.text(
            "DELETE FROM templates t WHERE t.org_id IS NULL AND t.created_by_id = :actor "
            "AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.template_id = t.id)"
        ).bindparams(actor=_SYSTEM_ACTOR)
    )
    op.execute("ALTER TABLE templates ENABLE TRIGGER templates_immutable")
    op.execute("ALTER TABLE notifications FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates FORCE ROW LEVEL SECURITY")
