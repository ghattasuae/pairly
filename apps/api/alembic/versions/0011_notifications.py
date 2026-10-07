# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Notifications, templates, preferences and the Slack integration (WP1.9).

Tables (all with Row Level Security):

* ``templates``: immutable template versions. ``org_id`` NULL rows are the shared system
  defaults (``pairly_core.notifications.defaults``), seeded here before RLS is forced;
  every organisation reads them, nobody writes them. Organisation rows are readable and
  insertable by their organisation only. The application role gets SELECT and INSERT,
  and a trigger refuses UPDATE and DELETE for every role: a change is a new version.
* ``notifications``: queued, held, sent, failed and suppressed messages. Content and
  address are encrypted (``pairly_core.crypto``). The application role may UPDATE the
  delivery columns only (status, attempts, timing, provider id, read time).
* ``notification_preferences``: one row per person per organisation.
* ``integrations``: the Slack workspace of an organisation, bot token encrypted. A
  workspace (team id) belongs to one organisation at most.
* ``slack_user_links``, ``slack_link_requests``, ``slack_interactions``: verified Slack
  user links, pending connect requests (token SHA-256 only), handled actions.

SECURITY DEFINER functions (owner role, flag-gated owner-only SELECT policies, the same
pattern as 0006), each returning the least possible:

