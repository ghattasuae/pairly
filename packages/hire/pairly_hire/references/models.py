# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Reference check tables (migration ``hire_0013``). All org scoped, Row Level Security
forced, hidden from candidates' portal sessions; the application role gets SELECT,
INSERT and UPDATE, never DELETE.

* ``hire_reference_requests``: one referee for one application. The referee's name and
  email address and their answers (draft or submitted) are encrypted. ``status``:
  ``requested`` (sent, not opened), ``in_progress`` (opened or saved), ``submitted``,
  ``cancelled``. A submitted reference never changes (the service refuses).
* ``hire_reference_links``: the referee's single use link (hash of the secret only),
  bound to the organisation, the request (so the referee and the application) and the
  link row; short expiry.
* ``hire_reference_sessions``: what a used link opens: a short session for that one
  request (the referee is never a user).
* ``hire_reference_summaries``: the summary of an application's submitted references,
  per language, keyed by the hash of the answers it summarises. Its content is
  encrypted (it repeats what referees said).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedJSON, EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped

REQUESTS: Final = "hire_reference_requests"
LINKS: Final = "hire_reference_links"
SESSIONS: Final = "hire_reference_sessions"
SUMMARIES: Final = "hire_reference_summaries"
RELATIONSHIPS: Final = ("manager", "peer", "direct_report", "client", "other")


class ReferenceStatus(StrEnum):
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    CANCELLED = "cancelled"


OPEN_STATUSES: Final = (ReferenceStatus.REQUESTED.value, ReferenceStatus.IN_PROGRESS.value)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class HireReferenceRequest(Base, OrgScoped):
    __tablename__ = REQUESTS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hire_reference_requests_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_reference_requests_application_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "candidate_id"],
            ["hire_candidates.org_id", "hire_candidates.id"],
            ondelete="RESTRICT",
            name="fk_hire_reference_requests_candidate_org",
        ),
        CheckConstraint(
            "status IN ('requested', 'in_progress', 'submitted', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint(
            "relationship IN ('manager', 'peer', 'direct_report', 'client', 'other')",
            name="relationship_valid",
        ),
        CheckConstraint("locale IN ('en', 'ar')", name="locale_valid"),
        CheckConstraint(
            "status <> 'submitted' OR submitted_at IS NOT NULL", name="submitted_complete"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    referee_name: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    referee_email: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    relationship: Mapped[str] = mapped_column(String(16), nullable=False)
    locale: Mapped[str] = mapped_column(String(2), nullable=False, server_default="en")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="requested")
    answers: Mapped[Sealed | None] = mapped_column(EncryptedJSON(), nullable=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


def _request_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["org_id", "request_id"],
        ["hire_reference_requests.org_id", "hire_reference_requests.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_request_org",
    )


class HireReferenceLink(Base, OrgScoped):
    __tablename__ = LINKS
    __table_args__ = (
        _request_fk(LINKS),
        CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_format"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts()


class HireReferenceSession(Base, OrgScoped):
    __tablename__ = SESSIONS
    __table_args__ = (
        _request_fk(SESSIONS),
        CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_format"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    link_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts()


class HireReferenceSummary(Base, OrgScoped):
    __tablename__ = SUMMARIES
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "application_id",
            "locale",
            "answers_hash",
            name="uq_hire_reference_summaries_inputs",
        ),
        ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_reference_summaries_application_org",
        ),
        CheckConstraint("locale IN ('en', 'ar')", name="locale_valid"),
        CheckConstraint("answers_hash ~ '^[0-9a-f]{64}$'", name="answers_hash_format"),
        CheckConstraint("source IN ('model', 'template')", name="source_valid"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    locale: Mapped[str] = mapped_column(String(2), nullable=False)
    answers_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), nullable=False)
    content: Mapped[Sealed] = mapped_column(EncryptedJSON(), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    from_recording: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = _ts()


def as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
