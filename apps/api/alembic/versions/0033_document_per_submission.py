# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Documents: a submission may keep its own row whatever else holds the same bytes.

``documents`` gains ``dedupe`` (default true): whether later identical bytes in the
workflow link to this row. Intake dedupe (0004's unique ``(org_id, workflow, sha256)``)
becomes a partial unique index over the rows that take part in it
(``uq_documents_org_id_workflow_sha256 ... WHERE dedupe``), so a row stored with
``dedupe = false`` (a candidate's CV, one row per application) never conflicts with and
is never found by another upload of the same bytes. The stored object stays one per
organisation and hash (security review 2026-10, L-09). Existing rows keep
``dedupe = true``; ``hire_0022`` gives the Hire files that several applications share a
row each.

Downgrade restores the unique constraint, which needs at most one row per organisation,
workflow and hash: it refuses while rows kept for one submission repeat a hash.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "uq_documents_org_id_workflow_sha256"


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("dedupe", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.drop_constraint(_NAME, "documents", type_="unique")
    op.create_index(
        _NAME,
        "documents",
        ["org_id", "workflow", "sha256"],
        unique=True,
        postgresql_where=sa.text("dedupe"),
    )


def downgrade() -> None:
    op.drop_index(_NAME, table_name="documents")
    op.create_unique_constraint(_NAME, "documents", ["org_id", "workflow", "sha256"])
    op.drop_column("documents", "dedupe")
