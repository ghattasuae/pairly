# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire candidate portal (WP2.9).

Tables (tenant tables, RLS forced, standard tenant policy; never DELETE):

* ``hire_portal_tokens``: magic links (secret hash, single use, short expiry).
  SELECT, INSERT, UPDATE.
* ``hire_portal_sessions``: portal sessions (secret hash, expiry, revocation).
  SELECT, INSERT, UPDATE.
* ``hire_data_requests``: candidates' requests to admins (deletion). SELECT, INSERT,
  UPDATE.
* ``hire_document_requests``: files the hiring team asks a candidate for.
  SELECT, INSERT, UPDATE.

The portal subject (``pairly_core.db.set_portal_subject``, setting
``app.portal_subject``, ``hire_candidate:<uuid>`` for a candidate's portal session) adds
a second, restrictive layer to Row Level Security. ``hire_portal_active()`` says whether
the transaction acts for a portal person; ``hire_portal_candidate()`` is the candidate's
id (NULL for any other kind of portal person, so nothing matches). While a portal
subject is set:

* tables of the candidate (``hire_candidates`` by id; ``hire_applications``,
  ``hire_messages``, ``hire_candidate_prefs`` and the four tables above by
  ``candidate_id``) show and accept only that candidate's rows;
* tables of an application (``hire_pipeline_entries``, ``hire_stage_moves``) only rows
  of that candidate's applications;
* what reviewers write or the screening computes (``hire_notes``, ``hire_screenings``,
  ``hire_screening_settings``, ``hire_rubric_drafts``, ``hire_referrals``,
  ``hire_referral_links``) shows nothing at all;
* ``hire_self_identification`` shows only the rows of that candidate's applications and
  accepts no writes (the candidate's own answers, for their data export,
  ``pairly_hire.selfid.export``).

Staff, agents and jobs never set a portal subject, so these policies change nothing
for them. ``hire_stage_moves.actor_type`` now accepts ``candidate`` (a withdrawal from
the portal).

System templates: ``hire.portal.login_link`` and ``hire.application.withdrawn`` (email,
English and Arabic, ``candidate_comms``), version 1 shared rows, skipped when present.

Revision ID: hire_0004b
Revises: hire_0004a
Create Date: 2026-10-09
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0004b"
down_revision: str | None = "hire_0004a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = "0018"

_RW = "SELECT, INSERT, UPDATE"
_SYSTEM_ACTOR = "pairly.migrations.hire_0004b"
_NEW = (
    "hire_portal_tokens",
    "hire_portal_sessions",
    "hire_data_requests",
    "hire_document_requests",
)
_BY_CANDIDATE = (
    "hire_applications",
    "hire_messages",
    "hire_candidate_prefs",
    *_NEW,
)
_BY_APPLICATION = ("hire_pipeline_entries", "hire_stage_moves")
_SELF_ID = "hire_self_identification"
_HIDDEN = (
    "hire_notes",
    "hire_screenings",
    "hire_screening_settings",
    "hire_rubric_drafts",
    "hire_referrals",
    "hire_referral_links",
)


def _uuid_pk() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _org(table: str) -> sa.Column[Any]:
    return sa.Column(
        "org_id",
        sa.Uuid(),
        sa.ForeignKey(
            "organisations.id", ondelete="RESTRICT", name=f"fk_{table}_org_id_organisations"
        ),
        nullable=False,
    )


def _ts(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _candidate_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id", "candidate_id"],
        ["hire_candidates.org_id", "hire_candidates.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_candidate_org",
    )


def _finish(table: str) -> None:
    op.create_index(f"ix_{table}_org_id", table, ["org_id"])
    for stmt in org_rls_statements(table, app_db_role(), privileges=_RW):
        op.execute(stmt)


def upgrade() -> None:
    t = "hire_portal_tokens"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("locale", sa.String(2), nullable=False),
        _ts("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _ts("used_at", nullable=True),
        _candidate_fk(t),
        sa.CheckConstraint(
            "secret_hash ~ '^[0-9a-f]{64}$'", name="ck_hire_portal_tokens_secret_hash_format"
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_hire_portal_tokens_expiry_after_creation"
        ),
    )
    op.create_index("ix_hire_portal_tokens_candidate", t, ["org_id", "candidate_id", "created_at"])
    _finish(t)

    t = "hire_portal_sessions"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("token_id", sa.Uuid(), nullable=True),
        _ts("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _ts("last_seen_at", nullable=True),
        _ts("revoked_at", nullable=True),
        _candidate_fk(t),
        sa.CheckConstraint(
            "secret_hash ~ '^[0-9a-f]{64}$'", name="ck_hire_portal_sessions_secret_hash_format"
        ),
    )
    op.create_index(
        "ix_hire_portal_sessions_candidate", t, ["org_id", "candidate_id", "created_at"]
    )
    _finish(t)

    t = "hire_data_requests"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        _ts("created_at"),
        sa.Column(
            "handled_by",
            sa.Uuid(),
            sa.ForeignKey(
                "users.id", ondelete="RESTRICT", name="fk_hire_data_requests_handled_by_users"
            ),
            nullable=True,
        ),
        _ts("handled_at", nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.UniqueConstraint("org_id", "id", name="uq_hire_data_requests_org_id_id"),
        _candidate_fk(t),
        sa.CheckConstraint("kind IN ('deletion')", name="ck_hire_data_requests_kind_valid"),
        sa.CheckConstraint(
            "status IN ('open', 'completed', 'declined')",
            name="ck_hire_data_requests_status_valid",
        ),
        sa.CheckConstraint(
            "(status = 'open') = (handled_at IS NULL)",
            name="ck_hire_data_requests_handled_when_closed",
        ),
    )
    op.create_index("ix_hire_data_requests_status", t, ["org_id", "status", "created_at"])
    _finish(t)

    t = "hire_document_requests"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("label_ar", sa.String(120), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey(
                "documents.id",
                ondelete="RESTRICT",
                name="fk_hire_document_requests_document_id_documents",
            ),
            nullable=True,
        ),
        sa.Column(
            "requested_by",
            sa.Uuid(),
            sa.ForeignKey(
                "users.id", ondelete="RESTRICT", name="fk_hire_document_requests_requested_by_users"
            ),
            nullable=False,
        ),
        _ts("created_at"),
        _ts("fulfilled_at", nullable=True),
        sa.UniqueConstraint("org_id", "id", name="uq_hire_document_requests_org_id_id"),
        sa.ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_document_requests_application_org",
        ),
        _candidate_fk(t),
        sa.CheckConstraint(
            "status IN ('open', 'received', 'cancelled')",
            name="ck_hire_document_requests_status_valid",
        ),
        sa.CheckConstraint(
            "status <> 'received' OR document_id IS NOT NULL",
            name="ck_hire_document_requests_received_has_document",
        ),
    )
    op.create_index(
        "ix_hire_document_requests_application", t, ["org_id", "application_id", "created_at"]
    )
    _finish(t)

    op.drop_constraint("ck_hire_stage_moves_actor_type_valid", "hire_stage_moves", type_="check")
    op.create_check_constraint(
        "ck_hire_stage_moves_actor_type_valid",
        "hire_stage_moves",
        "actor_type IN ('user', 'agent', 'api_key', 'system', 'candidate')",
    )

    _portal_policies()
    _seed_templates()


