# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Platform operations: per-tenant usage counts reused model calls apart from paid ones.

``ops_tenant_usage(since)`` (0027) gains ``reused_calls``: the model calls since
``since`` that reused an earlier answer (``model_calls.reused_from``, 0029: no model
request, zero cost), the way the AI governance board counts them. A function's result
columns cannot change in place, so it is dropped and created again with the same
SECURITY DEFINER pattern and grants. Downgrade restores 0027's function.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

from pairly_core.migration_helpers import app_db_role

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION = "ops_tenant_usage(timestamptz)"

# The flag is spelled out in the SQL below (app.ops_scan), never formatted in.
_HEAD = """
CREATE FUNCTION ops_tenant_usage(since timestamptz)
RETURNS TABLE (
    org_id uuid, org_slug text, sandbox boolean, members bigint, documents bigint,
    storage_bytes bigint, model_calls bigint, cost_micros bigint{extra_column}
)
LANGUAGE plpgsql VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    previous text := current_setting('app.ops_scan', true);
BEGIN
    IF since IS NULL THEN
        RAISE EXCEPTION 'since is required';
    END IF;
    PERFORM set_config('app.ops_scan', 'on', true);
    RETURN QUERY
    SELECT o.id, o.slug::text, o.sandbox,
        (SELECT count(*) FROM public.memberships m WHERE m.org_id = o.id),
        (SELECT count(*) FROM public.documents d WHERE d.org_id = o.id),
        (SELECT coalesce(sum(d.size_bytes), 0)::bigint FROM public.documents d
            WHERE d.org_id = o.id),
        (SELECT count(*) FROM public.model_calls c
            WHERE c.org_id = o.id AND c.created_at >= since),
        (SELECT coalesce(sum(c.cost_micros), 0)::bigint FROM public.model_calls c
            WHERE c.org_id = o.id AND c.created_at >= since){extra_value}
    FROM public.organisations o
    ORDER BY o.slug;
    PERFORM set_config('app.ops_scan', coalesce(previous, ''), true);
END
$$
"""
_REUSED = """,
        (SELECT count(*) FROM public.model_calls c
            WHERE c.org_id = o.id AND c.created_at >= since AND c.reused_from IS NOT NULL)"""


def _replace(sql: str) -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}")
    op.execute(sql)
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNCTION} TO {app_db_role()}")


def upgrade() -> None:
    _replace(_HEAD.format(extra_column=", reused_calls bigint", extra_value=_REUSED))


def downgrade() -> None:
    _replace(_HEAD.format(extra_column="", extra_value=""))
