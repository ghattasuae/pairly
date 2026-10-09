# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire blind mode: one reveal state per application.

Before this revision two flags said whether reviewers may see who a candidate is: the
screening's ``blind`` (lifted by the shortlist approval) and the pipeline entry's
``revealed`` (set by a move into a stage that reveals identity). They could disagree.
Now one column on the application holds it:

* ``hire_applications.identity_revealed_at`` and ``identity_revealed_by``
  (``shortlist`` or ``stage:<key>``): set once, by whichever comes first, never
  cleared (what a person has seen cannot be unseen). ``pairly_hire.reveal``.

Backfill: the moment either old flag says the identity was revealed (the screening's
``revealed_at``, or the entry's ``revealed`` with its ``stage_entered_at``). Then the
entry's ``revealed`` column is dropped. ``hire_screenings.blind`` now only records
whether the screening ran blind (what the scoring model read).

Revision ID: hire_0004a
Revises: hire_0003
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "hire_0004a"
down_revision: str | None = "hire_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "hire_applications",
        sa.Column("identity_revealed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "hire_applications", sa.Column("identity_revealed_by", sa.String(40), nullable=True)
    )
    op.create_check_constraint(
        "ck_hire_applications_identity_revealed_pair",
        "hire_applications",
        "(identity_revealed_at IS NULL) = (identity_revealed_by IS NULL)",
    )
    op.create_check_constraint(
        "ck_hire_applications_identity_revealed_by_format",
        "hire_applications",
        "identity_revealed_by IS NULL OR identity_revealed_by ~ "
        "'^(shortlist|stage:[a-z][a-z0-9_]{0,31})$'",
    )
    # Backfill as the owner (RLS is forced, so lift it for these statements only).
    op.execute("ALTER TABLE hire_applications NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hire_screenings NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hire_pipeline_entries NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        UPDATE hire_applications a
           SET identity_revealed_at = s.revealed_at, identity_revealed_by = 'shortlist'
          FROM (SELECT application_id, org_id, min(revealed_at) AS revealed_at
                  FROM hire_screenings WHERE revealed_at IS NOT NULL
                 GROUP BY application_id, org_id) s
         WHERE s.application_id = a.id AND s.org_id = a.org_id
           AND a.identity_revealed_at IS NULL
        """
    )
    op.execute(
        """
        UPDATE hire_applications a
           SET identity_revealed_at = e.stage_entered_at,
               identity_revealed_by = 'stage:' || e.stage_key
          FROM hire_pipeline_entries e
         WHERE e.application_id = a.id AND e.org_id = a.org_id AND e.revealed
           AND a.identity_revealed_at IS NULL
        """
    )
    op.execute("ALTER TABLE hire_pipeline_entries FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hire_screenings FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hire_applications FORCE ROW LEVEL SECURITY")
    op.drop_column("hire_pipeline_entries", "revealed")


def downgrade() -> None:
    op.add_column(
        "hire_pipeline_entries",
        sa.Column("revealed", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.execute("ALTER TABLE hire_pipeline_entries NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hire_applications NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        UPDATE hire_pipeline_entries e SET revealed = true
          FROM hire_applications a
         WHERE a.id = e.application_id AND a.org_id = e.org_id
           AND a.identity_revealed_at IS NOT NULL
        """
    )
    op.execute("ALTER TABLE hire_applications FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hire_pipeline_entries FORCE ROW LEVEL SECURITY")
    op.drop_constraint(
        "ck_hire_applications_identity_revealed_by_format", "hire_applications", type_="check"
    )
    op.drop_constraint(
        "ck_hire_applications_identity_revealed_pair", "hire_applications", type_="check"
    )
    op.drop_column("hire_applications", "identity_revealed_by")
    op.drop_column("hire_applications", "identity_revealed_at")
