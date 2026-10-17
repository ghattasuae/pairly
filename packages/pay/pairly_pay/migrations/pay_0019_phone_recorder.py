# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: who recorded the phone number on file (security review 2026-10, M-04).

* ``pay_vendors.phone_set_by``: the person who recorded the number on file (created the
  vendor with it, or last changed it). NULL when no staff member did (a vendor's own
  form, or a number from before this revision: nobody is barred for those).
* ``pay_bank_change_tickets.phone_set_by``: snapshot at opening of the person barred
  from verifying the change, because they changed the number within the phone-change
  window or recorded it for a vendor without a verified account.

Both reference ``users`` (RESTRICT, like the other people on a ticket).

Revision ID: pay_0019
Revises: pay_0018
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "pay_0019"
down_revision: str | None = "pay_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("pay_vendors", "pay_bank_change_tickets")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "phone_set_by",
                sa.Uuid(),
                sa.ForeignKey(
                    "users.id", ondelete="RESTRICT", name=f"fk_{table}_phone_set_by_users"
                ),
                nullable=True,
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "phone_set_by")
