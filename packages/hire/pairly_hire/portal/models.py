# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The candidate portal tables (migration ``hire_0004b``). Org scoped, RLS forced, and a
restrictive policy keyed on the portal subject (``app.portal_subject``): a portal
session only ever sees its own candidate's rows (see the migration).

* ``hire_portal_tokens``: magic links. Only the SHA-256 of the secret (bound to the
  organisation, the candidate and the token id) is stored; single use (``used_at``),
  short expiry (``expires_at``).
* ``hire_portal_sessions``: a candidate's signed-in portal session (the cookie holds
  the secret, the row its hash). Absolute expiry, revocable.
* ``hire_data_requests``: requests from candidates to the organisation's admins, such as
  deleting their data. Never acted on automatically; an admin resolves each one.
* ``hire_document_requests``: a file the hiring team asked a candidate for, and the
  document the candidate uploaded through the portal.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped

TOKENS: Final = "hire_portal_tokens"
SESSIONS: Final = "hire_portal_sessions"
DATA_REQUESTS: Final = "hire_data_requests"
DOCUMENT_REQUESTS: Final = "hire_document_requests"


class DataRequestKind(StrEnum):
    DELETION = "deletion"


class DataRequestStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    DECLINED = "declined"


class DocumentRequestStatus(StrEnum):
    OPEN = "open"
    RECEIVED = "received"
    CANCELLED = "cancelled"


def _in(values: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def _candidate_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["org_id", "candidate_id"],
        ["hire_candidates.org_id", "hire_candidates.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_candidate_org",
    )


class HirePortalToken(Base, OrgScoped):
    __tablename__ = TOKENS
    __table_args__ = (
        _candidate_fk(TOKENS),
        CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_format"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        Index("ix_hire_portal_tokens_candidate", "org_id", "candidate_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # ``request`` (asked from the portal) or ``acknowledgement`` (sent after applying).
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    locale: Mapped[str] = mapped_column(String(2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HirePortalSession(Base, OrgScoped):
    __tablename__ = SESSIONS
    __table_args__ = (
        _candidate_fk(SESSIONS),
        CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_format"),
        Index("ix_hire_portal_sessions_candidate", "org_id", "candidate_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HireDataRequest(Base, OrgScoped):
    __tablename__ = DATA_REQUESTS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hire_data_requests_org_id_id"),
        _candidate_fk(DATA_REQUESTS),
        CheckConstraint(f"kind IN ({_in(DataRequestKind)})", name="kind_valid"),
        CheckConstraint(f"status IN ({_in(DataRequestStatus)})", name="status_valid"),
        CheckConstraint("(status = 'open') = (handled_at IS NULL)", name="handled_when_closed"),
        Index("ix_hire_data_requests_status", "org_id", "status", "created_at"),
        Index(
            "ix_hire_data_requests_erasure_pending",
            "org_id",
            "erasure_started_at",
            postgresql_where=text("status = 'open' AND erasure_started_at IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # The candidate's own words (optional), encrypted.
    note: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    handled_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # What the admin did and why, encrypted.
    outcome: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    # Erasure (``pairly_hire.erasure``): the database part committed at
    # ``erasure_started_at``; the request completes once the stored files are gone.
    # ``erasure_error`` says why they are not yet (shown to the admin; retried).
    erasure_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    erasure_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    erasure_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class HireDocumentRequest(Base, OrgScoped):
    __tablename__ = DOCUMENT_REQUESTS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hire_document_requests_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_document_requests_application_org",
        ),
        _candidate_fk(DOCUMENT_REQUESTS),
        CheckConstraint(f"status IN ({_in(DocumentRequestStatus)})", name="status_valid"),
        CheckConstraint(
            "status <> 'received' OR document_id IS NOT NULL OR checklist_item_id IS NOT NULL",
            name="received_has_document",
        ),
        Index("ix_hire_document_requests_application", "org_id", "application_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    label_ar: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # A pre-employment checklist document (hire_0015): the file is stored encrypted on
    # the checklist item (``pairly_hire.onboarding``), never as a shared document.
    checklist_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
