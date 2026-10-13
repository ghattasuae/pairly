# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Insert shared (``org_id`` NULL) message templates at a version, for migrations.

Idempotent: a template that exists at that version is skipped. Every template is linted
first; an invalid one stops the migration. ``remove`` (downgrade) deletes the rows a
migration inserted that no notification uses.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from pairly_core.notifications.defaults import DefaultTemplate


def insert_templates(
    bind: Connection, templates: Sequence[DefaultTemplate], *, version: int, actor: str
) -> None:
    from pairly_core.notifications.templating import (
        TemplateSource,
        declarations_json,
        lint_source,
        parse_declarations,
    )

    bind.execute(sa.text("ALTER TABLE templates NO FORCE ROW LEVEL SECURITY"))
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
    for t in templates:
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
                "version": version,
                "category": t.category,
                "subject": t.subject,
                "body": t.body,
                "body_html": t.body_html,
                "variables": json.dumps(declarations_json(specs)),
                "requires_approval": t.requires_approval,
                "actor": actor,
            },
        )
    bind.execute(sa.text("ALTER TABLE templates FORCE ROW LEVEL SECURITY"))


def remove_templates(bind: Connection, *, actor: str) -> None:
    for stmt in (
        "ALTER TABLE templates NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE notifications NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE templates DISABLE TRIGGER templates_immutable",
    ):
        bind.execute(sa.text(stmt))
    bind.execute(
        sa.text(
            "DELETE FROM templates t WHERE t.org_id IS NULL AND t.created_by_id = :actor "
            "AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.template_id = t.id)"
        ).bindparams(actor=actor)
    )
    for stmt in (
        "ALTER TABLE templates ENABLE TRIGGER templates_immutable",
        "ALTER TABLE notifications FORCE ROW LEVEL SECURITY",
        "ALTER TABLE templates FORCE ROW LEVEL SECURITY",
    ):
        bind.execute(sa.text(stmt))
