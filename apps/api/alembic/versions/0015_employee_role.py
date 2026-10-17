# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The ``employee`` built-in role (a shared row, ``org_id`` NULL).

A person who hands work in (an expense note) and follows it. It holds no core
permission; workflow plugins grant it read, submit, create and upload permissions only
(``pairly_core.rbac.EMPLOYEE_ALLOWED_ACTIONS``). Assignable by invitation like the other
human roles.

``roles`` forces Row Level Security and its insert policy only accepts the tenant's own
rows, so the owner lifts FORCE for this insert only (as migration 0001 seeded the first
built-in roles before forcing). Idempotent. Downgrade removes the row when no membership
or invitation uses it.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMPLOYEE_ROLE_ID = "00000000-0000-4000-8000-000000000010"


def upgrade() -> None:
    op.execute("ALTER TABLE roles NO FORCE ROW LEVEL SECURITY")
    op.execute(
        sa.text(
            "INSERT INTO roles (id, org_id, key, name) "
            "SELECT CAST(:id AS uuid), NULL, 'employee', 'Employee' "
            "WHERE NOT EXISTS (SELECT 1 FROM roles WHERE org_id IS NULL AND key = 'employee')"
        ).bindparams(id=EMPLOYEE_ROLE_ID)
    )
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE roles NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memberships NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE invitations NO FORCE ROW LEVEL SECURITY")
    op.execute(
        sa.text(
            "DELETE FROM roles r WHERE r.id = CAST(:id AS uuid) "
            "AND NOT EXISTS (SELECT 1 FROM memberships m WHERE m.role_id = r.id) "
            "AND NOT EXISTS (SELECT 1 FROM invitations i WHERE i.role_id = r.id)"
        ).bindparams(id=EMPLOYEE_ROLE_ID)
    )
    op.execute("ALTER TABLE invitations FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memberships FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY")
