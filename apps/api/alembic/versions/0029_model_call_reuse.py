# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Response reuse in live mode: ``model_calls.reused_from``.

A live request whose hash the organisation already had answered reuses that answer
instead of calling the model (``pairly_core.llm.router``, ``PAIRLY_LLM_REUSE_RESPONSES``
and ``PAIRLY_LLM_REUSE_TTL_DAYS``). The new row records the call it reused. Nullable,
no default: every existing row reused nothing. ``model_calls`` stays append-only (the
application role has no UPDATE on this column) and the erasure guard of 0019 compares
every column but ``request``, ``response`` and ``erased_at``, so it covers this one.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("model_calls", sa.Column("reused_from", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_calls", "reused_from")
