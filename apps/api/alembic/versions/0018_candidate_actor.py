# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Audit events by a portal person: actor type ``candidate``.

A candidate signed in to a workflow's portal (``ActorKind.CANDIDATE``, Pairly Hire's
candidate portal) acts as themselves: signing in, replying, withdrawing an application,
asking for a human review or their data. Those audit events name the actor type
``candidate`` and the candidate's id, so the trail says who did it.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "actor_type IN ('user', 'agent', 'system', 'api_key')"
_NEW = "actor_type IN ('user', 'agent', 'system', 'api_key', 'candidate')"


def upgrade() -> None:
    op.drop_constraint("ck_audit_events_actor_type_valid", "audit_events", type_="check")
    op.create_check_constraint("ck_audit_events_actor_type_valid", "audit_events", _NEW)


def downgrade() -> None:
    op.drop_constraint("ck_audit_events_actor_type_valid", "audit_events", type_="check")
    op.create_check_constraint("ck_audit_events_actor_type_valid", "audit_events", _OLD)
