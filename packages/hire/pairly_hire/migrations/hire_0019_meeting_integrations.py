# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire meeting-link integrations (M3, brief 3.6.4): Zoom.

* ``hire_meeting_integrations`` (``pairly_hire.scheduling.models``): one row per
  organisation and provider (``zoom``), the Server-to-Server OAuth app's account id,
  client id, client secret and host user encrypted. Tenant table, RLS forced, standard
  tenant policy, the application role gets SELECT, INSERT and UPDATE (never DELETE),
  hidden from a candidate's portal session.
* ``hire_interviews.meeting_external_id``: the meeting provider's own id (a Zoom
  meeting), to move or delete the meeting with the interview.

Revision ID: hire_0019
Revises: hire_0018
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0019"
down_revision: str | None = "hire_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "hire_meeting_integrations"


def upgrade() -> None:
    op.add_column("hire_interviews", sa.Column("meeting_external_id", sa.String(64), nullable=True))
    t = _TABLE
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name=f"fk_{t}_org_id_organisations"
            ),
            nullable=False,
        ),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_secret", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_error", sa.String(64), nullable=True),
        sa.Column(
            "updated_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{t}_updated_by_users"),
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("org_id", "provider", name="uq_hire_meeting_integrations"),
        sa.CheckConstraint("provider IN ('zoom')", name=f"ck_{t}_provider_valid"),
        sa.CheckConstraint("status IN ('active', 'revoked', 'error')", name=f"ck_{t}_status_valid"),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    for stmt in org_rls_statements(t, app_db_role(), privileges="SELECT, INSERT, UPDATE"):
        op.execute(stmt)
    op.execute(
        f"CREATE POLICY {t}_portal_subject ON {t} AS RESTRICTIVE "
        "USING (NOT hire_portal_active()) WITH CHECK (NOT hire_portal_active())"
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
    op.drop_column("hire_interviews", "meeting_external_id")
