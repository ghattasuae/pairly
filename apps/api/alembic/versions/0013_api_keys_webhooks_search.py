# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Partial embedding failures are billed (status ``partial_error`` on ``model_calls``).

* ``model_calls.status`` also accepts ``partial_error``: a batched embedding call that
  failed after some batches were embedded (and billed by the provider) records the
  usage and cost of the successful batches.

The downgrade turns ``partial_error`` rows into ``provider_error`` (the cost stays).

Revision ID: 0013
Revises: 0010
Create Date: 2026-10-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CALL_STATUS_OLD = "status IN ('ok', 'invalid_output', 'low_confidence', 'provider_error')"
_CALL_STATUS_NEW = (
    "status IN ('ok', 'invalid_output', 'low_confidence', 'provider_error', 'partial_error')"
)


def _upgrade_model_calls() -> None:
    op.drop_constraint("ck_model_calls_status", "model_calls", type_="check")
    op.create_check_constraint("ck_model_calls_status", "model_calls", _CALL_STATUS_NEW)


def _downgrade_model_calls() -> None:
    # RLS is forced on the owner too: lift it for this migration's transaction.
    op.execute("ALTER TABLE model_calls NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE model_calls SET status = 'provider_error' WHERE status = 'partial_error'")
    op.execute("ALTER TABLE model_calls FORCE ROW LEVEL SECURITY")
    op.drop_constraint("ck_model_calls_status", "model_calls", type_="check")
    op.create_check_constraint("ck_model_calls_status", "model_calls", _CALL_STATUS_OLD)


def upgrade() -> None:
    _upgrade_model_calls()


def downgrade() -> None:
    _downgrade_model_calls()
