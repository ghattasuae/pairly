# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: the call-back number comes from an independent source (security review
2026-10, M-05).

* ``pay_vendors.phone_claimed``: the number a vendor's onboarding form gave, encrypted.
  It is no longer the number on file: accounts payable records that one from an
  independent source when it accepts the onboarding.
* ``pay_bank_change_tickets``: ``callback_source`` (``trade_licence``,
  ``signed_contract``, ``prior_correspondence``, ``official_registry``),
  ``callback_reference`` (encrypted free text), ``callback_recorded_by`` (the person)
  and ``callback_matches_claim`` (whether it matched the number the request claimed).
  A source needs a reference and a person (check ``callback_source_valid``).

Revision ID: pay_0020
Revises: pay_0019
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "pay_0020"
down_revision: str | None = "pay_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "pay_bank_change_tickets"
_SOURCES = "'trade_licence', 'signed_contract', 'prior_correspondence', 'official_registry'"


def upgrade() -> None:
    op.add_column("pay_vendors", sa.Column("phone_claimed", sa.Text(), nullable=True))
    op.add_column(T, sa.Column("callback_source", sa.String(32), nullable=True))
    op.add_column(T, sa.Column("callback_reference", sa.Text(), nullable=True))
    op.add_column(
        T,
        sa.Column(
            "callback_recorded_by",
            sa.Uuid(),
            sa.ForeignKey(
                "users.id", ondelete="RESTRICT", name=f"fk_{T}_callback_recorded_by_users"
            ),
            nullable=True,
        ),
    )
    op.add_column(T, sa.Column("callback_matches_claim", sa.Boolean(), nullable=True))
    # The naming convention makes it ck_pay_bank_change_tickets_callback_source_valid.
    op.create_check_constraint(
        "callback_source_valid",
        T,
        f"callback_source IS NULL OR (callback_source IN ({_SOURCES}) "
        "AND callback_reference IS NOT NULL AND callback_recorded_by IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("callback_source_valid", T, type_="check")
    for column in (
        "callback_matches_claim",
        "callback_recorded_by",
        "callback_reference",
        "callback_source",
    ):
        op.drop_column(T, column)
    op.drop_column("pay_vendors", "phone_claimed")