* ``notification_due_org_ids()``: organisations with email or Slack messages due, or
  items held for a digest (the worker's delivery and digest jobs).
* ``slack_team_org_id(team_id)``: the organisation of an active Slack workspace.
* ``slack_link_request_org_id(token_hash)``: the organisation of a pending connect
  request.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import CURRENT_ORG_FN, app_db_role, ident, org_rls_statements

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEX64 = "'^[0-9a-f]{64}$'"
_SYSTEM_ACTOR = "pairly.migrations.0011"


def _pk() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _org(table: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(
        "org_id",
        sa.Uuid(),
        sa.ForeignKey(
            "organisations.id", ondelete="RESTRICT", name=f"fk_{table}_org_id_organisations"
        ),
        nullable=nullable,
    )


def _user_fk(table: str, column: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(
        column,
        sa.Uuid(),
        sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{table}_{column}_users"),
        nullable=nullable,
    )


def _ts(name: str, *, nullable: bool = True, default: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if default else None,
    )


def _templates(app_role: str) -> None:
    op.create_table(
        "templates",
        _pk(),
        _org("templates", nullable=True),
        sa.Column("key", sa.String(96), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("locale", sa.String(8), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("variables", postgresql.JSONB(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.String(128), nullable=False),
        _ts("created_at", nullable=False, default=True),
        sa.UniqueConstraint(
            "org_id",
            "key",
            "channel",
            "locale",
            "version",
            name="uq_templates_version",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            "channel IN ('email', 'slack', 'in_app')", name="ck_templates_channel_valid"
        ),
        sa.CheckConstraint("locale IN ('en', 'ar')", name="ck_templates_locale_valid"),
        sa.CheckConstraint("version >= 1", name="ck_templates_version_positive"),
        sa.CheckConstraint(
            r"key ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,4}$'", name="ck_templates_key_format"
        ),
        sa.CheckConstraint(
            "category ~ '^[a-z][a-z0-9_]{0,31}$'", name="ck_templates_category_format"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(variables) = 'object'", name="ck_templates_variables_object"
        ),
        sa.CheckConstraint(
            "created_by_type IN ('user', 'system')", name="ck_templates_created_by_valid"
        ),
    )
    op.create_index("ix_templates_org_id", "templates", ["org_id"])
    op.create_index("ix_templates_lookup", "templates", ["key", "channel", "locale", "version"])

    # System defaults, seeded before RLS is forced (as the shared roles of 0001).
    from pairly_core.notifications.defaults import SYSTEM_TEMPLATES
    from pairly_core.notifications.templating import (
        TemplateSource,
        declarations_json,
        lint_source,
        parse_declarations,
    )

    table = sa.table(
        "templates",
        sa.column("org_id", sa.Uuid()),
        sa.column("key", sa.String()),
        sa.column("channel", sa.String()),
        sa.column("locale", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("category", sa.String()),
        sa.column("subject", sa.Text()),
        sa.column("body", sa.Text()),
        sa.column("body_html", sa.Text()),
        sa.column("variables", postgresql.JSONB()),
        sa.column("requires_approval", sa.Boolean()),
        sa.column("created_by_type", sa.String()),
        sa.column("created_by_id", sa.String()),
    )
    rows = []
    for t in SYSTEM_TEMPLATES:
        specs = parse_declarations(t.variables)
        errors = [
            i
            for i in lint_source(
                TemplateSource(t.channel, t.locale, t.subject, t.body, t.body_html, specs)  # type: ignore[arg-type]
            )
            if i.severity == "error"
        ]
        if errors:
            raise RuntimeError(f"system template {t.key} ({t.channel}, {t.locale}) is invalid")
        rows.append(
            {
                "org_id": None,
                "key": t.key,
                "channel": t.channel,
                "locale": t.locale,
                "version": 1,
                "category": t.category,
                "subject": t.subject,
                "body": t.body,
                "body_html": t.body_html,
                "variables": declarations_json(specs),
                "requires_approval": t.requires_approval,
                "created_by_type": "system",
                "created_by_id": _SYSTEM_ACTOR,
            }
        )
    op.bulk_insert(table, rows)

    cur_org = CURRENT_ORG_FN
    op.execute("ALTER TABLE templates ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY templates_select ON templates FOR SELECT "
        f"USING (org_id IS NULL OR org_id = {cur_org})"
    )
    op.execute(
        f"CREATE POLICY templates_insert ON templates FOR INSERT WITH CHECK (org_id = {cur_org})"
    )
    op.execute("REVOKE ALL ON templates FROM PUBLIC")
    op.execute(f"REVOKE ALL ON templates FROM {app_role}")
    op.execute(f"GRANT SELECT, INSERT ON templates TO {app_role}")
    op.execute(
        """
        CREATE FUNCTION templates_immutable() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            RAISE EXCEPTION 'template versions are immutable: save a new version'
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER templates_immutable BEFORE UPDATE OR DELETE ON templates "
        "FOR EACH ROW EXECUTE FUNCTION templates_immutable()"
    )


def _notifications(app_role: str) -> None:
    op.create_table(
        "notifications",
        _pk(),
        _org("notifications"),
        _user_fk("notifications", "recipient_user_id", nullable=True),
        sa.Column("recipient_address", sa.Text(), nullable=True),
        sa.Column("recipient_bidx", sa.String(64), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("urgency", sa.String(8), nullable=False),
        sa.Column("template_key", sa.String(96), nullable=False),
        sa.Column(
            "template_id",
            sa.Uuid(),
            sa.ForeignKey(
                "templates.id", ondelete="RESTRICT", name="fk_notifications_template_id_templates"
            ),
            nullable=True,
        ),
        sa.Column("locale", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        sa.Column(
            "approval_id",
            sa.Uuid(),
            sa.ForeignKey(
                "approvals.id", ondelete="RESTRICT", name="fk_notifications_approval_id_approvals"
            ),
            nullable=True,
        ),
        sa.Column("workflow", sa.String(32), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        _ts("next_attempt_at"),
        sa.Column("provider_message_id", sa.String(256), nullable=True),
        sa.Column("digest_id", sa.Uuid(), nullable=True),
        _ts("created_at", nullable=False, default=True),
        _ts("sent_at"),
        _ts("read_at"),
        sa.UniqueConstraint("org_id", "dedupe_key", name="uq_notifications_dedupe"),
        sa.CheckConstraint(
            "channel IN ('email', 'slack', 'in_app')", name="ck_notifications_channel_valid"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'digest', 'sent', 'failed', 'suppressed')",
            name="ck_notifications_status_valid",
        ),
        sa.CheckConstraint(
            "urgency IN ('normal', 'urgent')", name="ck_notifications_urgency_valid"
        ),
        sa.CheckConstraint("locale IN ('en', 'ar')", name="ck_notifications_locale_valid"),
        sa.CheckConstraint("attempts >= 0", name="ck_notifications_attempts_positive"),
        sa.CheckConstraint(
            "recipient_user_id IS NOT NULL OR recipient_address IS NOT NULL",
            name="ck_notifications_has_recipient",
        ),
        sa.CheckConstraint(f"content_hash ~ {_HEX64}", name="ck_notifications_content_hash_format"),
    )
    op.create_index("ix_notifications_org_id", "notifications", ["org_id"])
    op.create_index("ix_notifications_due", "notifications", ["status", "next_attempt_at"])
    op.create_index(
        "ix_notifications_inbox", "notifications", ["recipient_user_id", "channel", "created_at"]
    )
    op.create_index(
        "ix_notifications_recipient_bidx",
        "notifications",
        ["recipient_bidx", "category", "created_at"],
    )
    op.create_index(
        "uq_notifications_approval",
        "notifications",
        ["approval_id"],
        unique=True,
        postgresql_where=sa.text("approval_id IS NOT NULL"),
    )
    for stmt in org_rls_statements("notifications", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    op.execute(
        "GRANT UPDATE (status, reason, attempts, next_attempt_at, provider_message_id, "
        f"digest_id, sent_at, read_at) ON notifications TO {app_role}"
    )


def _preferences(app_role: str) -> None:
    op.create_table(
        "notification_preferences",
        _pk(),
        _org("notification_preferences"),
        _user_fk("notification_preferences", "user_id"),
        sa.Column("locale", sa.String(8), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("channels", postgresql.JSONB(), nullable=False),
        sa.Column("digest", postgresql.JSONB(), nullable=False),
        _ts("snooze_until"),
        sa.Column("quiet_start", sa.Integer(), nullable=True),
        sa.Column("quiet_end", sa.Integer(), nullable=True),
        _ts("updated_at", nullable=False, default=True),
        sa.UniqueConstraint("org_id", "user_id", name="uq_notification_preferences_user"),
        sa.CheckConstraint(
            "locale IN ('en', 'ar')", name="ck_notification_preferences_locale_valid"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(channels) = 'object'", name="ck_notification_preferences_channels_object"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(digest) = 'object'", name="ck_notification_preferences_digest_object"
        ),
        sa.CheckConstraint(
            "(quiet_start IS NULL) = (quiet_end IS NULL)",
            name="ck_notification_preferences_quiet_hours_complete",
        ),
        sa.CheckConstraint(
            "quiet_start IS NULL OR (quiet_start BETWEEN 0 AND 1439 "
            "AND quiet_end BETWEEN 0 AND 1439)",
            name="ck_notification_preferences_quiet_hours_range",
        ),
    )
    op.create_index("ix_notification_preferences_org_id", "notification_preferences", ["org_id"])
    for stmt in org_rls_statements(
        "notification_preferences", app_role, privileges="SELECT, INSERT, UPDATE"
    ):
        op.execute(stmt)


def _slack(app_role: str) -> None:
    op.create_table(
        "integrations",
        _pk(),
        _org("integrations"),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("credential", sa.Text(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        _user_fk("integrations", "created_by"),
        _ts("created_at", nullable=False, default=True),
        _ts("updated_at", nullable=False, default=True),
        sa.UniqueConstraint("org_id", "provider", name="uq_integrations_provider"),
        sa.UniqueConstraint("provider", "external_id", name="uq_integrations_external"),
        sa.CheckConstraint("provider IN ('slack')", name="ck_integrations_provider_valid"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_integrations_status_valid"),
        sa.CheckConstraint(
            "jsonb_typeof(settings) = 'object'", name="ck_integrations_settings_object"
        ),
    )
    op.create_index("ix_integrations_org_id", "integrations", ["org_id"])
    for stmt in org_rls_statements("integrations", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    op.execute(
        "GRANT UPDATE (external_id, credential, settings, status, updated_at) "
        f"ON integrations TO {app_role}"
    )

    op.create_table(
        "slack_user_links",
        _pk(),
        _org("slack_user_links"),
        _user_fk("slack_user_links", "user_id"),
        sa.Column("team_id", sa.String(32), nullable=False),
        sa.Column("slack_user_id", sa.String(32), nullable=False),
        _ts("created_at", nullable=False, default=True),
        sa.UniqueConstraint("org_id", "user_id", name="uq_slack_user_links_user"),
        sa.UniqueConstraint("org_id", "team_id", "slack_user_id", name="uq_slack_user_links_slack"),
    )
    op.create_index("ix_slack_user_links_org_id", "slack_user_links", ["org_id"])
    for stmt in org_rls_statements(
        "slack_user_links", app_role, privileges="SELECT, INSERT, DELETE"
    ):
        op.execute(stmt)

    op.create_table(
        "slack_link_requests",
        _pk(),
        _org("slack_link_requests"),
        sa.Column("team_id", sa.String(32), nullable=False),
        sa.Column("slack_user_id", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        _ts("created_at", nullable=False),
        _ts("expires_at", nullable=False),
        _ts("used_at"),
        _user_fk("slack_link_requests", "used_by", nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_slack_link_requests_token_hash"),
        sa.CheckConstraint(
            f"token_hash ~ {_HEX64}", name="ck_slack_link_requests_token_hash_format"
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_slack_link_requests_expiry_after_creation"
        ),
    )
    op.create_index("ix_slack_link_requests_org_id", "slack_link_requests", ["org_id"])
    for stmt in org_rls_statements("slack_link_requests", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    op.execute(f"GRANT UPDATE (used_at, used_by) ON slack_link_requests TO {app_role}")

    op.create_table(
        "slack_interactions",
        _pk(),
        _org("slack_interactions"),
        sa.Column("team_id", sa.String(32), nullable=False),
        sa.Column("slack_user_id", sa.String(32), nullable=False),
        sa.Column("action_ts", sa.String(32), nullable=False),
        sa.Column("action_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        _ts("created_at", nullable=False, default=True),
        sa.UniqueConstraint(
            "org_id",
            "team_id",
            "slack_user_id",
            "action_ts",
            name="uq_slack_interactions_action",
        ),
    )
    op.create_index("ix_slack_interactions_org_id", "slack_interactions", ["org_id"])
    for stmt in org_rls_statements("slack_interactions", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)


def _definer_functions(app_role: str) -> None:
    op.execute(
        "CREATE POLICY notifications_worker_scan ON notifications FOR SELECT TO CURRENT_USER "
        "USING (current_setting('app.notification_scan', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION notification_due_org_ids() RETURNS SETOF uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.notification_scan', true);
            ids uuid[];
        BEGIN
            PERFORM set_config('app.notification_scan', 'on', true);
            SELECT array_agg(DISTINCT n.org_id ORDER BY n.org_id) INTO ids
              FROM public.notifications n
             WHERE (n.status = 'queued' AND n.channel IN ('email', 'slack')
                    AND n.next_attempt_at <= now())
                OR n.status = 'digest';
            PERFORM set_config('app.notification_scan', coalesce(previous, ''), true);
            RETURN QUERY SELECT unnest(coalesce(ids, '{}'::uuid[]));
        END
        $$
        """
    )
    op.execute(
        "CREATE POLICY integrations_team_lookup ON integrations FOR SELECT TO CURRENT_USER "
        "USING (current_setting('app.slack_lookup', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION slack_team_org_id(p_team_id text) RETURNS uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.slack_lookup', true);
            found uuid;
        BEGIN
            IF p_team_id IS NULL OR p_team_id !~ '^T[A-Z0-9]{2,30}$' THEN
                RETURN NULL;
            END IF;
            PERFORM set_config('app.slack_lookup', 'on', true);
            SELECT i.org_id INTO found
              FROM public.integrations i
             WHERE i.provider = 'slack' AND i.external_id = p_team_id AND i.status = 'active';
            PERFORM set_config('app.slack_lookup', coalesce(previous, ''), true);
            RETURN found;
        END
        $$
        """
    )
    op.execute(
        "CREATE POLICY slack_link_requests_token_lookup ON slack_link_requests FOR SELECT "
        "TO CURRENT_USER USING (current_setting('app.slack_lookup', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION slack_link_request_org_id(p_token_hash text) RETURNS uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.slack_lookup', true);
            found uuid;
        BEGIN
            IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$' THEN
                RETURN NULL;
            END IF;
            PERFORM set_config('app.slack_lookup', 'on', true);
            SELECT r.org_id INTO found
              FROM public.slack_link_requests r
             WHERE r.token_hash = p_token_hash
               AND r.used_at IS NULL
               AND r.expires_at > now();
            PERFORM set_config('app.slack_lookup', coalesce(previous, ''), true);
            RETURN found;
        END
        $$
        """
    )
    for fn in (
        "notification_due_org_ids()",
        "slack_team_org_id(text)",
        "slack_link_request_org_id(text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {app_role}")


def upgrade() -> None:
    app_role = ident(app_db_role())
    _templates(app_role)
    _notifications(app_role)
    _preferences(app_role)
    _slack(app_role)
    _definer_functions(app_role)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS slack_link_request_org_id(text)")
    op.execute("DROP FUNCTION IF EXISTS slack_team_org_id(text)")
    op.execute("DROP FUNCTION IF EXISTS notification_due_org_ids()")
    op.drop_table("slack_interactions")
    op.drop_table("slack_link_requests")
    op.drop_table("slack_user_links")
    op.drop_table("integrations")
    op.drop_table("notification_preferences")
    op.drop_table("notifications")
    op.drop_table("templates")
    op.execute("DROP FUNCTION IF EXISTS templates_immutable()")
