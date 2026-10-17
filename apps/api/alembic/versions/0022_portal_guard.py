# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Portal guard: core tables are closed to portal sessions (defence in depth).

A candidate (Hire) or a vendor's contact (Pay) signed in to a portal works in a
database session with ``app.portal_subject`` set (``pairly_core.db.set_portal_subject``):
``hire_candidate:<uuid>`` or ``pay_vendor:<uuid>``. The workflow plugins already add
restrictive policies on their own tables keyed on it. This revision adds the same
second layer to every org-scoped core table (and ``users``): a restrictive policy
``<table>_portal_guard`` under which, while a portal subject is set, the table shows no
rows and accepts no writes. A code path that queried a core table from a portal request
by mistake sees nothing.

Functions: ``app_portal_subject()`` (the subject or NULL), ``app_portal_active()``,
``app_portal_kind()`` (``hire_candidate``, ``pay_vendor``) and ``app_portal_id()`` (the
uuid of the subject).

Narrow exceptions (``portal_guard_rules``, owner only): a rule names a table, a kind of
portal person (or ``*`` for any) and SQL conditions for rows shown and rows written,
tied to the subject. ``portal_guard_apply(table)`` rebuilds the table's guard from its
rules, so each plugin adds its own (``pairly_core.migration_helpers``). Core's own:

* ``documents``: a new column ``portal_subject``, filled by the database from the
  setting on a portal upload; a portal person sees and writes only their own uploads.
* ``templates``: read only (the texts a portal flow's emails render), never written.
* ``webhook_outbox``: insert only (an upload emits ``document.received``), never read.

``audit_chain_head()`` reads the organisation's audit chain head (sequence and hash,
nothing else) with the portal subject cleared for that one lookup, and the chain link
trigger does the same, so an event a portal person causes continues the chain without
the portal ever reading the log.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import (
    PORTAL_ANY,
    app_db_role,
    portal_guard_statements,
    portal_rule_statements,
)

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every org-scoped core table (``org_id`` column); ``users`` is guarded too.
CORE_TABLES: tuple[str, ...] = (
    "api_keys",
    "appeals",
    "approval_decisions",
    "approval_delegations",
    "approval_links",
    "approval_steps",
    "approvals",
    "audit_denial_limits",
    "audit_events",
    "audit_verifications",
    "compliance_exports",
    "decision_records",
    "decision_replays",
    "documents",
    "extraction_corrections",
    "extraction_fields",
    "extractions",
    "feature_flags",
    "idempotency_keys",
    "inbound_addresses",
    "inbound_emails",
    "inbound_events",
    "integrations",
    "invitations",
    "llm_budget_alerts",
    "llm_budget_reservations",
    "memberships",
    "model_calls",
    "notification_preferences",
    "notifications",
    "org_llm_budgets",
    "policy_documents",
    "policy_versions",
    "retention_reports",
    "risk_assessments",
    "risk_signals",
    "roles",
    "rule_results",
    "search_chunks",
    "slack_interactions",
    "slack_link_requests",
    "slack_user_links",
    "templates",
    "webhook_deliveries",
    "webhook_endpoints",
    "webhook_outbox",
    "workflow_runs",
)

RULES: tuple[tuple[str, str | None, str | None], ...] = (
    ("documents", "portal_subject = app_portal_subject()", "portal_subject = app_portal_subject()"),
    ("templates", "true", None),
    ("webhook_outbox", None, "true"),
)


