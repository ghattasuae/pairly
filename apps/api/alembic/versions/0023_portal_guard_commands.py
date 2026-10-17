# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Portal guard per command: each exception names the commands it allows.

Migration 0022 built one restrictive policy per table (``<table>_portal_guard``, for
every command) from the table's ``portal_guard_rules``. A rule's ``using_sql`` (rows
shown) and ``check_sql`` (rows written) then also allowed UPDATE of a row that passed
both: a portal person could change the document rows they had uploaded.

This revision gives every rule the list of commands it allows (``commands``: a subset
of SELECT, INSERT, UPDATE, DELETE) and ``portal_guard_apply(table)`` builds four
restrictive policies from them, ``<table>_portal_guard_select``, ``_insert``,
``_update`` and ``_delete``. A command no rule of the table names is closed to portal
sessions: SELECT, UPDATE and DELETE see no row, INSERT is refused.

Existing rules are given the commands they were written for: SELECT when they show
rows, INSERT when they accept writes, never UPDATE or DELETE. So ``documents`` is
insert and read only for portal sessions (their own uploads), ``templates`` read
only, ``webhook_outbox`` insert only. A rule inserted without commands (a plugin
migration that predates this revision and runs after it) gets the same derived list
from a trigger, so ``commands`` is never empty of meaning.

``portal_guard_apply`` stays the single builder; plugins keep calling it through
``pairly_core.migration_helpers``.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DERIVED = (
    "array_remove(ARRAY["
    "CASE WHEN {using} IS NOT NULL THEN 'SELECT' END, "
    "CASE WHEN {check} IS NOT NULL THEN 'INSERT' END]::text[], NULL)"
)

_APPLY_PER_COMMAND = """
CREATE OR REPLACE FUNCTION portal_guard_apply(target text) RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    cmd text;
    shown text;
    written text;
    scope text;
    r record;
BEGIN
    IF target !~ '^[a-z_][a-z0-9_]{0,42}$' THEN
        RAISE EXCEPTION 'invalid table name %', target;
    END IF;
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', target || '_portal_guard', target);
    FOREACH cmd IN ARRAY ARRAY['select', 'insert', 'update', 'delete'] LOOP
        shown := '';
        written := '';
        FOR r IN
            SELECT * FROM public.portal_guard_rules
             WHERE table_name = target AND upper(cmd) = ANY (commands)
             ORDER BY subject_kind
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
        EXECUTE format(
            'DROP POLICY IF EXISTS %I ON %I', target || '_portal_guard_' || cmd, target
        );
        IF cmd = 'insert' THEN
            EXECUTE format(
                'CREATE POLICY %I ON %I AS RESTRICTIVE FOR INSERT '
                'WITH CHECK (NOT app_portal_active()%s)',
                target || '_portal_guard_' || cmd, target, written
            );
        ELSIF cmd = 'update' THEN
            EXECUTE format(
                'CREATE POLICY %I ON %I AS RESTRICTIVE FOR UPDATE '
                'USING (NOT app_portal_active()%s) WITH CHECK (NOT app_portal_active()%s)',
                target || '_portal_guard_' || cmd, target, shown, written
            );
        ELSE
            EXECUTE format(
                'CREATE POLICY %I ON %I AS RESTRICTIVE FOR %s '
                'USING (NOT app_portal_active()%s)',
                target || '_portal_guard_' || cmd, target, upper(cmd), shown
            );
        END IF;
    END LOOP;
END
$$
"""

# Migration 0022's builder: one policy for every command.
_APPLY_SINGLE = """
CREATE OR REPLACE FUNCTION portal_guard_apply(target text) RETURNS void
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

_COMMANDS = ("select", "insert", "update", "delete")


def _guarded(suffix: str) -> list[str]:
    """Tables carrying a guard policy named ``<table><suffix>``."""
    rows = op.get_bind().execute(
        sa.text(
            "SELECT tablename FROM pg_policies WHERE schemaname = 'public' "
            "AND policyname = tablename || :suffix ORDER BY 1"
        ),
        {"suffix": suffix},
    )
    return [str(r[0]) for r in rows]


def upgrade() -> None:
    op.add_column(
        "portal_guard_rules",
        sa.Column("commands", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.execute(
        "UPDATE portal_guard_rules SET commands = "  # noqa: S608 (constant SQL, never user input)
        + _DERIVED.format(using="using_sql", check="check_sql")
    )
    op.alter_column("portal_guard_rules", "commands", nullable=False)
    op.create_check_constraint(
        "ck_portal_guard_rules_commands_valid",
        "portal_guard_rules",
        "commands <@ ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']::text[]",
    )
    # A rule written without commands (a plugin migration older than this revision)
    # gets the commands it was written for: SELECT when it shows rows, INSERT when it
    # accepts writes; never UPDATE or DELETE.
    op.execute(
        f"""
        CREATE FUNCTION portal_guard_rules_default_commands() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NEW.commands IS NULL THEN
                NEW.commands := {_DERIVED.format(using="NEW.using_sql", check="NEW.check_sql")};
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER portal_guard_rules_default_commands BEFORE INSERT OR UPDATE "
        "ON portal_guard_rules FOR EACH ROW EXECUTE FUNCTION "
        "portal_guard_rules_default_commands()"
    )
    op.execute(_APPLY_PER_COMMAND)
    op.execute("REVOKE ALL ON FUNCTION portal_guard_apply(text) FROM PUBLIC")
    for table in _guarded("_portal_guard"):
        op.execute(f"SELECT portal_guard_apply('{table}')")


def downgrade() -> None:
    op.execute(_APPLY_SINGLE)
    op.execute("REVOKE ALL ON FUNCTION portal_guard_apply(text) FROM PUBLIC")
    for table in _guarded("_portal_guard_select"):
        for cmd in _COMMANDS:
            op.execute(f"DROP POLICY IF EXISTS {table}_portal_guard_{cmd} ON {table}")
        op.execute(f"SELECT portal_guard_apply('{table}')")
    op.execute("DROP TRIGGER IF EXISTS portal_guard_rules_default_commands ON portal_guard_rules")
    op.execute("DROP FUNCTION IF EXISTS portal_guard_rules_default_commands()")
    op.drop_constraint("ck_portal_guard_rules_commands_valid", "portal_guard_rules", type_="check")
    op.drop_column("portal_guard_rules", "commands")
