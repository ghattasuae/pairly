# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Expense notes are closed to portal sessions (core portal guard, migration 0022).

No portal reads or writes expense notes: while a portal subject is set
(``app.portal_subject``), ``expense_notes`` shows no rows and accepts no writes.

Revision ID: expense_0004
Revises: expense_0003
Create Date: 2026-10-14
"""

from collections.abc import Sequence

from alembic import op

from pairly_core.migration_helpers import drop_portal_guard_statements, portal_guard_statements

revision: str = "expense_0004"
down_revision: str | None = "expense_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = "0022"


def upgrade() -> None:
    for stmt in portal_guard_statements("expense_notes"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in drop_portal_guard_statements("expense_notes"):
        op.execute(stmt)
