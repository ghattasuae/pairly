# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Worker enumeration for the batch URL sweeper (M6).

``batch_stale_org_ids(minutes)`` returns the ids of organisations with a URL batch
item still ``pending`` in a batch created more than ``minutes`` ago (the API process
that was fetching it stopped). Ids only. Same pattern as 0012: SECURITY DEFINER, owned
by the migration role, it switches ``app.batch_scan`` on for its own query, and a SELECT
policy limited to the owner role admits rows while the flag is on. The worker then works
in each organisation's own tenant-bound session.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

from pairly_core.migration_helpers import app_db_role

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCAN_FLAG = "app.batch_scan"
_FUNCTION = "batch_stale_org_ids(integer)"
_TABLES = ("document_batches", "document_batch_items")


def upgrade() -> None:
    role = app_db_role()
    for table in _TABLES:
        op.execute(
            f"CREATE POLICY {table}_worker_scan ON {table} FOR SELECT TO CURRENT_USER "
            f"USING (current_setting('{_SCAN_FLAG}', true) = 'on')"
        )
    # The flag is spelled out (not formatted in): app.batch_scan, as _SCAN_FLAG.
    op.execute(
        """
        CREATE FUNCTION batch_stale_org_ids(minutes integer) RETURNS SETOF uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.batch_scan', true);
            ids uuid[];
        BEGIN
            IF minutes IS NULL OR minutes < 1 THEN
                RAISE EXCEPTION 'minutes must be at least 1';
            END IF;
            PERFORM set_config('app.batch_scan', 'on', true);
            SELECT array_agg(DISTINCT i.org_id ORDER BY i.org_id) INTO ids
            FROM public.document_batch_items i
            JOIN public.document_batches b ON b.id = i.batch_id
            WHERE i.status = 'pending' AND i.source = 'url'
              AND b.created_at < now() - make_interval(mins => minutes);
            PERFORM set_config('app.batch_scan', coalesce(previous, ''), true);
            RETURN QUERY SELECT unnest(coalesce(ids, '{}'::uuid[]));
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNCTION} TO {role}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}")
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_worker_scan ON {table}")
