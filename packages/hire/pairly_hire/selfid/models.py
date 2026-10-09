# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The ``hire_self_identification`` table (migration ``hire_0001``). Org scoped, RLS forced.

One row per application that answered at least one self-identification question. The
answers are encrypted (``EncryptedJSON``); the application role may insert and read
rows but never change them. Staff reading needs ``hire.selfid.read`` and only ever produces
aggregates with small counts suppressed (``pairly_hire.api.reports``); the candidate's own
data export reads their rows through ``pairly_hire.selfid.export`` (portal session, Row
Level Security by portal subject).

Kept in its own module tree so the scoring code cannot import it (see the package
docstring).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedJSON, Sealed
from pairly_core.db import Base, OrgScoped

TABLE = "hire_self_identification"


class HireSelfIdentification(Base, OrgScoped):
    __tablename__ = TABLE
    __table_args__ = (
        UniqueConstraint("org_id", "application_id", name="uq_hire_self_identification_app"),
        ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="CASCADE",
            name="fk_hire_self_identification_application_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["hire_jobs.org_id", "hire_jobs.id"],
            ondelete="RESTRICT",
            name="fk_hire_self_identification_job_org",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # For per-job aggregates without touching the application row.
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    answers: Mapped[Sealed] = mapped_column(EncryptedJSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
