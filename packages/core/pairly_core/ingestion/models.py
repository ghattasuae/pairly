# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Tables for ingested documents and inbound emails (migration ``0004_documents``).

Both are tenant tables (RLS forced, standard tenant policy). The application role may
SELECT, INSERT and UPDATE; there is no DELETE grant (retention deletion is a later,
audited job).

Document status moves forward only:

    received -> scanning -> clean -> parsed
                         |        -> parse_failed
                         -> infected
    (rejected is set by a person or a policy after intake; intake refusals never
    create a row, they are audit events)

``needs_review`` is the human queue flag. It is set whenever a check could not run or
was not conclusive (scanner unreachable, parser crash or timeout, low OCR confidence,
truncated output). A document is never treated as clean or parsed because a check
failed to run.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped, Timestamped

_UUID_DEFAULT = text("gen_random_uuid()")


class DocumentStatus(StrEnum):
    RECEIVED = "received"
    SCANNING = "scanning"
    CLEAN = "clean"
    INFECTED = "infected"
    REJECTED = "rejected"
    PARSED = "parsed"
    PARSE_FAILED = "parse_failed"


class DocumentSource(StrEnum):
    UPLOAD = "upload"
    EMAIL = "email"
    API = "api"
    FORM = "form"


# States in which the original bytes passed a virus scan and may be handed out.
CONTENT_AVAILABLE = frozenset(
    {DocumentStatus.CLEAN, DocumentStatus.PARSED, DocumentStatus.PARSE_FAILED}
)


def _in(values: Any) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


class InboundEmail(Base, OrgScoped, Timestamped):
    """One received email, routed to one organisation and workflow."""

    __tablename__ = "inbound_emails"
    __table_args__ = (
        UniqueConstraint("org_id", "message_id", name="uq_inbound_emails_org_id_message_id"),
        CheckConstraint("workflow ~ '^[a-z][a-z0-9_]{0,31}$'", name="workflow_format"),
        CheckConstraint("raw_sha256 ~ '^[0-9a-f]{64}$'", name="raw_sha256_format"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    workflow: Mapped[str] = mapped_column(String(32), nullable=False)
    # Message-ID header (or a synthetic one from the raw hash when missing).
    message_id: Mapped[str] = mapped_column(String(998), nullable=False)
    raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Business contact data: needed to reply with a receipt and to check sender domains.
    # TODO(WP1.x encryption): move to application-level encryption with a blind index.
    sender_address: Mapped[str | None] = mapped_column(String(320), nullable=True)
    sender_domain: Mapped[str | None] = mapped_column(String(253), nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Senders, subjects and dates of forwarded messages, oldest last.
    forwarded_chain: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # Bulk or automated mail hints (List-Unsubscribe, Precedence: bulk, Auto-Submitted).
    is_bulk: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    attachment_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class Document(Base, OrgScoped, Timestamped):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("org_id", "sha256", name="uq_documents_org_id_sha256"),
        CheckConstraint(f"status IN ({_in(DocumentStatus)})", name="status_valid"),
        CheckConstraint(f"source IN ({_in(DocumentSource)})", name="source_valid"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
        CheckConstraint("workflow ~ '^[a-z][a-z0-9_]{0,31}$'", name="workflow_format"),
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        Index("ix_documents_org_id_status", "org_id", "status"),
        Index(
            "ix_documents_needs_review",
            "org_id",
            "created_at",
            postgresql_where=text("needs_review"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=_UUID_DEFAULT
    )
    workflow: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    # Sanitised, for display only. Never used to build a path or a storage key.
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Sniffed from the bytes, never taken from the client.
    media_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DocumentStatus.RECEIVED.value
    )
    status_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    needs_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    inbound_email_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("inbound_emails.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # Virus scan.
    scan_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scan_engine: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scan_signature: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Parsing.
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    parser: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parse_result_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    parse_warnings: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
