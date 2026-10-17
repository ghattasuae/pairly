# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Retention: pause a rule, and the first-deletion notice (``pairly_core.retention``).

* ``retention_rule_controls``: per organisation and rule, a pause (who, when) and the
  first-deletion notice sent to the admins (when, how many, for which day; once per
  organisation and rule). SELECT, INSERT, UPDATE for the application role; org scoped
  with Row Level Security forced and closed to portal sessions.
* Seeds ``core.retention.first_deletion`` (email and in-app, en and ar; category
  ``retention``, always urgent) as version 1 shared rows, the way 0014 does. Downgrade
  removes the seeded rows no notification references.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-16
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import app_db_role, org_rls_statements, portal_guard_statements

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "retention_rule_controls"
_SYSTEM_ACTOR = "pairly.migrations.0031"
_KEY = "core.retention.first_deletion"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name=f"fk_{TABLE}_org_id_organisations"
            ),
            nullable=False,
        ),
        sa.Column("rule", sa.String(100), nullable=False),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "paused_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{TABLE}_paused_by_users"),
            nullable=True,
        ),
        sa.Column("notice_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notice_count", sa.Integer(), nullable=True),
        sa.Column("notice_deletes_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("org_id", "rule", name="uq_retention_rule_controls_org_rule"),
        sa.CheckConstraint(
            "(paused_at IS NULL) = (paused_by IS NULL)",
            name="ck_retention_rule_controls_paused_by_person",
        ),
        sa.CheckConstraint(
            "(notice_sent_at IS NULL) = (notice_deletes_on IS NULL)",
            name="ck_retention_rule_controls_notice_complete",
        ),
        sa.CheckConstraint(
            "notice_count IS NULL OR notice_count >= 0",
            name="ck_retention_rule_controls_notice_count_valid",
        ),
    )
    op.create_index(f"ix_{TABLE}_org_id", TABLE, ["org_id"])
    for statement in org_rls_statements(TABLE, app_db_role(), privileges="SELECT, INSERT, UPDATE"):
        op.execute(statement)
    for statement in portal_guard_statements(TABLE):
        op.execute(statement)
    _seed_templates()


def _seed_templates() -> None:
    from pairly_core.notifications.defaults import RETENTION_FIRST_DELETION
    from pairly_core.notifications.templating import (
        TemplateSource,
        declarations_json,
        lint_source,
        parse_declarations,
    )

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
    for t in RETENTION_FIRST_DELETION:
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
        ).bindparams(key=_KEY, actor=_SYSTEM_ACTOR)
    )
    op.execute("ALTER TABLE templates ENABLE TRIGGER templates_immutable")
    op.execute("ALTER TABLE notifications FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates FORCE ROW LEVEL SECURITY")
    op.drop_table(TABLE)
