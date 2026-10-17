# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire reference checks (M3, brief 3.8.2 and 5.1.6).

Tables (``pairly_hire.references.models``), all tenant tables, RLS forced, standard
tenant policy, the application role gets SELECT, INSERT and UPDATE (never DELETE),
hidden from a candidate's portal session:

* ``hire_reference_requests``: one referee for one application; name, address and
  answers encrypted.
* ``hire_reference_links``: single use links (hash of the secret only).
* ``hire_reference_sessions``: the short session a used link opens, for one request.
* ``hire_reference_summaries``: kept summaries per application, language and answers
  hash (content encrypted).

The ``hire.referee.request`` email, English and Arabic, version 1.

Revision ID: hire_0013
Revises: hire_0012
Create Date: 2026-10-13
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0013"
down_revision: str | None = "hire_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SYSTEM_ACTOR = "pairly.migrations.hire_0013"
_TABLES = (
    "hire_reference_requests",
    "hire_reference_links",
    "hire_reference_sessions",
    "hire_reference_summaries",
)


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name=f"fk_{table}_org_id_organisations"
            ),
            nullable=False,
        ),
    ]


def _ts(name: str, *, nullable: bool = True) -> sa.Column[Any]:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _request_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id", "request_id"],
        ["hire_reference_requests.org_id", "hire_reference_requests.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_request_org",
    )


def upgrade() -> None:
    t = "hire_reference_requests"
    op.create_table(
        t,
        *_base(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("referee_name", sa.Text(), nullable=False),
        sa.Column("referee_email", sa.Text(), nullable=False),
        sa.Column("relationship", sa.String(16), nullable=False),
        sa.Column("locale", sa.String(2), nullable=False, server_default="en"),
        sa.Column("status", sa.String(16), nullable=False, server_default="requested"),
        sa.Column("answers", sa.Text(), nullable=True),
        sa.Column(
            "requested_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{t}_requested_by_users"),
            nullable=False,
        ),
        _ts("sent_at"),
        _ts("opened_at"),
        _ts("submitted_at"),
        _ts("erased_at"),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("org_id", "id", name="uq_hire_reference_requests_org_id_id"),
        sa.ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_reference_requests_application_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "candidate_id"],
            ["hire_candidates.org_id", "hire_candidates.id"],
            ondelete="RESTRICT",
            name="fk_hire_reference_requests_candidate_org",
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'in_progress', 'submitted', 'cancelled')",
            name=f"ck_{t}_status_valid",
        ),
        sa.CheckConstraint(
            "relationship IN ('manager', 'peer', 'direct_report', 'client', 'other')",
            name=f"ck_{t}_relationship_valid",
        ),
        sa.CheckConstraint("locale IN ('en', 'ar')", name=f"ck_{t}_locale_valid"),
        sa.CheckConstraint(
            "status <> 'submitted' OR submitted_at IS NOT NULL",
            name=f"ck_{t}_submitted_complete",
        ),
    )

    t = "hire_reference_links"
    op.create_table(
        t,
        *_base(t),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _ts("used_at"),
        _ts("created_at", nullable=False),
        _request_fk(t),
        sa.CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name=f"ck_{t}_secret_hash_format"),
    )

    t = "hire_reference_sessions"
    op.create_table(
        t,
        *_base(t),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("link_id", sa.Uuid(), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _ts("revoked_at"),
        _ts("created_at", nullable=False),
        _request_fk(t),
        sa.CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name=f"ck_{t}_secret_hash_format"),
    )

    t = "hire_reference_summaries"
    op.create_table(
        t,
        *_base(t),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("locale", sa.String(2), nullable=False),
        sa.Column("answers_hash", sa.String(64), nullable=False),
        sa.Column("request_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("model_call_id", sa.Uuid(), nullable=True),
        sa.Column("from_recording", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint(
            "org_id",
            "application_id",
            "locale",
            "answers_hash",
            name="uq_hire_reference_summaries_inputs",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_reference_summaries_application_org",
        ),
        sa.CheckConstraint("locale IN ('en', 'ar')", name=f"ck_{t}_locale_valid"),
        sa.CheckConstraint("answers_hash ~ '^[0-9a-f]{64}$'", name=f"ck_{t}_answers_hash_format"),
        sa.CheckConstraint("source IN ('model', 'template')", name=f"ck_{t}_source_valid"),
    )

    role = app_db_role()
    for table in _TABLES:
        op.create_index(f"ix_{table}_org_id", table, ["org_id"])
        for stmt in org_rls_statements(table, role, privileges="SELECT, INSERT, UPDATE"):
            op.execute(stmt)
        op.execute(
            f"CREATE POLICY {table}_portal_subject ON {table} AS RESTRICTIVE "
            "USING (NOT hire_portal_active()) WITH CHECK (NOT hire_portal_active())"
        )

    from pairly_hire.interviewing.template_seed import insert_templates
    from pairly_hire.references.templates import REFEREE_TEMPLATES

    insert_templates(op.get_bind(), REFEREE_TEMPLATES, version=1, actor=_SYSTEM_ACTOR)


def downgrade() -> None:
    from pairly_hire.interviewing.template_seed import remove_templates

    remove_templates(op.get_bind(), actor=_SYSTEM_ACTOR)
    for table in reversed(_TABLES):
        op.drop_table(table)
