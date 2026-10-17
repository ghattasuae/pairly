# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: the urgent alert for a bank change asked in the vendor portal.

Seeds ``pay.bank_change.portal_alert`` (email, Slack and in-app, en and ar; category
``urgent``, see ``pairly_pay.templates``) as version 1 shared rows (``org_id`` NULL),
like ``pay_0008`` seeds the invoice check's alert. Downgrade removes the seeded rows
that no notification references.

Revision ID: pay_0014
Revises: pay_0013
Create Date: 2026-10-14
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "pay_0014"
down_revision: str | None = "pay_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SYSTEM_ACTOR = "pairly.migrations.pay_0014"
_TEMPLATE_KEY = "pay.bank_change.portal_alert"


def upgrade() -> None:
    from pairly_core.notifications.templating import (
        TemplateSource,
        declarations_json,
        lint_source,
        parse_declarations,
    )
    from pairly_pay.templates import PORTAL_ALERT_TEMPLATES

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
    for t in PORTAL_ALERT_TEMPLATES:
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
