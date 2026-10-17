# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The offer table (migration ``hire_0014``). Org scoped, Row Level Security forced,
hidden from candidates' portal sessions except through the portal's own narrow
service (an RLS policy lets a portal session read its own sent offers); the
application role gets SELECT, INSERT and UPDATE, never DELETE.

``hire_offers``: one offer for one application. ``compensation`` (monthly amounts,
currency, bonus target, the annual total) is encrypted and shown only to people with
``hire.offer.compensation``. ``status``::

    draft -> pending_approval -> approved -> sending -> accepted | declined
                              -> changes_requested -> (edit) -> pending_approval
                              -> rejected | failed
    draft, changes_requested, approved, sending -> withdrawn

``message_id``: the offer email, drafted and frozen at submission; the offer chain's
approval binds its exact rendered hash. ``sending``: that email was queued under the
same approval (no second approval); the candidate sees the offer once it was sent.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.types import EncryptedJSON, EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped

OFFERS: Final = "hire_offers"


class OfferStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    SENDING = "sending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


EDITABLE: Final = (OfferStatus.DRAFT.value, OfferStatus.CHANGES_REQUESTED.value)
LIVE: Final = (
    OfferStatus.DRAFT.value,
    OfferStatus.PENDING_APPROVAL.value,
    OfferStatus.CHANGES_REQUESTED.value,
    OfferStatus.APPROVED.value,
    OfferStatus.SENDING.value,
)


def _in(values: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


class HireOffer(Base, OrgScoped):
    __tablename__ = OFFERS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hire_offers_org_id_id"),
        UniqueConstraint("org_id", "reference", name="uq_hire_offers_reference"),
        ForeignKeyConstraint(
            ["org_id", "application_id"],
            ["hire_applications.org_id", "hire_applications.id"],
            ondelete="RESTRICT",
            name="fk_hire_offers_application_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "candidate_id"],
            ["hire_candidates.org_id", "hire_candidates.id"],
            ondelete="RESTRICT",
            name="fk_hire_offers_candidate_org",
        ),
        CheckConstraint(f"status IN ({_in(OfferStatus)})", name="status_valid"),
        CheckConstraint("locale IN ('en', 'ar')", name="locale_valid"),
        CheckConstraint("jsonb_typeof(conditions) = 'array'", name="conditions_array"),
        CheckConstraint("reference ~ '^OFR-[A-Z0-9]{6}$'", name="reference_format"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "status NOT IN ('accepted', 'declined') OR (responded_at IS NOT NULL "
            "AND response_record_id IS NOT NULL)",
            name="response_complete",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    reference: Mapped[str] = mapped_column(String(10), nullable=False)
    template_key: Mapped[str] = mapped_column(String(32), nullable=False)
    locale: Mapped[str] = mapped_column(String(2), nullable=False, server_default="en")
    job_title: Mapped[str] = mapped_column(String(160), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    compensation: Mapped[Sealed] = mapped_column(EncryptedJSON(), nullable=False)
    conditions: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft")
    status_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_record_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_note: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def as_list(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []
