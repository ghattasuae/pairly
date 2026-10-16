# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire: complete erasure of a candidate's personal data (``pairly_hire.erasure``).

* ``hire_candidates.erased_at``: when the candidate's data was erased. The row stays (ids
  and counts), with placeholder name and address.
* ``hire_data_requests``: ``erasure_started_at`` (the database part committed),
  ``erasure_error`` (why the stored files could not be removed yet, shown to the admin)
  and ``erasure_attempts``. A deletion request stays open until the files are gone.
* The application role may now clear what the trail does not need: a note's text
  (``hire_notes.body``) and a stage move's reason (``hire_stage_moves.reason``), both
  otherwise append-only, and DELETE the candidate's self-identification answers and
  portal sign-in links and sessions.

Revision ID: hire_0007
Revises: hire_0006
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import app_db_role

revision: str = "hire_0007"
down_revision: str | None = "hire_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = "0019"

_GRANTS = (
    "UPDATE (body) ON hire_notes",
    "UPDATE (reason) ON hire_stage_moves",
    "DELETE ON hire_self_identification",
    "DELETE ON hire_portal_tokens",
    "DELETE ON hire_portal_sessions",
)


def upgrade() -> None:
    app_role = app_db_role()
    op.add_column(
        "hire_candidates", sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "hire_data_requests",
        sa.Column("erasure_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("hire_data_requests", sa.Column("erasure_error", sa.String(64), nullable=True))
    op.add_column(
        "hire_data_requests",
        sa.Column("erasure_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_hire_data_requests_erasure_pending",
        "hire_data_requests",
        ["org_id", "erasure_started_at"],
        postgresql_where=sa.text("status = 'open' AND erasure_started_at IS NOT NULL"),
    )
    for grant in _GRANTS:
        op.execute(f"GRANT {grant} TO {app_role}")
    # Worker enumeration for the retry job: organisation ids only (same pattern as
    # ``hire_pipeline_org_ids``).
    op.execute(
        "CREATE POLICY hire_data_requests_erasure_scan ON hire_data_requests FOR SELECT "
        "TO CURRENT_USER USING (current_setting('app.hire_erasure_scan', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION hire_erasure_pending_org_ids() RETURNS SETOF uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.hire_erasure_scan', true);
            ids uuid[];
        BEGIN
            PERFORM set_config('app.hire_erasure_scan', 'on', true);
            SELECT array_agg(DISTINCT r.org_id ORDER BY r.org_id) INTO ids
              FROM public.hire_data_requests r
             WHERE r.status = 'open' AND r.erasure_started_at IS NOT NULL;
            PERFORM set_config('app.hire_erasure_scan', coalesce(previous, ''), true);
            RETURN QUERY SELECT unnest(coalesce(ids, '{}'::uuid[]));
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION hire_erasure_pending_org_ids() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION hire_erasure_pending_org_ids() TO {app_role}")


def downgrade() -> None:
    app_role = app_db_role()
    op.execute("DROP FUNCTION IF EXISTS hire_erasure_pending_org_ids()")
    op.execute("DROP POLICY IF EXISTS hire_data_requests_erasure_scan ON hire_data_requests")
    for grant in _GRANTS:
        op.execute(f"REVOKE {grant} FROM {app_role}")
    op.drop_index("ix_hire_data_requests_erasure_pending", table_name="hire_data_requests")
    op.drop_column("hire_data_requests", "erasure_attempts")
    op.drop_column("hire_data_requests", "erasure_error")
    op.drop_column("hire_data_requests", "erasure_started_at")
    op.drop_column("hire_candidates", "erased_at")
