# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The ``closed`` status: a reviewer closed a note that needed review.

Reviewers act on ``needs_review`` notes (``POST /v1/expense/notes/{id}/close`` and
``/restart``). Closing ends the note with a written reason; restarting sends it back to
``processing`` with a fresh run, which needs no new status.

Revision ID: expense_0003
Revises: expense_0002
Create Date: 2026-10-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "expense_0003"
down_revision: str | None = "expense_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = (
    "'processing', 'awaiting_approval', 'approved', 'rejected', 'changes_requested', "
    "'failed', 'needs_review'"
)
_NEW = _OLD + ", 'closed'"


def upgrade() -> None:
    op.drop_constraint("ck_expense_notes_status_valid", "expense_notes", type_="check")
    op.create_check_constraint(
        "ck_expense_notes_status_valid", "expense_notes", f"status IN ({_NEW})"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE expense_notes NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE expense_notes SET status = 'failed' WHERE status = 'closed'")
    op.execute("ALTER TABLE expense_notes FORCE ROW LEVEL SECURITY")
    op.drop_constraint("ck_expense_notes_status_valid", "expense_notes", type_="check")
    op.create_check_constraint(
        "ck_expense_notes_status_valid", "expense_notes", f"status IN ({_OLD})"
    )
