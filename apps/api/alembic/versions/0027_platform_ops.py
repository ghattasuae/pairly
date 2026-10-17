# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Platform operations aggregates (M6, brief 5.1 item 15).

Two SECURITY DEFINER functions, owned by the migration role, that return numbers only,
never tenant content (no names, no file names, no URLs, no payloads):

* ``ops_tenant_usage(since)``: per organisation, its id, slug and sandbox flag, members,
  documents, stored bytes, and model calls and their cost since ``since``.
* ``ops_webhook_summary()``: webhook deliveries of every organisation by status, how
  many needed more than one attempt, the oldest pending one and failures of the last
  24 hours.

Same pattern as 0012 and 0026: each switches ``app.ops_scan`` on for its own query, and
a SELECT policy limited to the owner role admits rows while the flag is on. The API
calls them only from the platform operations routes, which only admins of the
deployment's system organisation reach (``PAIRLY_SYSTEM_ORG_ID``).

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

from pairly_core.migration_helpers import app_db_role

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("organisations", "memberships", "documents", "model_calls", "webhook_deliveries")
_FUNCTIONS = ("ops_tenant_usage(timestamptz)", "ops_webhook_summary()")

# The flag is spelled out in the SQL below (app.ops_scan), never formatted in.
_USAGE = """
CREATE FUNCTION ops_tenant_usage(since timestamptz)
RETURNS TABLE (
    org_id uuid, org_slug text, sandbox boolean, members bigint, documents bigint,
    storage_bytes bigint, model_calls bigint, cost_micros bigint
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
            WHERE c.org_id = o.id AND c.created_at >= since)
    FROM public.organisations o
    ORDER BY o.slug;
    PERFORM set_config('app.ops_scan', coalesce(previous, ''), true);
END
$$
"""

_WEBHOOKS = """
CREATE FUNCTION ops_webhook_summary()
RETURNS TABLE (
    status text, deliveries bigint, retried bigint, oldest_pending timestamptz,
    failed_24h bigint
)
LANGUAGE plpgsql VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    previous text := current_setting('app.ops_scan', true);
BEGIN
    PERFORM set_config('app.ops_scan', 'on', true);
    RETURN QUERY
    SELECT w.status::text, count(*), count(*) FILTER (WHERE w.attempts > 1),
        min(w.next_attempt_at) FILTER (WHERE w.status = 'pending'),
        count(*) FILTER (
            WHERE w.status = 'failed' AND w.last_attempt_at >= now() - interval '24 hours'
        )
    FROM public.webhook_deliveries w
    GROUP BY w.status
    ORDER BY w.status;
    PERFORM set_config('app.ops_scan', coalesce(previous, ''), true);
END
$$
"""


def upgrade() -> None:
    role = app_db_role()
    for table in _TABLES:
        op.execute(
            f"CREATE POLICY {table}_ops_scan ON {table} FOR SELECT TO CURRENT_USER "
            "USING (current_setting('app.ops_scan', true) = 'on')"
        )
    op.execute(_USAGE)
    op.execute(_WEBHOOKS)
    for fn in _FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {role}")


def downgrade() -> None:
    for fn in _FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_ops_scan ON {table}")
