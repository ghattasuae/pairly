# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Expense notes (the example workflow ``pairly_expense``): table and system templates.

* ``expense_notes``: a tenant table (RLS forced, standard tenant policy). The
  application role gets SELECT, INSERT and UPDATE (a note moves through its statuses);
  never DELETE. Links to the extraction and risk assessment use ``(org_id, id)`` keys,
  so a note can never point at another organisation's row.
* ``expense.note.decided`` templates (email and in-app, en and ar) as version 1 shared
  rows (``org_id`` NULL), like migrations 0011 and 0014 seed core's defaults. Rows that
  already exist are skipped, so the upgrade is idempotent.

Downgrade drops the table and removes the seeded template rows that no notification
references.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-08
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "expense_notes"
_SYSTEM_ACTOR = "pairly.migrations.0016"
_TEMPLATE_KEY = "expense.note.decided"
_STATUSES = (
    "'processing', 'awaiting_approval', 'approved', 'rejected', 'changes_requested', 'failed'"
)


def _jsonb_list(name: str) -> sa.Column[Any]:
    return sa.Column(
        name, postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
    )


def upgrade() -> None:
    app_role = app_db_role()
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id",
                ondelete="RESTRICT",
                name="fk_expense_notes_org_id_organisations",
            ),
            nullable=False,
        ),
        sa.Column(
            "submitted_by",
            sa.Uuid(),
            sa.ForeignKey(
                "users.id", ondelete="RESTRICT", name="fk_expense_notes_submitted_by_users"
            ),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey(
                "documents.id", ondelete="RESTRICT", name="fk_expense_notes_document_id_documents"
            ),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey(
                "workflow_runs.id",
                ondelete="RESTRICT",
                name="fk_expense_notes_run_id_workflow_runs",
            ),
            nullable=True,
        ),
        sa.Column("extraction_id", sa.Uuid(), nullable=True),
        sa.Column("rules_evaluation_id", sa.Uuid(), nullable=True),
        sa.Column("risk_assessment_id", sa.Uuid(), nullable=True),
        sa.Column(
            "approval_id",
            sa.Uuid(),
            sa.ForeignKey(
                "approvals.id", ondelete="RESTRICT", name="fk_expense_notes_approval_id_approvals"
            ),
            nullable=True,
        ),
        sa.Column("merchant", sa.String(200), nullable=True),
        sa.Column("expense_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("amount_aed", sa.Numeric(18, 2), nullable=True),
        sa.Column("category", sa.String(32), nullable=True),
        _jsonb_list("review_reasons"),
        sa.Column("guardrail_report", postgresql.JSONB(), nullable=True),
        sa.Column("sanitized_sha256", sa.String(64), nullable=True),
        _jsonb_list("removed_spans"),
        _jsonb_list("decision_ids"),
        sa.Column("reimbursable", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("failure_reason", sa.String(200), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="ck_expense_notes_status_valid"),
        sa.CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="ck_expense_notes_currency_format"
        ),
        sa.CheckConstraint(
            "amount IS NULL OR amount >= 0", name="ck_expense_notes_amount_non_negative"
        ),
        sa.CheckConstraint(
            "amount_aed IS NULL OR amount_aed >= 0",
            name="ck_expense_notes_amount_aed_non_negative",
        ),
        sa.CheckConstraint("char_length(note) <= 1000", name="ck_expense_notes_note_length"),
        sa.CheckConstraint(
            "NOT reimbursable OR status = 'approved'",
            name="ck_expense_notes_reimbursable_approved",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "extraction_id"],
            ["extractions.org_id", "extractions.id"],
            ondelete="RESTRICT",
            name="fk_expense_notes_extraction_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "risk_assessment_id"],
            ["risk_assessments.org_id", "risk_assessments.id"],
            ondelete="RESTRICT",
            name="fk_expense_notes_risk_assessment_org",
        ),
    )
    op.create_index("ix_expense_notes_org_id", TABLE, ["org_id"])
    op.create_index("ix_expense_notes_submitter", TABLE, ["org_id", "submitted_by", "created_at"])
    op.create_index("ix_expense_notes_status", TABLE, ["org_id", "status", "created_at"])
    for stmt in org_rls_statements(TABLE, app_role, privileges="SELECT, INSERT, UPDATE"):
        op.execute(stmt)
    _seed_templates()


def _seed_templates() -> None:
    from pairly_core.notifications.templating import (
        TemplateSource,
        declarations_json,
        lint_source,
        parse_declarations,
    )
    from pairly_expense.templates import DECIDED_TEMPLATES

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
    for t in DECIDED_TEMPLATES:
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
    op.drop_table(TABLE)
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
