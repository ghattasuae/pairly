# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Audit events and decision records by a vendor portal contact: actor type ``vendor``.

A vendor's contact signed in to Pairly Pay's vendor portal (``ActorKind.VENDOR``) acts
as themselves: signing in, submitting an invoice or a credit note, updating contact
details, asking for a bank-detail change, writing to accounts payable. Those audit
events and decision records name the actor type ``vendor`` and the contact's id.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "actor_type IN ('user', 'agent', 'system', 'api_key', 'candidate')"
_NEW = "actor_type IN ('user', 'agent', 'system', 'api_key', 'candidate', 'vendor')"
_CHECKS = (
    ("ck_audit_events_actor_type_valid", "audit_events"),
    ("ck_decision_records_actor_valid", "decision_records"),
)


def upgrade() -> None:
    for name, table in _CHECKS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, _NEW)


def downgrade() -> None:
    for name, table in _CHECKS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, _OLD)
