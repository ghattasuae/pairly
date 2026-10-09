# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire interview scheduling (WP2.8).

Tables (all tenant tables, RLS forced, standard tenant policy; the application role gets
SELECT, INSERT and UPDATE and never DELETE): ``hire_scheduling_settings``,
``hire_interviewer_settings``, ``hire_interviews``, ``hire_interview_panelists``,
``hire_calendar_blocks``, ``hire_calendar_connections``
(``pairly_hire.scheduling.models``).

Children point at their parents by ``(org_id, id)``, so no row can reference another
organisation's.

The scheduling messages of ``pairly_hire.scheduling.templates.SCHEDULING_TEMPLATES``
(English and Arabic) as version 1 shared rows, skipped when they exist (idempotent).

Revision ID: hire_0005
Revises: hire_0003 (the coordinator re-chains it after hire_0004)
Create Date: 2026-10-09
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0005"
down_revision: str | None = "hire_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SYSTEM_ACTOR = "pairly.migrations.hire_0005"
_RW = "SELECT, INSERT, UPDATE"
_STAGE_KEY = "'^[a-z][a-z0-9_]{0,31}$'"
_STATUSES = "'proposed', 'booked', 'rescheduled', 'cancelled', 'completed', 'no_show'"


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


def _user(table: str, column: str, *, nullable: bool) -> sa.Column[Any]:
    return sa.Column(
        column,
        sa.Uuid(),
        sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{table}_{column}_users"),
        nullable=nullable,
    )


def _int(name: str, default: int | None = None, *, nullable: bool = False) -> sa.Column[Any]:
    if default is None:
        return sa.Column(name, sa.Integer(), nullable=nullable)
    return sa.Column(name, sa.Integer(), nullable=nullable, server_default=str(default))


def _finish(table: str) -> None:
    op.create_index(f"ix_{table}_org_id", table, ["org_id"])
    for stmt in org_rls_statements(table, app_db_role(), privileges=_RW):
        op.execute(stmt)


