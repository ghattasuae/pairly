# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire offers (M3, brief 3.8.3).

``hire_offers`` (``pairly_hire.offers.models``): one offer for one application, with the
compensation encrypted. Tenant table, RLS forced, standard tenant policy; the
application role gets SELECT, INSERT and UPDATE, never DELETE. A candidate's portal
session reads and answers only their own offers (restrictive policy by portal subject);
the portal service shows only offers whose email was sent.

Depends on core ``0020`` (decision records by a candidate: the answer to an offer).

Revision ID: hire_0014
Revises: hire_0013
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "hire_0014"
down_revision: str | None = "hire_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = "0020"

_T = "hire_offers"


def _user(column: str, *, nullable: bool) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        column,
        sa.Uuid(),
        sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{_T}_{column}_users"),
        nullable=nullable,
    )


def _ts(name: str) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    op.create_table(
        _T,
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name=f"fk_{_T}_org_id_organisations"
            ),
            nullable=False,
        ),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("reference", sa.String(10), nullable=False),
        sa.Column("template_key", sa.String(32), nullable=False),
        sa.Column("locale", sa.String(2), nullable=False, server_default="en"),
        sa.Column("job_title", sa.String(160), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("compensation", sa.Text(), nullable=False),
        sa.Column("conditions", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("status_reason", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        _user("created_by", nullable=False),
        _user("submitted_by", nullable=True),
        _ts("approved_at"),
        _ts("sent_at"),
        _ts("responded_at"),
        sa.Column("response_record_id", sa.String(64), nullable=True),
        sa.Column("response_note", sa.Text(), nullable=True),
        _ts("erased_at"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("org_id", "id", name="uq_hire_offers_org_id_id"),
        sa.UniqueConstraint("org_id", "reference", name="uq_hire_offers_reference"),
        sa.ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_offers_application_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "candidate_id"],
            ["hire_candidates.org_id", "hire_candidates.id"],
            ondelete="RESTRICT",
            name="fk_hire_offers_candidate_org",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'changes_requested', 'approved', "
            "'rejected', 'failed', 'sending', 'accepted', 'declined', 'withdrawn')",
            name=f"ck_{_T}_status_valid",
        ),
        sa.CheckConstraint("locale IN ('en', 'ar')", name=f"ck_{_T}_locale_valid"),
        sa.CheckConstraint("jsonb_typeof(conditions) = 'array'", name=f"ck_{_T}_conditions_array"),
        sa.CheckConstraint("reference ~ '^OFR-[A-Z0-9]{6}$'", name=f"ck_{_T}_reference_format"),
        sa.CheckConstraint("version >= 1", name=f"ck_{_T}_version_positive"),
        sa.CheckConstraint(
            "status NOT IN ('accepted', 'declined') OR (responded_at IS NOT NULL "
            "AND response_record_id IS NOT NULL)",
            name=f"ck_{_T}_response_complete",
        ),
    )
    op.create_index(f"ix_{_T}_org_id", _T, ["org_id"])
    for stmt in org_rls_statements(_T, app_db_role(), privileges="SELECT, INSERT, UPDATE"):
        op.execute(stmt)
    own = "NOT hire_portal_active() OR candidate_id = hire_portal_candidate()"
    op.execute(
        f"CREATE POLICY {_T}_portal_subject ON {_T} AS RESTRICTIVE USING ({own}) WITH CHECK ({own})"
    )


def downgrade() -> None:
    op.drop_table(_T)
