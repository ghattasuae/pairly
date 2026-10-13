# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Decision records by a portal person: actor type ``candidate``.

A candidate accepts or declines an offer in Pairly Hire's candidate portal. That
decision is theirs, so its decision record names the actor type ``candidate`` and the
candidate's id, like their audit events (``0018``).

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "actor_type IN ('user', 'agent', 'system', 'api_key')"
_NEW = "actor_type IN ('user', 'agent', 'system', 'api_key', 'candidate')"


def upgrade() -> None:
    op.drop_constraint("ck_decision_records_actor_valid", "decision_records", type_="check")
    op.create_check_constraint("ck_decision_records_actor_valid", "decision_records", _NEW)


def downgrade() -> None:
    op.drop_constraint("ck_decision_records_actor_valid", "decision_records", type_="check")
    op.create_check_constraint("ck_decision_records_actor_valid", "decision_records", _OLD)