def _portal_policies() -> None:
    op.execute(
        """
        CREATE FUNCTION hire_portal_active() RETURNS boolean
        LANGUAGE sql STABLE
        AS $$ SELECT coalesce(current_setting('app.portal_subject', true), '') <> '' $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION hire_portal_candidate() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$
            SELECT CASE
                WHEN current_setting('app.portal_subject', true)
                     ~ '^hire_candidate:[0-9a-f-]{36}$'
                THEN substr(current_setting('app.portal_subject', true), 16)::uuid
            END
        $$
        """
    )
    own = "NOT hire_portal_active() OR {column} = hire_portal_candidate()"
    op.execute(
        "CREATE POLICY hire_candidates_portal_subject ON hire_candidates AS RESTRICTIVE "
        f"USING ({own.format(column='id')}) WITH CHECK ({own.format(column='id')})"
    )
    for table in _BY_CANDIDATE:
        rule = own.format(column="candidate_id")
        op.execute(
            f"CREATE POLICY {table}_portal_subject ON {table} AS RESTRICTIVE "
            f"USING ({rule}) WITH CHECK ({rule})"
        )
    for table in _BY_APPLICATION:
        rule = (
            "NOT hire_portal_active() OR application_id IN (SELECT a.id FROM hire_applications a "
            "WHERE a.candidate_id = hire_portal_candidate())"
        )
        op.execute(
            f"CREATE POLICY {table}_portal_subject ON {table} AS RESTRICTIVE "
            f"USING ({rule}) WITH CHECK ({rule})"
        )
    for table in _HIDDEN:
        op.execute(
            f"CREATE POLICY {table}_portal_subject ON {table} AS RESTRICTIVE "
            "USING (NOT hire_portal_active()) WITH CHECK (NOT hire_portal_active())"
        )
    # The candidate's own self-identification answers, read only, for their data export
    # (``pairly_hire.selfid.export``); a portal session can never write them.
    own_apps = (
        "NOT hire_portal_active() OR application_id IN (SELECT a.id FROM hire_applications a "
        "WHERE a.candidate_id = hire_portal_candidate())"
    )
    op.execute(
        f"CREATE POLICY {_SELF_ID}_portal_subject ON {_SELF_ID} AS RESTRICTIVE "
        f"USING ({own_apps}) WITH CHECK (NOT hire_portal_active())"
    )


def _seed_templates() -> None:
    from pairly_core.notifications.templating import (
        TemplateSource,
        declarations_json,
        lint_source,
        parse_declarations,
    )
    from pairly_hire.portal.templates import PORTAL_TEMPLATES

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
    for t in PORTAL_TEMPLATES:
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
    for table in reversed(_NEW):
        op.drop_table(table)
    for table in (_SELF_ID, *_HIDDEN, *_BY_APPLICATION, *_BY_CANDIDATE, "hire_candidates"):
        if table not in _NEW:
            op.execute(f"DROP POLICY IF EXISTS {table}_portal_subject ON {table}")
    op.execute("DROP FUNCTION IF EXISTS hire_portal_candidate()")
    op.execute("DROP FUNCTION IF EXISTS hire_portal_active()")
    op.drop_constraint("ck_hire_stage_moves_actor_type_valid", "hire_stage_moves", type_="check")
    op.create_check_constraint(
        "ck_hire_stage_moves_actor_type_valid",
        "hire_stage_moves",
        "actor_type IN ('user', 'agent', 'api_key', 'system')",
    )
    op.execute("ALTER TABLE templates NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notifications NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates DISABLE TRIGGER templates_immutable")
    op.execute(
        sa.text(
            "DELETE FROM templates t WHERE t.org_id IS NULL AND t.created_by_id = :actor "
            "AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.template_id = t.id)"
        ).bindparams(actor=_SYSTEM_ACTOR)
    )
    op.execute("ALTER TABLE templates ENABLE TRIGGER templates_immutable")
    op.execute("ALTER TABLE notifications FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates FORCE ROW LEVEL SECURITY")
