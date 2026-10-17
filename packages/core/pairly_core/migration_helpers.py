# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""SQL building blocks for Alembic migrations (core and workflow plugins).

Every org-scoped table must call ``org_rls_statements`` in the migration that creates
it. That enables and forces Row Level Security, adds the tenant policy keyed on
``app_current_org_id()`` (NULL when no org is set, so no rows match) and grants the
application role DML only. The application role never owns a table.
"""

from __future__ import annotations

import os
import re

APP_ROLE_ENV = "PAIRLY_DB_APP_ROLE"
DEFAULT_APP_ROLE = "pairly_app"
CURRENT_ORG_FN = "app_current_org_id()"
CURRENT_SUBJECT_FN = "app_current_subject()"

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def ident(name: str) -> str:
    """Validate a SQL identifier we interpolate into DDL (never user input)."""
    if not _IDENT_RE.fullmatch(name):
        raise ValueError(f"invalid SQL identifier {name!r}")
    return name


def app_db_role() -> str:
    """Name of the non-owner application role that receives grants."""
    return ident(os.environ.get(APP_ROLE_ENV, DEFAULT_APP_ROLE))


def org_rls_statements(
    table: str,
    app_role: str,
    *,
    privileges: str = "SELECT, INSERT, UPDATE, DELETE",
) -> list[str]:
    """Standard RLS setup for a table with a non-null ``org_id`` column."""
    t = ident(table)
    role = ident(app_role)
    if not re.fullmatch(r"[A-Z, ]+", privileges):
        raise ValueError("privileges must be a plain SQL privilege list")
    return [
        f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {t}_tenant_isolation ON {t} "
        f"USING (org_id = {CURRENT_ORG_FN}) WITH CHECK (org_id = {CURRENT_ORG_FN})",
        # Revoke first: default privileges may have granted more than asked for.
        f"REVOKE ALL ON {t} FROM PUBLIC",
        f"REVOKE ALL ON {t} FROM {role}",
        f"GRANT {privileges} ON {t} TO {role}",
    ]


PORTAL_ACTIVE_FN = "app_portal_active()"
PORTAL_ANY = "*"


PORTAL_COMMANDS: tuple[str, ...] = ("SELECT", "INSERT", "UPDATE", "DELETE")


def portal_guard_statements(table: str) -> list[str]:
    """Close an org-scoped table to portal sessions (core migrations 0022 and 0023).

    While a portal subject is set (``app.portal_subject``: a candidate or a vendor
    contact signed in to a portal) the table shows no rows and accepts no writes, except
    what the rules of ``portal_rule_statements`` allow, command by command (restrictive
    policies ``<table>_portal_guard_select``, ``_insert``, ``_update``, ``_delete``).
    Staff, agents and jobs never set a portal subject, so the guard changes nothing for
    them. Call it in the migration that creates any org-scoped table that has no portal
    policy of its own.
    """
    return [f"SELECT portal_guard_apply('{ident(table)}')"]


def drop_portal_guard_statements(table: str) -> list[str]:
    """Remove a table's portal guard (downgrades), whichever shape it has: the single
    policy of migration 0022 or the per-command policies of 0023."""
    t = ident(table)
    return [
        f"DROP POLICY IF EXISTS {t}_portal_guard ON {t}",
        *(f"DROP POLICY IF EXISTS {t}_portal_guard_{c.lower()} ON {t}" for c in PORTAL_COMMANDS),
    ]


def portal_rule_statements(
    table: str,
    subject_kind: str,
    *,
    using: str | None = None,
    check: str | None = None,
    commands: tuple[str, ...] | None = None,
) -> list[str]:
    """A narrow exception to a table's portal guard, for one kind of portal person
    (``hire_candidate``, ``pay_vendor``; ``PORTAL_ANY`` for every kind). ``using`` (rows
    shown, and rows an UPDATE or DELETE may touch) and ``check`` (rows written) are SQL
    conditions tied to the subject (``app_portal_id()``, a plugin's own subject
    function); ``None`` allows nothing.

    ``commands`` names what the rule allows, a subset of ``PORTAL_COMMANDS``: SELECT and
    DELETE need ``using``, INSERT needs ``check``, UPDATE needs both. ``None`` is the
    shape of the rules written before migration 0023 (the column did not exist yet):
    the database then derives SELECT when ``using`` is set and INSERT when ``check`` is
    set, never UPDATE or DELETE.

    The rules live in ``portal_guard_rules`` and the table's guard policies are rebuilt
    from all of them, so plugins add their own without touching each other's."""
    t = ident(table)
    if subject_kind != PORTAL_ANY:
        ident(subject_kind)
    for part in (using, check):
        if part is not None and "$rule$" in part:
            raise ValueError("a portal rule cannot contain the $rule$ quote")

    def quoted(part: str | None) -> str:
        return "NULL" if part is None else f"$rule${part}$rule$"

    columns = "table_name, subject_kind, using_sql, check_sql"
    values = f"'{t}', '{subject_kind}', {quoted(using)}, {quoted(check)}"
    updates = "using_sql = EXCLUDED.using_sql, check_sql = EXCLUDED.check_sql"
    if commands is not None:
        named = tuple(dict.fromkeys(c.upper() for c in commands))
        if not named or not set(named) <= set(PORTAL_COMMANDS):
            raise ValueError(f"portal rule commands must be a subset of {PORTAL_COMMANDS}")
        if {"SELECT", "UPDATE", "DELETE"} & set(named) and using is None:
            raise ValueError("SELECT, UPDATE and DELETE need a using condition")
        if {"INSERT", "UPDATE"} & set(named) and check is None:
            raise ValueError("INSERT and UPDATE need a check condition")
        columns += ", commands"
        values += ", ARRAY[" + ", ".join(f"'{c}'" for c in named) + "]::text[]"
        updates += ", commands = EXCLUDED.commands"
    return [
        f"INSERT INTO portal_guard_rules ({columns}) "  # noqa: S608 (constant SQL, never user input)
        f"VALUES ({values}) "
        f"ON CONFLICT (table_name, subject_kind) DO UPDATE SET {updates}",
        f"SELECT portal_guard_apply('{t}')",
    ]


def drop_portal_rule_statements(table: str, subject_kind: str) -> list[str]:
    """Undo ``portal_rule_statements`` (downgrades)."""
    t = ident(table)
    if subject_kind != PORTAL_ANY:
        ident(subject_kind)
    return [
        f"DELETE FROM portal_guard_rules WHERE table_name = '{t}' "  # noqa: S608 (constant SQL, never user input)
        f"AND subject_kind = '{subject_kind}'",
        f"SELECT portal_guard_apply('{t}')",
    ]