def upgrade() -> None:
    t = "hire_scheduling_settings"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("default_timezone", sa.String(64), nullable=False, server_default="Asia/Dubai"),
        _int("default_duration_min", 45),
        _int("slot_step_min", 30),
        _int("min_notice_hours", 12),
        _int("horizon_days", 14),
        _int("link_days", 7),
        sa.Column("fake_seed", sa.String(64), nullable=False, server_default="pairly-demo"),
        _user(t, "updated_by", nullable=True),
        _ts("updated_at"),
        sa.UniqueConstraint("org_id", name="uq_hire_scheduling_settings_org_id"),
        sa.CheckConstraint(
            "default_duration_min BETWEEN 15 AND 240", name=f"ck_{t}_duration_range"
        ),
        sa.CheckConstraint("slot_step_min IN (15, 30, 60)", name=f"ck_{t}_step_valid"),
        sa.CheckConstraint("min_notice_hours BETWEEN 0 AND 168", name=f"ck_{t}_notice_range"),
        sa.CheckConstraint("horizon_days BETWEEN 1 AND 60", name=f"ck_{t}_horizon_range"),
        sa.CheckConstraint("link_days BETWEEN 1 AND 30", name=f"ck_{t}_link_days_range"),
    )
    _finish(t)

    t = "hire_interviewer_settings"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        _user(t, "user_id", nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("working_hours", postgresql.JSONB(), nullable=False),
        _int("buffer_before_min", 0),
        _int("buffer_after_min", 0),
        _int("max_per_day", nullable=True),
        sa.Column(
            "holidays", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        _user(t, "updated_by", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("org_id", "user_id", name="uq_hire_interviewer_settings_user"),
        sa.CheckConstraint(
            "jsonb_typeof(working_hours) = 'object'", name=f"ck_{t}_working_hours_object"
        ),
        sa.CheckConstraint("jsonb_typeof(holidays) = 'array'", name=f"ck_{t}_holidays_array"),
        sa.CheckConstraint(
            "buffer_before_min BETWEEN 0 AND 240", name=f"ck_{t}_buffer_before_range"
        ),
        sa.CheckConstraint("buffer_after_min BETWEEN 0 AND 240", name=f"ck_{t}_buffer_after_range"),
        sa.CheckConstraint(
            "max_per_day IS NULL OR max_per_day BETWEEN 0 AND 24", name=f"ck_{t}_max_per_day_range"
        ),
    )
    _finish(t)

    t = "hire_interviews"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("stage_key", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        _int("duration_min"),
        _ts("starts_at", nullable=True),
        _ts("ends_at", nullable=True),
        sa.Column("candidate_timezone", sa.String(64), nullable=False),
        sa.Column("candidate_locale", sa.String(2), nullable=False),
        _user(t, "organizer_id", nullable=False),
        sa.Column("meeting_url", sa.Text(), nullable=True),
        sa.Column("meeting_provider", sa.String(16), nullable=True),
        sa.Column("calendar_provider", sa.String(16), nullable=True),
        sa.Column("calendar_event_id", sa.String(256), nullable=True),
        sa.Column("ics_uid", sa.String(200), nullable=False),
        _int("ics_sequence", 0),
        sa.Column("link_nonce", sa.String(16), nullable=True),
        _ts("link_expires_at", nullable=True),
        _ts("reminded_24h_at", nullable=True),
        _ts("reminded_1h_at", nullable=True),
        _ts("cancelled_at", nullable=True),
        sa.Column("cancelled_by", sa.String(16), nullable=True),
        _ts("outcome_at", nullable=True),
        _user(t, "outcome_by", nullable=True),
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.String(128), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("org_id", "id", name="uq_hire_interviews_org_id_id"),
        sa.UniqueConstraint("org_id", "ics_uid", name="uq_hire_interviews_ics_uid"),
        sa.ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_interviews_application_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_interviews_job_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "candidate_id"],
            ["hire_candidates.org_id", "hire_candidates.id"],
            ondelete="RESTRICT",
            name="fk_hire_interviews_candidate_org",
        ),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name=f"ck_{t}_status_valid"),
        sa.CheckConstraint(f"stage_key ~ {_STAGE_KEY}", name=f"ck_{t}_stage_key_format"),
        sa.CheckConstraint("duration_min BETWEEN 5 AND 480", name=f"ck_{t}_duration_range"),
        sa.CheckConstraint(
            "status IN ('proposed', 'cancelled') OR starts_at IS NOT NULL",
            name=f"ck_{t}_booked_has_time",
        ),
        sa.CheckConstraint(
            "(starts_at IS NULL) = (ends_at IS NULL) AND (ends_at IS NULL OR ends_at > starts_at)",
            name=f"ck_{t}_times_valid",
        ),
        sa.CheckConstraint("ics_sequence >= 0", name=f"ck_{t}_sequence_valid"),
        sa.CheckConstraint("candidate_locale IN ('en', 'ar')", name=f"ck_{t}_locale_valid"),
        sa.CheckConstraint(
            "link_nonce IS NULL OR link_nonce ~ '^[0-9a-f]{16}$'", name=f"ck_{t}_link_nonce_format"
        ),
    )
    op.create_index("ix_hire_interviews_application", t, ["org_id", "application_id", "created_at"])
    op.create_index("ix_hire_interviews_status_start", t, ["org_id", "status", "starts_at"])
    op.create_index("ix_hire_interviews_job", t, ["org_id", "job_id", "starts_at"])
    _finish(t)

    t = "hire_interview_panelists"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        _user(t, "user_id", nullable=False),
        _ts("created_at"),
        _ts("removed_at", nullable=True),
        sa.UniqueConstraint(
            "org_id", "interview_id", "user_id", name="uq_hire_interview_panelists"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "interview_id"],
            ["hire_interviews.org_id", "hire_interviews.id"],
            ondelete="RESTRICT",
            name="fk_hire_interview_panelists_interview_org",
        ),
    )
    op.create_index("ix_hire_interview_panelists_user", t, ["org_id", "user_id"])
    _finish(t)

    t = "hire_calendar_blocks"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        _user(t, "user_id", nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        _int("seq"),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("event_uid", sa.String(200), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        _ts("cancelled_at", nullable=True),
        _ts("created_at"),
        sa.CheckConstraint("kind IN ('seed', 'event')", name=f"ck_{t}_kind_valid"),
        sa.CheckConstraint("ends_at > starts_at", name=f"ck_{t}_times_valid"),
        sa.CheckConstraint("kind = 'seed' OR event_uid IS NOT NULL", name=f"ck_{t}_event_has_uid"),
    )
    op.create_index(
        "uq_hire_calendar_blocks_seed",
        t,
        ["org_id", "user_id", "day", "seq"],
        unique=True,
        postgresql_where=sa.text("kind = 'seed'"),
    )
    op.create_index(
        "uq_hire_calendar_blocks_event",
        t,
        ["org_id", "user_id", "event_uid"],
        unique=True,
        postgresql_where=sa.text("kind = 'event'"),
    )
    op.create_index("ix_hire_calendar_blocks_user_time", t, ["org_id", "user_id", "starts_at"])
    _finish(t)

    t = "hire_calendar_connections"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        _user(t, "user_id", nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("account_email", sa.Text(), nullable=True),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("scopes", sa.String(500), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_error", sa.String(64), nullable=True),
        _ts("connected_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("org_id", "user_id", "provider", name="uq_hire_calendar_connections"),
        sa.CheckConstraint("provider IN ('google', 'microsoft')", name=f"ck_{t}_provider_valid"),
        sa.CheckConstraint("status IN ('active', 'revoked', 'error')", name=f"ck_{t}_status_valid"),
    )
    _finish(t)

    _seed_templates()


def _seed_templates() -> None:
    from pairly_core.notifications.templating import (
        TemplateSource,
        declarations_json,
        lint_source,
        parse_declarations,
    )
    from pairly_hire.scheduling.templates import SCHEDULING_TEMPLATES

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
    for t in SCHEDULING_TEMPLATES:
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
    for table in (
        "hire_calendar_connections",
        "hire_calendar_blocks",
        "hire_interview_panelists",
        "hire_interviews",
        "hire_interviewer_settings",
        "hire_scheduling_settings",
    ):
        op.drop_table(table)
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
