# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Retention with automatic deletion (brief 6.9, ``pairly_core.retention``).

* ``retention_reports`` gains ``scheduled_for`` (when what it counts may be deleted),
  ``enabled`` (the rule was on) and ``deleted`` (what the run deleted; the application
  role may UPDATE that one column).
* ``retention_rule_settings``: per organisation and rule, on or off and the period in
  days. SELECT, INSERT, UPDATE for the application role.
* ``retention_org_settings``: the daily cap of subjects deleted (default 200). SELECT,
  INSERT, UPDATE.
* ``retention_schedule``: the preview list (each due subject, listed seven days before
  it may be deleted, held, erasing, deleted). SELECT, INSERT, UPDATE, DELETE (a subject
  no longer due leaves the list).
* ``retention_holds``: legal holds on a candidate or a vendor, the reason encrypted.
  SELECT, INSERT, UPDATE (released, never deleted).

All org scoped with Row Level Security forced and closed to portal sessions.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-16
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import app_db_role, org_rls_statements, portal_guard_statements

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RULES = "retention_rule_settings"
ORG = "retention_org_settings"
SCHEDULE = "retention_schedule"
HOLDS = "retention_holds"


def _id() -> sa.Column[Any]:
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


def _user(table: str, column: str, *, nullable: bool = True) -> sa.Column[Any]:
    return sa.Column(
        column,
        sa.Uuid(),
        sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{table}_{column}_users"),
        nullable=nullable,
    )


def _ts(column: str, *, nullable: bool = True, now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        column,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if now else None,
    )


def upgrade() -> None:
    role = app_db_role()
    op.add_column("retention_reports", _ts("scheduled_for"))
    op.add_column(
        "retention_reports",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "retention_reports",
        sa.Column("deleted", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_retention_reports_deleted_non_negative", "retention_reports", "deleted >= 0"
    )
    # A run records what it deleted on its own report rows; nothing else of a report
    # ever changes.
    op.execute(f"GRANT UPDATE (deleted) ON retention_reports TO {role}")

    op.create_table(
        RULES,
        _id(),
        _org(RULES),
        sa.Column("rule", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("days", sa.Integer(), nullable=True),
        _user(RULES, "updated_by"),
        _ts("updated_at", nullable=False, now=True),
        sa.UniqueConstraint("org_id", "rule", name="uq_retention_rule_settings_org_rule"),
        sa.CheckConstraint(
            "days IS NULL OR days BETWEEN 0 AND 36500", name="ck_retention_rule_settings_days_range"
        ),
    )
    op.create_table(
        ORG,
        _id(),
        _org(ORG),
        sa.Column("daily_cap", sa.Integer(), nullable=False, server_default="200"),
        _user(ORG, "updated_by"),
        _ts("updated_at", nullable=False, now=True),
        sa.UniqueConstraint("org_id", name="uq_retention_org_settings_org"),
        sa.CheckConstraint(
            "daily_cap BETWEEN 1 AND 10000", name="ck_retention_org_settings_daily_cap_range"
        ),
    )
    op.create_table(
        SCHEDULE,
        _id(),
        _org(SCHEDULE),
        sa.Column("rule", sa.String(100), nullable=False),
        sa.Column("subject_type", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(64), nullable=True),
        _ts("due_at", nullable=False),
        _ts("listed_at", nullable=False),
        _ts("delete_after", nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(64), nullable=True),
        sa.Column("decision_id", sa.String(32), nullable=True),
        _ts("erased_at"),
        _ts("deleted_at"),
        _ts("updated_at", nullable=False, now=True),
        sa.UniqueConstraint(
            "org_id", "rule", "subject_id", name="uq_retention_schedule_org_rule_subject"
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'held', 'erasing', 'deleted')",
            name="ck_retention_schedule_status_valid",
        ),
        sa.CheckConstraint(
            "delete_after >= listed_at", name="ck_retention_schedule_preview_after_listing"
        ),
        sa.CheckConstraint(
            "(status = 'deleted') = (deleted_at IS NOT NULL)",
            name="ck_retention_schedule_deleted_when_done",
        ),
    )
    op.create_index(
        "ix_retention_schedule_org_id_status", SCHEDULE, ["org_id", "status", "delete_after"]
    )
    op.create_table(
        HOLDS,
        _id(),
        _org(HOLDS),
        sa.Column("subject_type", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        _user(HOLDS, "placed_by", nullable=False),
        _ts("placed_at", nullable=False, now=True),
        _user(HOLDS, "released_by"),
        _ts("released_at"),
        sa.CheckConstraint(
            "(released_at IS NULL) = (released_by IS NULL)",
            name="ck_retention_holds_released_by_person",
        ),
    )
    op.create_index(
        "ix_retention_holds_active",
        HOLDS,
        ["org_id", "subject_type", "subject_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    for table, privileges in (
        (RULES, "SELECT, INSERT, UPDATE"),
        (ORG, "SELECT, INSERT, UPDATE"),
        (SCHEDULE, "SELECT, INSERT, UPDATE, DELETE"),
        (HOLDS, "SELECT, INSERT, UPDATE"),
    ):
        op.create_index(f"ix_{table}_org_id", table, ["org_id"])
        for statement in org_rls_statements(table, role, privileges=privileges):
            op.execute(statement)
        for statement in portal_guard_statements(table):
            op.execute(statement)


def downgrade() -> None:
    op.execute(f"REVOKE UPDATE (deleted) ON retention_reports FROM {app_db_role()}")
    for table in (HOLDS, SCHEDULE, ORG, RULES):
        op.drop_table(table)
    # Dropping ``deleted`` drops its check constraint too.
    for column in ("deleted", "enabled", "scheduled_for"):
        op.drop_column("retention_reports", column)
