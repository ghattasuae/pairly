# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Stuck notes: the ``needs_review`` status, a start attempt counter, and the org finder.

The ``expense_stuck_note_sweep`` job (``pairly_expense.sweeper``) looks for notes still
``processing`` long after they were submitted (the run never started, failed or
stalled). It finds the organisations to visit through
``expense_stuck_note_org_ids(minutes)``, a SECURITY DEFINER function that returns
organisation ids only (the pattern of migration 0006), and then works in each
organisation under normal Row Level Security as the application role.

* ``expense_notes.start_attempts``: how many times the sweeper restarted the run (once
  at most).
* ``needs_review`` status: the run could not get the note to a person's decision; a
  reviewer looks at it with the reason in ``failure_reason``.

Revision ID: expense_0002
Revises: expense_0001
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import app_db_role

revision: str = "expense_0002"
down_revision: str | None = "expense_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'processing', 'awaiting_approval', 'approved', 'rejected', 'changes_requested', 'failed'"
_NEW = _OLD + ", 'needs_review'"


def upgrade() -> None:
    app_role = app_db_role()
    op.add_column(
        "expense_notes",
        sa.Column("start_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_constraint("ck_expense_notes_status_valid", "expense_notes", type_="check")
    op.create_check_constraint(
        "ck_expense_notes_status_valid", "expense_notes", f"status IN ({_NEW})"
    )
    op.execute(
        "CREATE POLICY expense_notes_stuck_scan ON expense_notes FOR SELECT TO CURRENT_USER "
        "USING (current_setting('app.expense_stuck_scan', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION expense_stuck_note_org_ids(p_minutes integer) RETURNS SETOF uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.expense_stuck_scan', true);
            ids uuid[];
        BEGIN
            IF p_minutes IS NULL OR p_minutes < 1 THEN
                RETURN;
            END IF;
            PERFORM set_config('app.expense_stuck_scan', 'on', true);
            SELECT array_agg(DISTINCT n.org_id ORDER BY n.org_id) INTO ids
              FROM public.expense_notes n
             WHERE n.status = 'processing'
               AND n.updated_at < now() - make_interval(mins => p_minutes);
            PERFORM set_config('app.expense_stuck_scan', coalesce(previous, ''), true);
            RETURN QUERY SELECT unnest(coalesce(ids, '{}'::uuid[]));
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION expense_stuck_note_org_ids(integer) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION expense_stuck_note_org_ids(integer) TO {app_role}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS expense_stuck_note_org_ids(integer)")
    op.execute("DROP POLICY IF EXISTS expense_notes_stuck_scan ON expense_notes")
    op.execute("ALTER TABLE expense_notes NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE expense_notes SET status = 'failed' WHERE status = 'needs_review'")
    op.execute("ALTER TABLE expense_notes FORCE ROW LEVEL SECURITY")
    op.drop_constraint("ck_expense_notes_status_valid", "expense_notes", type_="check")
    op.create_check_constraint(
        "ck_expense_notes_status_valid", "expense_notes", f"status IN ({_OLD})"
    )
    op.drop_column("expense_notes", "start_attempts")
