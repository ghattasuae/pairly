# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Decision kind ``human_override``: a person changed what an AI step produced.

Until now a person's override of an AI score was stored with the AI step's own kind
(``score``), so the compliance pack's overrides list and the human/AI split could not
tell them apart. ``human_override`` is a human kind (``HUMAN_KINDS``) that is still
replayable: its handler recomputes the deterministic result (for screening, the overall
score from the person's criterion scores and the locked rubric) without re-running a
model or a person.

Extends the kind check of ``decision_records``. Records are never updated, so existing
override records keep the kind they were written with (and replay as before). The
downgrade refuses while a ``human_override`` record exists.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BEFORE = (
    "'score', 'shortlist_suggestion', 'fraud_flag', 'risk_score', 'extraction', "
    "'extraction_correction', 'policy_check', 'approval', 'final_action', 'appeal_outcome'"
)
_AFTER = f"{_BEFORE}, 'human_override'"
# Migration 0012 named it ``ck_decision_records_kind_valid`` and the metadata naming
# convention (``ck_%(table_name)s_%(constraint_name)s``) prefixed it once more.
_NAME = "ck_decision_records_ck_decision_records_kind_valid"


def upgrade() -> None:
    op.execute(f"ALTER TABLE decision_records DROP CONSTRAINT {_NAME}")
    op.execute(f"ALTER TABLE decision_records ADD CONSTRAINT {_NAME} CHECK (kind IN ({_AFTER}))")


def downgrade() -> None:
    # Fails (check violation) while human_override records exist: records are never
    # rewritten to hide what kind of decision they were.
    op.execute(f"ALTER TABLE decision_records DROP CONSTRAINT {_NAME}")
    op.execute(f"ALTER TABLE decision_records ADD CONSTRAINT {_NAME} CHECK (kind IN ({_BEFORE}))")
