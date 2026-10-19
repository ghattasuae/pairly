# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Version 2 of the approval request email (``core.approval.requested``, email, en and ar).

Seeds ``pairly_core.notifications.defaults.APPROVAL_REQUESTED_V2`` as version 2 shared
rows (``org_id`` NULL), the way 0014 seeds its defaults: the same subject, text and
variables as version 1, the HTML on the shared email shell with one saffron Approve
button (ink text) and a danger outline Reject, in place of the hand-coded green and red
buttons with white text. Version 1 stays as it is (versions are immutable): mail already
queued keeps the HTML it was rendered with, and an organisation's own version still wins
over every shared one. ``templates`` forces Row Level Security and has no INSERT policy
for shared rows, so the owner lifts FORCE for the insert only. Rows that already exist
are skipped, so the upgrade is idempotent.

Downgrade removes the seeded version 2 rows that no notification references (the
immutability trigger is paused for that delete only); referenced rows stay, and a later
upgrade skips them.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-19
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SYSTEM_ACTOR = "pairly.migrations.0036"
_KEY = "core.approval.requested"
_VERSION = 2


def upgrade() -> None:
    from pairly_core.notifications.defaults import APPROVAL_REQUESTED_V2
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
        "SELECT NULL, CAST(:key AS text), CAST(:channel AS text), CAST(:locale AS text), "
        "CAST(:version AS integer), CAST(:category AS text), CAST(:subject AS text), "
        "CAST(:body AS text), CAST(:body_html AS text), CAST(:variables AS jsonb), "
        "CAST(:requires_approval AS boolean), 'system', CAST(:actor AS text) "
        "WHERE NOT EXISTS (SELECT 1 FROM templates WHERE org_id IS NULL "
        "AND key = CAST(:key AS text) AND channel = CAST(:channel AS text) "
        "AND locale = CAST(:locale AS text) AND version = CAST(:version AS integer))"
    )
    for t in APPROVAL_REQUESTED_V2:
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
                "version": _VERSION,
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
            "AND t.version = :version AND t.created_by_id = :actor "
            "AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.template_id = t.id)"
        ).bindparams(key=_KEY, version=_VERSION, actor=_SYSTEM_ACTOR)
    )
    op.execute("ALTER TABLE templates ENABLE TRIGGER templates_immutable")
    op.execute("ALTER TABLE notifications FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates FORCE ROW LEVEL SECURITY")
