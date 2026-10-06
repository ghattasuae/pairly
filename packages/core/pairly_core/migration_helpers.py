# SPDX-License-Identifier: AGPL-3.0-only
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
