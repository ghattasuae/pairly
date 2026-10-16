# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: appeals from the vendor portal (``pairly_pay.portal.appeals``).

Two narrow additions to the vendor portal's exceptions to the core portal guard
(``pay_0013``), tied to the signed-in vendor (``pay_portal_vendor()``):

* ``decision_records``: besides the steps the vendor's own contacts took (``pay_0013``,
  unchanged, still the only rows it may write), read the decisions a vendor may ask a
  person to review: the duplicate flag (a ``fraud_flag`` record whose duplicate check
  failed; no other check result) and the rejection of its own invoices, and the staff
  or system steps of its own bank-detail change tickets (opened, rejected, cancelled,
  failed).
* ``appeals``: read and open its own (requester ``vendor``, ``pay_vendor:<vendor id>``).

Seeds ``pay.vendor_portal.appeal`` (email and in-app, en and ar; category
``data_requests``, see ``pairly_pay.templates``) as version 1 shared rows, like
``pay_0014``. Downgrade restores ``pay_0013``'s rule, removes the appeals rule and the
seeded rows no notification references.

Revision ID: pay_0018
Revises: pay_0017
Create Date: 2026-10-16
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import drop_portal_rule_statements, portal_rule_statements

revision: str = "pay_0018"
down_revision: str | None = "pay_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KIND = "pay_vendor"
_SYSTEM_ACTOR = "pairly.migrations.pay_0018"
_TEMPLATE_KEY = "pay.vendor_portal.appeal"
_ME = "app_portal_id() = pay_portal_vendor()"
# pay_0013's rule: the steps one of the vendor's contacts took.
_ACTOR = (
    "app_portal_id() = pay_portal_vendor() AND actor_type = 'vendor' AND actor_id IN "
    "(SELECT c.id::text FROM pay_portal_contacts c WHERE c.vendor_id = pay_portal_vendor())"
)
_OWN_INVOICES = "SELECT i.id::text FROM pay_invoices i WHERE i.vendor_id = pay_portal_vendor()"
_OWN_TICKETS = (
    "SELECT t.id::text FROM pay_bank_change_tickets t WHERE t.vendor_id = pay_portal_vendor()"
)
_APPEALABLE = (
    f"{_ME} AND workflow = 'pay' AND ("
    f"(entity_type = 'pay.invoice' AND entity_id IN ({_OWN_INVOICES}) "
    "AND ((kind = 'fraud_flag' AND output -> 'checks' ->> 'duplicates' = 'fail') "
    "OR (kind = 'final_action' "
    "AND final_action = 'pay.invoice.rejected')))"
    f" OR (entity_type = 'pay.bank_change' AND entity_id IN ({_OWN_TICKETS}) "
    "AND kind = 'final_action' AND actor_type <> 'vendor' AND final_action IN "
    "('bank_change_requested', 'bank_change_rejected', 'bank_change_cancelled', "
    "'bank_change_failed')))"
)
_REQUESTER = (
    f"{_ME} AND requester_type = 'vendor' "
    "AND requester_id = 'pay_vendor:' || pay_portal_vendor()::text"
)


def upgrade() -> None:
    for stmt in portal_rule_statements(
        "decision_records", KIND, using=f"({_ACTOR}) OR ({_APPEALABLE})", check=_ACTOR
    ):
        op.execute(stmt)
    for stmt in portal_rule_statements("appeals", KIND, using=_REQUESTER, check=_REQUESTER):
        op.execute(stmt)
    _seed_templates()


def _seed_templates() -> None:
    from pairly_core.notifications.templating import (
        TemplateSource,
        declarations_json,
        lint_source,
        parse_declarations,
    )
    from pairly_pay.templates import APPEAL_TEMPLATES

    bind = op.get_bind()
    op.execute("ALTER TABLE templates NO FORCE ROW LEVEL SECURITY")
    insert = sa.text(
        "INSERT INTO templates (org_id, key, channel, locale, version, category, subject, "
        "body, body_html, variables, requires_approval, created_by_type, created_by_id) "
        "SELECT NULL, CAST(:key AS text), CAST(:channel AS text), CAST(:locale AS text), 1, "
        "CAST(:category AS text), CAST(:subject AS text), CAST(:body AS text), "
        "CAST(:body_html AS text), CAST(:variables AS jsonb), "
        "CAST(:requires_approval AS boolean), 'system', CAST(:actor AS text) "
        "WHERE NOT EXISTS (SELECT 1 FROM templates WHERE org_id IS NULL "
        "AND key = CAST(:key AS text) AND channel = CAST(:channel AS text) "
        "AND locale = CAST(:locale AS text) AND version = 1)"
    )
    for t in APPEAL_TEMPLATES:
        specs = parse_declarations(t.variables)
        source = TemplateSource(t.channel, t.locale, t.subject, t.body, t.body_html, specs)  # type: ignore[arg-type]
        if any(i.severity == "error" for i in lint_source(source)):
            raise RuntimeError(f"system template {t.key} ({t.channel}, {t.locale}) is invalid")
        bind.execute(
            insert,
            {
                "key": t.key,
                "channel": t.channel,
                "locale": t.locale,
                "category": t.category,
                "subject": t.subject,
                "body": t.body,
                "body_html": t.body_html,
                "variables": json.dumps(declarations_json(specs)),
                "requires_approval": t.requires_approval,
                "actor": _SYSTEM_ACTOR,
            },
        )
    op.execute("ALTER TABLE templates FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE templates NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notifications NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates DISABLE TRIGGER templates_immutable")
    op.execute(
        sa.text(
            "DELETE FROM templates t WHERE t.org_id IS NULL AND t.key = :key "
            "AND t.created_by_id = :actor "
            "AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.template_id = t.id)"
        ).bindparams(key=_TEMPLATE_KEY, actor=_SYSTEM_ACTOR)
    )
    op.execute("ALTER TABLE templates ENABLE TRIGGER templates_immutable")
    op.execute("ALTER TABLE notifications FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates FORCE ROW LEVEL SECURITY")
    for stmt in drop_portal_rule_statements("appeals", KIND):
        op.execute(stmt)
    for stmt in portal_rule_statements("decision_records", KIND, using=_ACTOR, check=_ACTOR):
        op.execute(stmt)
