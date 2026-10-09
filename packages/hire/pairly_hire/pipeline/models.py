# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The pipeline tables (migration ``hire_0003``). All org scoped, Row Level Security forced.

* ``hire_pipeline_configs``: a job's customised stages, one row per job (``stages`` is
  the ordered list, validated by ``pairly_hire.pipeline.stages``). A job without a row
  uses the default stages.
* ``hire_pipeline_entries``: where one application stands: its stage, when it got
  there, its tags, when the candidate last heard from us (``last_update_at``, for the
  anti-ghosting SLA). An application without a row is in the first stage (``applied``)
  and never had an update. Whether its identity was revealed (blind mode) lives on the
  application (``hire_applications.identity_revealed_at``, ``pairly_hire.reveal``).
* ``hire_stage_moves``: every stage change, append only (SELECT and INSERT for the
  application role). An undo is a new row pointing at the move it reverts
  (``undo_of``); a bulk action shares one ``batch_id``.
* ``hire_notes``: notes on an application with the people they mention, encrypted,
  append only.
* ``hire_referral_links``: one referral link per employee and job.
* ``hire_referrals``: which application came through which link, append only.

Nothing here holds self-identification answers (``pairly_hire.selfid``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped

CONFIGS = "hire_pipeline_configs"
ENTRIES = "hire_pipeline_entries"
MOVES = "hire_stage_moves"
NOTES = "hire_notes"
REFERRAL_LINKS = "hire_referral_links"
REFERRALS = "hire_referrals"

STAGE_KEY_PATTERN = "^[a-z][a-z0-9_]{0,31}$"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _job_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["org_id", "job_id"],
        ["hire_jobs.org_id", "hire_jobs.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_job_org",
    )


def _application_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["org_id", "application_id"],
        ["hire_applications.org_id", "hire_applications.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_application_org",
    )


class HirePipelineConfig(Base, OrgScoped):
    __tablename__ = CONFIGS
    __table_args__ = (
        UniqueConstraint("org_id", "job_id", name="uq_hire_pipeline_configs_job"),
        _job_fk(CONFIGS),
        CheckConstraint("jsonb_typeof(stages) = 'array'", name="stages_array"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # [{"key", "label", "label_ar", "kind", "reveal_identity"}, ...] in board order.
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class HirePipelineEntry(Base, OrgScoped):
    __tablename__ = ENTRIES
    __table_args__ = (
        UniqueConstraint("org_id", "application_id", name="uq_hire_pipeline_entries_application"),
        _application_fk(ENTRIES),
        _job_fk(ENTRIES),
        CheckConstraint(f"stage_key ~ '{STAGE_KEY_PATTERN}'", name="stage_key_format"),
        CheckConstraint("jsonb_typeof(tags) = 'array'", name="tags_array"),
        Index("ix_hire_pipeline_entries_job_stage", "org_id", "job_id", "stage_key"),
        Index("ix_hire_pipeline_entries_waiting", "org_id", "last_update_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    stage_key: Mapped[str] = mapped_column(String(32), nullable=False)
    stage_entered_at: Mapped[datetime] = _created()
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # The last message the candidate got from us (NULL: none since applying).
    last_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the anti-ghosting SLA last drafted an update for this application.
    sla_drafted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class HireStageMove(Base, OrgScoped):
    __tablename__ = MOVES
    __table_args__ = (
        _application_fk(MOVES),
        _job_fk(MOVES),
        CheckConstraint(f"to_stage ~ '{STAGE_KEY_PATTERN}'", name="to_stage_format"),
        CheckConstraint(
            "actor_type IN ('user', 'agent', 'api_key', 'system')", name="actor_type_valid"
        ),
        Index("ix_hire_stage_moves_application", "org_id", "application_id", "created_at"),
        Index("ix_hire_stage_moves_batch", "org_id", "batch_id"),
        Index(
            "uq_hire_stage_moves_undo",
            "undo_of",
            unique=True,
            postgresql_where=text("undo_of IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    from_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    to_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Why (optional, shown in the timeline): encrypted, it can name a person.
    reason: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    undo_of: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("hire_stage_moves.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = _created()


class HireNote(Base, OrgScoped):
    __tablename__ = NOTES
    __table_args__ = (
        _application_fk(NOTES),
        CheckConstraint("jsonb_typeof(mentions) = 'array'", name="mentions_array"),
        Index("ix_hire_notes_application", "org_id", "application_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    author_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    body: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    # User ids of the members the note mentions (they were told in the app).
    mentions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = _created()


class HireReferralLink(Base, OrgScoped):
    __tablename__ = REFERRAL_LINKS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hire_referral_links_org_id_id"),
        UniqueConstraint(
            "org_id", "job_id", "referrer_id", name="uq_hire_referral_links_job_referrer"
        ),
        _job_fk(REFERRAL_LINKS),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    referrer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = _created()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HireReferral(Base, OrgScoped):
    __tablename__ = REFERRALS
    __table_args__ = (
        UniqueConstraint("org_id", "application_id", name="uq_hire_referrals_application"),
        _application_fk(REFERRALS),
        _job_fk(REFERRALS),
        ForeignKeyConstraint(
            ["org_id", "link_id"],
            ["hire_referral_links.org_id", "hire_referral_links.id"],
            ondelete="RESTRICT",
            name="fk_hire_referrals_link_org",
        ),
        Index("ix_hire_referrals_referrer", "org_id", "referrer_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    link_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    referrer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = _created()
