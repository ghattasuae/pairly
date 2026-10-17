# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: the vendor portal's narrow exceptions to the core portal guard.

Core migration 0022 closes every org-scoped core table to portal sessions. The vendor
portal (``pay_vendor:<uuid>``, ``pay_portal_vendor()``) legitimately needs, tied to the
vendor's own portal contacts (``pay_portal_contacts`` already shows a portal session
only its vendor's rows):

* ``audit_events``: insert the events one of the vendor's contacts is the actor of
  (actor type ``vendor``); never read.
* ``decision_records``: insert, and read back, only the steps one of the vendor's
  contacts took (a bank change request).

Its uploads (``documents``) are covered by core's rule (the files the portal person
uploaded).

Revision ID: pay_0013
Revises: pay_0012
Create Date: 2026-10-14
"""

from collections.abc import Sequence

from alembic import op

from pairly_core.migration_helpers import drop_portal_rule_statements, portal_rule_statements

revision: str = "pay_0013"
down_revision: str | None = "pay_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = "0022"

KIND = "pay_vendor"
_ACTOR = (
    "app_portal_id() = pay_portal_vendor() AND actor_type = 'vendor' AND actor_id IN "
    "(SELECT c.id::text FROM pay_portal_contacts c WHERE c.vendor_id = pay_portal_vendor())"
)
RULES: tuple[tuple[str, str | None, str | None], ...] = (
    ("audit_events", None, _ACTOR),
    ("decision_records", _ACTOR, _ACTOR),
)


def upgrade() -> None:
    for table, using, check in RULES:
        for stmt in portal_rule_statements(table, KIND, using=using, check=check):
            op.execute(stmt)


def downgrade() -> None:
    for table, _using, _check in reversed(RULES):
        for stmt in drop_portal_rule_statements(table, KIND):
            op.execute(stmt)