def _link_check(*, portal_aware: bool) -> str:
    """The audit chain link trigger (0002); portal aware: the head lookup clears the
    portal subject for itself, as ``audit_chain_head()`` does."""
    save = (
        "saved text := coalesce(current_setting('app.portal_subject', true), '');"
        if portal_aware
        else ""
    )
    clear = "PERFORM set_config('app.portal_subject', '', true);" if portal_aware else ""
    restore = "PERFORM set_config('app.portal_subject', saved, true);" if portal_aware else ""
    return f"""
        CREATE OR REPLACE FUNCTION audit_events_check_link() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            last_seq bigint;
            last_hash text;
            {save}
        BEGIN
            {clear}
            SELECT e.seq, e.hash INTO last_seq, last_hash
              FROM public.audit_events e
             WHERE e.org_id = NEW.org_id
             ORDER BY e.seq DESC
             LIMIT 1;
            {restore}
            IF last_seq IS NULL THEN
                IF NEW.seq <> 1 OR NEW.prev_hash <> repeat('0', 64) THEN
                    RAISE EXCEPTION 'audit chain: first event must be seq 1 with genesis hash'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSIF NEW.seq <> last_seq + 1 OR NEW.prev_hash <> last_hash THEN
                RAISE EXCEPTION 'audit chain: event does not continue the chain'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """  # noqa: S608 (constant SQL, never user input)


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION app_portal_subject() RETURNS text
        LANGUAGE sql STABLE
        AS $$ SELECT nullif(coalesce(current_setting('app.portal_subject', true), ''), '') $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_portal_active() RETURNS boolean
        LANGUAGE sql STABLE
        AS $$ SELECT app_portal_subject() IS NOT NULL $$
        """
    )
    # The kind (``hire_candidate``, ``pay_vendor``) and the id of the portal person.
    op.execute(
        """
        CREATE FUNCTION app_portal_kind() RETURNS text
        LANGUAGE sql STABLE
        AS $$ SELECT split_part(app_portal_subject(), ':', 1) $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_portal_id() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$
            SELECT CASE
                WHEN app_portal_subject() ~ '^[a-z][a-z0-9_]{0,31}:[0-9a-f-]{36}$'
                THEN split_part(app_portal_subject(), ':', 2)::uuid
            END
        $$
        """
    )
    # The current organisation's audit chain head (sequence and hash, nothing else),
    # read with the portal subject cleared for the one lookup, so an event a portal
    # person causes continues the chain. The link trigger reads the head the same way.
    op.execute(
        """
        CREATE FUNCTION audit_chain_head(OUT seq bigint, OUT hash text)
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            saved text := coalesce(current_setting('app.portal_subject', true), '');
        BEGIN
            PERFORM set_config('app.portal_subject', '', true);
            SELECT e.seq, e.hash INTO seq, hash
              FROM public.audit_events e
             WHERE e.org_id = public.app_current_org_id()
             ORDER BY e.seq DESC
             LIMIT 1;
            PERFORM set_config('app.portal_subject', saved, true);
        END
        $$
        """
    )
    op.execute(_link_check(portal_aware=True))
    _registry()
    op.add_column(
        "documents",
        sa.Column(
            "portal_subject",
            sa.String(80),
            nullable=True,
            server_default=sa.text("app_portal_subject()"),
        ),
    )
    for table in (*CORE_TABLES, "users"):
        for stmt in portal_guard_statements(table):
            op.execute(stmt)
    for table, using, check in RULES:
        for stmt in portal_rule_statements(table, PORTAL_ANY, using=using, check=check):
            op.execute(stmt)


def _registry() -> None:
    op.create_table(
        "portal_guard_rules",
        sa.Column("table_name", sa.String(63), primary_key=True),
        sa.Column("subject_kind", sa.String(32), primary_key=True),
        sa.Column("using_sql", sa.Text(), nullable=True),
        sa.Column("check_sql", sa.Text(), nullable=True),
    )
    op.execute("REVOKE ALL ON portal_guard_rules FROM PUBLIC")
    op.execute(f"REVOKE ALL ON portal_guard_rules FROM {app_db_role()}")
    # Rebuild one table's guard from its rules. Run by migrations (the owner) only.
    op.execute(
        """
        CREATE FUNCTION portal_guard_apply(target text) RETURNS void
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            shown text := '';
            written text := '';
            scope text;
            r record;
        BEGIN
            IF target !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
                RAISE EXCEPTION 'invalid table name %', target;
            END IF;
            FOR r IN
                SELECT * FROM public.portal_guard_rules
                 WHERE table_name = target ORDER BY subject_kind
            LOOP
                scope := CASE WHEN r.subject_kind = '*' THEN 'true'
                    ELSE format('app_portal_kind() = %L', r.subject_kind) END;
                IF r.using_sql IS NOT NULL THEN
                    shown := shown || format(' OR (%s AND (%s))', scope, r.using_sql);
                END IF;
                IF r.check_sql IS NOT NULL THEN
                    written := written || format(' OR (%s AND (%s))', scope, r.check_sql);
                END IF;
            END LOOP;
            EXECUTE format('DROP POLICY IF EXISTS %I ON %I', target || '_portal_guard', target);
            EXECUTE format(
                'CREATE POLICY %I ON %I AS RESTRICTIVE '
                'USING (NOT app_portal_active()%s) WITH CHECK (NOT app_portal_active()%s)',
                target || '_portal_guard', target, shown, written
            );
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION portal_guard_apply(text) FROM PUBLIC")


def downgrade() -> None:
    op.execute(_link_check(portal_aware=False))
    op.execute("DROP FUNCTION IF EXISTS audit_chain_head()")
    for table in reversed((*CORE_TABLES, "users")):
        op.execute(f"DROP POLICY IF EXISTS {table}_portal_guard ON {table}")
    op.drop_column("documents", "portal_subject")
    op.execute("DROP FUNCTION IF EXISTS portal_guard_apply(text)")
    op.drop_table("portal_guard_rules")
    op.execute("DROP FUNCTION IF EXISTS app_portal_id()")
    op.execute("DROP FUNCTION IF EXISTS app_portal_kind()")
    op.execute("DROP FUNCTION IF EXISTS app_portal_active()")
    op.execute("DROP FUNCTION IF EXISTS app_portal_subject()")
