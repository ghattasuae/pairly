# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The vendor portal tables (migration ``pay_0012``). Org scoped, RLS forced, and a
restrictive policy keyed on the portal subject (``app.portal_subject``,
``pay_vendor:<vendor id>``): a portal session only ever sees its own vendor's rows.

* ``pay_portal_contacts``: who at a vendor may sign in to the portal. Added by the AP
  team from the vendor's contacts on file (never an address typed in by anyone else).
  Name and email are encrypted; ``email_bidx`` (HMAC blind index) finds the contact by
  address without decrypting. At most one active contact per address per organisation,
  so an address always signs in to one vendor.
* ``pay_portal_tokens``: magic links. Only the SHA-256 of the secret (bound to the
  organisation, the contact, the vendor and the token id) is stored; single use
  (``used_at``), short expiry (``expires_at``).
* ``pay_portal_sessions``: a contact's signed-in portal session (the cookie holds the
  secret, the row its hash). Absolute expiry, revocable (also when the contact is).
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
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.blind_index import blind_index_info
from pairly_core.crypto.types import EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped

CONTACTS: Final = "pay_portal_contacts"
TOKENS: Final = "pay_portal_tokens"
SESSIONS: Final = "pay_portal_sessions"


class ContactStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _ts() -> Mapped[datetime | None]:
    return mapped_column(DateTime(timezone=True), nullable=True)


def _vendor_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["org_id", "vendor_id"],
        ["pay_vendors.org_id", "pay_vendors.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_vendor_org",
    )


def _contact_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["org_id", "contact_id"],
        ["pay_portal_contacts.org_id", "pay_portal_contacts.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_contact_org",
    )


class PayPortalContact(Base, OrgScoped):
    __tablename__ = CONTACTS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_portal_contacts_org_id_id"),
        _vendor_fk(CONTACTS),
        CheckConstraint("status IN ('active', 'revoked')", name="status_valid"),
        CheckConstraint("email_bidx ~ '^[0-9a-f]{64}$'", name="email_bidx_format"),
        CheckConstraint("locale IN ('en', 'ar')", name="locale_valid"),
        CheckConstraint("(status = 'revoked') = (revoked_at IS NOT NULL)", name="revoked_when"),
        Index("ix_pay_portal_contacts_vendor", "org_id", "vendor_id", "created_at"),
        Index(
            "uq_pay_portal_contacts_active_email",
            "org_id",
            "email_bidx",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    email: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    email_bidx: Mapped[str] = mapped_column(
        String(64), nullable=False, info=blind_index_info("email", "email")
    )
    locale: Mapped[str] = mapped_column(String(2), nullable=False, default="en")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = _created()
    revoked_at: Mapped[datetime | None] = _ts()
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    last_sign_in_at: Mapped[datetime | None] = _ts()


class PayPortalToken(Base, OrgScoped):
    __tablename__ = TOKENS
    __table_args__ = (
        _vendor_fk(TOKENS),
        _contact_fk(TOKENS),
        CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_format"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint("purpose IN ('request', 'invite')", name="purpose_valid"),
        Index("ix_pay_portal_tokens_contact", "org_id", "contact_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    contact_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # ``request`` (asked from the portal) or ``invite`` (sent when AP added the contact).
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    locale: Mapped[str] = mapped_column(String(2), nullable=False)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = _ts()


class PayPortalSession(Base, OrgScoped):
    __tablename__ = SESSIONS
    __table_args__ = (
        _vendor_fk(SESSIONS),
        _contact_fk(SESSIONS),
        CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_format"),
        Index("ix_pay_portal_sessions_contact", "org_id", "contact_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    contact_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = _ts()
    revoked_at: Mapped[datetime | None] = _ts()
