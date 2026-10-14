# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pay tables of the vendor master and the bank-detail change workflow (``pay_0001``).

All org scoped with Row Level Security forced. The application role gets SELECT, INSERT
and UPDATE (never DELETE); call records are insert only. Children point at their
parents by ``(org_id, id)``, so no row can reference another organisation's.

* ``pay_vendors``: the vendor master. Contacts and the phone number on file are
  personal data, encrypted (the phone also has a blind index for dedupe). The payment
  hold lives here (``payment_hold`` plus why, see ``pairly_pay.holds``).
* ``pay_vendor_bank_accounts``: bank details, one row per set of details, never edited.
  IBAN and account name are encrypted; ``iban_bidx`` (HMAC blind index) finds the same
  IBAN across vendors without decrypting. A trigger refuses any change to the plain
  columns that identify the account; a change of details is always a new row through a
  change ticket. At most one ``active`` row per vendor.
* ``pay_bank_change_tickets``: the locked change ticket (brief 4.1.4). It snapshots the
  on-file phone number at opening (the only number anyone may call), binds the proposed
  details with a keyed digest, and records the verification, the approval run, the
  cooling-off period and how it ended. At most one open ticket per vendor.
* ``pay_bank_change_calls``: every call back attempt (insert only).
* ``pay_vendor_invites`` and ``pay_vendor_onboarding_sessions``: the signed single-use
  onboarding link and the short session it opens (``pairly_pay.onboarding``).
* ``pay_settings``: per organisation settings (cooling-off hours).

Invoices (``pay_0002``, same rules):

* ``pay_invoices``: one row per invoice document (file hash dedupe: unique per
  document), its status lifecycle, source, vendor link and how it was found, the
  current header values and why it needs a person.
* ``pay_intake_emails``: how Pay classified an inbound email (invoice, marketing,
  unsure) and the receipt it sent.
* ``pay_invoice_corrections``: corrections filed per vendor, insert only.

Checks (``pay_0003``): ``pay_invoices`` gains the comparison keys, the check results and
the risk score of ``pairly_pay.checks``; ``pay_settings`` gains ``check_settings``.

Purchase orders (``pay_0007``, same rules, ``pairly_pay.purchase_orders``):

* ``pay_purchase_orders`` and ``pay_purchase_order_lines``: what was ordered from a
  vendor (description, quantity, unit price, in the PO's currency), its status and
  whether invoices match it 2-way (PO) or 3-way (PO and goods received).
* ``pay_goods_receipts`` and ``pay_goods_receipt_lines``: what arrived, per PO line.
* ``pay_invoices`` gains the PO it was matched with (``po_id``) and how its lines were
  allocated to the PO lines (``po_match``), for the remaining quantity of later invoices.

GL coding (``pay_0009``, same rules, ``pairly_pay.coding``):

* ``pay_coding_lists`` and ``pay_coding_entries``: versions of the chart of accounts and
  of the cost centres (insert only; the newest version of each is in force).
* ``pay_invoice_line_codings``: the GL account and cost centre of each invoice line,
  with the list versions and the suggestion of the time (what later suggestions learn
  from).
* ``pay_coding_corrections``: a person chose another code than the suggestion (insert
  only).

Purchase orders gain who closed or cancelled them, when and why (``pay_0011``).

Messages (``pay_0010``, same rules, ``pairly_pay.messages``): ``pay_messages``, a drafted
message about an invoice exception to the vendor or the requester, sent only after a
person approved its exact text. Recipient and text are encrypted.

Vendor portal and credit notes (``pay_0012``, ``pairly_pay.portal``): ``pay_invoices``
gains the document type (invoice or credit note), the invoice a credit note reduces and
the credit applied to an invoice; ``pay_messages`` gains the channel (email or portal),
the direction (from AP or from the vendor) and the vendor, so the portal thread of an
invoice lives with the other messages; ``pay_payment_run_items`` keeps the run's date.
The portal tables are in ``pairly_pay.portal.models``. A restrictive Row Level Security
layer keyed on the portal subject limits a vendor portal session to its own vendor.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.crypto.blind_index import blind_index_info
from pairly_core.crypto.types import EncryptedJSON, EncryptedString, Sealed
from pairly_core.db import Base, OrgScoped

VENDORS: Final = "pay_vendors"
ACCOUNTS: Final = "pay_vendor_bank_accounts"
TICKETS: Final = "pay_bank_change_tickets"
CALLS: Final = "pay_bank_change_calls"
INVITES: Final = "pay_vendor_invites"
SESSIONS: Final = "pay_vendor_onboarding_sessions"
SETTINGS: Final = "pay_settings"

DEFAULT_COOLING_OFF_HOURS: Final = 24
MAX_COOLING_OFF_HOURS: Final = 720
DEFAULT_PHONE_WINDOW_DAYS: Final = 30


class VendorStatus(StrEnum):
    # Submitted through onboarding, waiting for an AP review.
    PENDING = "pending"
    ACTIVE = "active"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class VendorSource(StrEnum):
    STAFF = "staff"
    ONBOARDING = "onboarding"


class HoldReason(StrEnum):
    # An open change ticket holds payments; it lifts when the last ticket closes.
    BANK_CHANGE = "bank_change"
    # Sticky reasons: only a person with pay.hold.manage lifts them.
    VERIFICATION_FAILED = "verification_failed"
    CANCELLED_KEEP_HOLD = "cancelled_keep_hold"
    APPROVAL_NOT_POSSIBLE = "approval_not_possible"
    MANUAL = "manual"


class AccountStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    RETIRED = "retired"
    REJECTED = "rejected"


class TicketStatus(StrEnum):
    AWAITING_VERIFICATION = "awaiting_verification"
    AWAITING_APPROVAL = "awaiting_approval"
    COOLING_OFF = "cooling_off"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


OPEN_TICKET_STATUSES: Final = frozenset(
    {
        TicketStatus.AWAITING_VERIFICATION,
        TicketStatus.AWAITING_APPROVAL,
        TicketStatus.COOLING_OFF,
    }
)


class TicketSource(StrEnum):
    STAFF = "staff"
    EMAIL = "email"
    INVOICE = "invoice"
    VENDOR_PORTAL = "vendor_portal"
    ONBOARDING = "onboarding"


class CallResult(StrEnum):
    CONFIRMED = "confirmed"
    NOT_CONFIRMED = "not_confirmed"
    NO_ANSWER = "no_answer"


class TrnStatus(StrEnum):
    NONE = "none"
    NOT_VERIFIED = "not_verified"
    VERIFIED = "verified"
    INVALID = "invalid"


def _in(values: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


OPEN_SQL: Final = ", ".join(f"'{s.value}'" for s in sorted(OPEN_TICKET_STATUSES))


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _ts() -> Mapped[datetime | None]:
    return mapped_column(DateTime(timezone=True), nullable=True)


def _jsonb_list() -> Mapped[list[Any]]:
    return mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))


def _user_fk(nullable: bool = True) -> Mapped[Any]:
    return mapped_column(Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=nullable)


class PayVendor(Base, OrgScoped):
    __tablename__ = VENDORS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_vendors_org_id_id"),
        UniqueConstraint("org_id", "reference", name="uq_pay_vendors_org_id_reference"),
        CheckConstraint(f"status IN ({_in(VendorStatus)})", name="status_valid"),
        CheckConstraint(f"source IN ({_in(VendorSource)})", name="source_valid"),
        CheckConstraint(
            f"hold_reason IS NULL OR hold_reason IN ({_in(HoldReason)})", name="hold_reason_valid"
        ),
        CheckConstraint(f"trn_status IN ({_in(TrnStatus)})", name="trn_status_valid"),
        CheckConstraint("country ~ '^[A-Z]{2}$'", name="country_format"),
        CheckConstraint("trn IS NULL OR trn ~ '^[0-9]{15}$'", name="trn_format"),
        CheckConstraint(
            "phone_bidx IS NULL OR phone_bidx ~ '^[0-9a-f]{64}$'", name="phone_bidx_format"
        ),
        CheckConstraint("payment_hold OR hold_reason IS NULL", name="hold_reason_needs_hold"),
        Index("ix_pay_vendors_status", "org_id", "status", "created_at"),
        Index("ix_pay_vendors_trn", "org_id", "trn"),
        Index("ix_pay_vendors_phone_bidx", "org_id", "phone_bidx"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    reference: Mapped[str] = mapped_column(String(16), nullable=False)
    legal_name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name_ar: Mapped[str | None] = mapped_column(String(200), nullable=True)
    trade_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # ``pairly_pay.dedupe.name_key`` of the legal and trade names (duplicate hints).
    name_key: Mapped[str] = mapped_column(String(300), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    trade_licence: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trn: Mapped[str | None] = mapped_column(String(15), nullable=True)
    trn_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TrnStatus.NONE.value, server_default="none"
    )
    # Business address (a company's, not a person's): line1, line2, city, region,
    # country, po_box.
    address: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # People at the vendor: [{name, role, email, phone}]. Personal data, encrypted.
    contacts: Mapped[Sealed | None] = mapped_column(EncryptedJSON(), nullable=True)
    # The number AP calls back on (brief 4.1.4). Changing it is recorded and blocked
    # while a change ticket is open.
    phone_on_file: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    phone_bidx: Mapped[str | None] = mapped_column(
        String(64), nullable=True, info=blind_index_info("phone", "phone_on_file")
    )
    phone_changed_at: Mapped[datetime | None] = _ts()
    # Domains invoices and emails from this vendor normally come from.
    email_domains: Mapped[list[str]] = _jsonb_list()
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The vendor's agreed payment terms as AP records them, for example "2/10 net 30"
    # (``pay_0015``): the early-payment discount when an invoice prints none.
    payment_terms: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=VendorStatus.ACTIVE.value
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=VendorSource.STAFF.value
    )
    payment_hold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    hold_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hold_set_at: Mapped[datetime | None] = _ts()
    # Duplicate hints and other reasons a reviewer must look (onboarding).
    review_reasons: Mapped[list[str]] = _jsonb_list()
    # Documents the vendor sent with the onboarding form (``documents`` ids).
    document_ids: Mapped[list[str]] = _jsonb_list()
    created_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()
    activated_at: Mapped[datetime | None] = _ts()


class PayVendorBankAccount(Base, OrgScoped):
    __tablename__ = ACCOUNTS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_vendor_bank_accounts_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "vendor_id"],
            ["pay_vendors.org_id", "pay_vendors.id"],
            ondelete="RESTRICT",
            name="fk_pay_vendor_bank_accounts_vendor_org",
        ),
        CheckConstraint(f"status IN ({_in(AccountStatus)})", name="status_valid"),
        CheckConstraint("iban_bidx ~ '^[0-9a-f]{64}$'", name="iban_bidx_format"),
        CheckConstraint("country ~ '^[A-Z]{2}$'", name="country_format"),
        Index("ix_pay_vendor_bank_accounts_vendor", "org_id", "vendor_id", "created_at"),
        Index("ix_pay_vendor_bank_accounts_iban_bidx", "org_id", "iban_bidx"),
        Index(
            "uq_pay_vendor_bank_accounts_one_active",
            "org_id",
            "vendor_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    iban: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    iban_bidx: Mapped[str] = mapped_column(
        String(64), nullable=False, info=blind_index_info("iban", "iban")
    )
    iban_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    bank_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The name on the account (brief 4.1.3: name-on-account capture).
    account_name: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    bank_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    swift: Mapped[str | None] = mapped_column(String(11), nullable=True)
    created_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _created()
    activated_at: Mapped[datetime | None] = _ts()
    retired_at: Mapped[datetime | None] = _ts()


class PayBankChangeTicket(Base, OrgScoped):
    __tablename__ = TICKETS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_bank_change_tickets_org_id_id"),
        UniqueConstraint("org_id", "reference", name="uq_pay_bank_change_tickets_org_id_reference"),
        ForeignKeyConstraint(
            ["org_id", "vendor_id"],
            ["pay_vendors.org_id", "pay_vendors.id"],
            ondelete="RESTRICT",
            name="fk_pay_bank_change_tickets_vendor_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "previous_account_id"],
            ["pay_vendor_bank_accounts.org_id", "pay_vendor_bank_accounts.id"],
            ondelete="RESTRICT",
            name="fk_pay_bank_change_tickets_previous_account_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "proposed_account_id"],
            ["pay_vendor_bank_accounts.org_id", "pay_vendor_bank_accounts.id"],
            ondelete="RESTRICT",
            name="fk_pay_bank_change_tickets_proposed_account_org",
        ),
        CheckConstraint(f"status IN ({_in(TicketStatus)})", name="status_valid"),
        CheckConstraint(f"source IN ({_in(TicketSource)})", name="source_valid"),
        CheckConstraint(
            f"cooling_off_hours >= 0 AND cooling_off_hours <= {MAX_COOLING_OFF_HOURS}",
            name="cooling_off_range",
        ),
        CheckConstraint("details_digest ~ '^[0-9a-f]{64}$'", name="details_digest_format"),
        Index("ix_pay_bank_change_tickets_status", "org_id", "status", "created_at"),
        Index("ix_pay_bank_change_tickets_vendor", "org_id", "vendor_id", "created_at"),
        Index(
            "uq_pay_bank_change_tickets_one_open",
            "org_id",
            "vendor_id",
            unique=True,
            postgresql_where=text(f"status IN ({OPEN_SQL})"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    reference: Mapped[str] = mapped_column(String(16), nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # The person who asked, when it was a person (segregation of duties).
    requester_user_id: Mapped[uuid.UUID | None] = _user_fk()
    previous_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    proposed_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # Keyed digest of the proposed details (``pairly_pay.bank_changes.details_digest``):
    # the approval payload carries it, and the final action and the activation check it.
    details_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # Staff note on where the request came from. Personal data possible: encrypted.
    note: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    # Anything the request itself carried (a phone number or email in the message).
    # Kept for investigation only: never shown, never called (brief 4.1.4).
    request_claims: Mapped[Sealed | None] = mapped_column(EncryptedJSON(), nullable=True)
    # The vendor's phone number on file when the ticket opened: the only number anyone
    # may call to verify this change.
    verify_phone: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    verify_phone_bidx: Mapped[str | None] = mapped_column(
        String(64), nullable=True, info=blind_index_info("phone", "verify_phone")
    )
    # The on-file number changed shortly before the ticket opened (a fraud pattern).
    phone_recently_changed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    cooling_off_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    verified_by: Mapped[uuid.UUID | None] = _user_fk()
    verified_at: Mapped[datetime | None] = _ts()
    verification_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=True
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=True
    )
    approver_ids: Mapped[list[str]] = _jsonb_list()
    approved_at: Mapped[datetime | None] = _ts()
    activates_at: Mapped[datetime | None] = _ts()
    completed_at: Mapped[datetime | None] = _ts()
    closed_at: Mapped[datetime | None] = _ts()
    closed_by_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    closed_by_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Why it ended when it did not complete (``rejected_by_approver``, ...).
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    close_reason: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    hold_kept: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    decision_ids: Mapped[list[str]] = _jsonb_list()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class PayBankChangeCall(Base, OrgScoped):
    __tablename__ = CALLS
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "ticket_id"],
            ["pay_bank_change_tickets.org_id", "pay_bank_change_tickets.id"],
            ondelete="RESTRICT",
            name="fk_pay_bank_change_calls_ticket_org",
        ),
        CheckConstraint(f"result IN ({_in(CallResult)})", name="result_valid"),
        Index("ix_pay_bank_change_calls_ticket", "org_id", "ticket_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    ticket_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    called_by: Mapped[uuid.UUID] = _user_fk(nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    notes: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    # The number called: always the ticket's on-file snapshot (last four for display).
    number_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    created_at: Mapped[datetime] = _created()


class PayVendorInvite(Base, OrgScoped):
    __tablename__ = INVITES
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_vendor_invites_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "vendor_id"],
            ["pay_vendors.org_id", "pay_vendors.id"],
            ondelete="RESTRICT",
            name="fk_pay_vendor_invites_vendor_org",
        ),
        CheckConstraint("locale IN ('en', 'ar')", name="locale_valid"),
        CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_format"),
        Index("ix_pay_vendor_invites_created", "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_hint: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Who the link was given to (for the AP team's list). Personal data: encrypted.
    email: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    locale: Mapped[str] = mapped_column(String(2), nullable=False, default="en")
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = _ts()
    revoked_at: Mapped[datetime | None] = _ts()
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_by: Mapped[uuid.UUID] = _user_fk(nullable=False)
    created_at: Mapped[datetime] = _created()


class PayVendorOnboardingSession(Base, OrgScoped):
    __tablename__ = SESSIONS
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "invite_id"],
            ["pay_vendor_invites.org_id", "pay_vendor_invites.id"],
            ondelete="RESTRICT",
            name="fk_pay_vendor_onboarding_sessions_invite_org",
        ),
        CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="secret_hash_format"),
        Index("ix_pay_vendor_onboarding_sessions_invite", "org_id", "invite_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    invite_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    document_ids: Mapped[list[str]] = _jsonb_list()
    submitted_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _created()


class PaySettings(Base, OrgScoped):
    __tablename__ = SETTINGS
    __table_args__ = (
        UniqueConstraint("org_id", name="uq_pay_settings_org_id"),
        CheckConstraint(
            f"cooling_off_hours >= 0 AND cooling_off_hours <= {MAX_COOLING_OFF_HOURS}",
            name="cooling_off_range",
        ),
        CheckConstraint(
            "phone_change_window_days >= 0 AND phone_change_window_days <= 365",
            name="phone_window_range",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    cooling_off_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_COOLING_OFF_HOURS
    )
    phone_change_window_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_PHONE_WINDOW_DAYS
    )
    # Invoice check settings (``pairly_pay.checks.settings``; empty: the defaults).
    check_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Approval and payment settings (``pairly_pay.payment_settings``, ``pay_0005``).
    payment_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    updated_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    updated_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = _created()


# ------------------------------------------------------------------ invoices (pay_0002)

INVOICES: Final = "pay_invoices"
INTAKE_EMAILS: Final = "pay_intake_emails"
CORRECTIONS: Final = "pay_invoice_corrections"


class InvoiceStatus(StrEnum):
    # Intake has the document; the pay_invoice run has not started yet.
    RECEIVED = "received"
    # The run is reading the invoice (scan, parse, guardrails, extraction, vendor).
    EXTRACTING = "extracting"
    # A person must look: low confidence, guardrail findings, no vendor, a failed step.
    NEEDS_REVIEW = "needs_review"
    # Read and matched; the checks and approvals of later steps take it from here.
    READY = "ready"
    # Submitted by a person; the ``pay_invoice_approval`` run waits for the approvers
    # (WP4.7, ``pay_0005``). Nothing about the invoice can change meanwhile.
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PAID = "paid"
    # Closed by a person (not an invoice, a duplicate, a fake). Never by an agent.
    REJECTED = "rejected"
    # A credit note a person applied against its invoice (``pay_0012``): it reduced
    # what is paid on that invoice. Never paid itself.
    APPLIED = "applied"


class InvoiceSource(StrEnum):
    EMAIL = "email"
    UPLOAD = "upload"
    API = "api"
    # Submitted by a vendor's contact in the vendor portal (``pay_0012``).
    VENDOR_PORTAL = "vendor_portal"


class DocumentType(StrEnum):
    INVOICE = "invoice"
    # Reduces what is owed on one invoice of the same vendor once a person applies it.
    CREDIT_NOTE = "credit_note"


class ExtractionSource(StrEnum):
    MODEL = "model"
    EINVOICE = "einvoice"


class EmailClass(StrEnum):
    INVOICE = "invoice"
    MARKETING = "marketing"
    # The rules could not tell: a person looks (the email's body becomes an invoice in
    # needs_review with the reason).
    UNSURE = "unsure"


class PayInvoice(Base, OrgScoped):
    __tablename__ = INVOICES
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_invoices_org_id_id"),
        UniqueConstraint("org_id", "reference", name="uq_pay_invoices_org_id_reference"),
        # File hash dedupe: a document (one file in this workflow) is one invoice.
        UniqueConstraint("org_id", "document_id", name="uq_pay_invoices_org_id_document_id"),
        ForeignKeyConstraint(
            ["org_id", "vendor_id"],
            ["pay_vendors.org_id", "pay_vendors.id"],
            ondelete="RESTRICT",
            name="fk_pay_invoices_vendor_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "intake_email_id"],
            ["pay_intake_emails.org_id", "pay_intake_emails.id"],
            ondelete="RESTRICT",
            name="fk_pay_invoices_intake_email_org",
        ),
        CheckConstraint(f"status IN ({_in(InvoiceStatus)})", name="status_valid"),
        CheckConstraint(f"source IN ({_in(InvoiceSource)})", name="source_valid"),
        CheckConstraint(
            f"extraction_source IS NULL OR extraction_source IN ({_in(ExtractionSource)})",
            name="extraction_source_valid",
        ),
        CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint(
            "checks_digest IS NULL OR checks_digest ~ '^[0-9a-f]{64}$'",
            name="checks_digest_format",
        ),
        ForeignKeyConstraint(
            ["org_id", "approved_account_id"],
            ["pay_vendor_bank_accounts.org_id", "pay_vendor_bank_accounts.id"],
            ondelete="RESTRICT",
            name="fk_pay_invoices_approved_account_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "payment_run_id"],
            ["pay_payment_runs.org_id", "pay_payment_runs.id"],
            ondelete="RESTRICT",
            name="fk_pay_invoices_payment_run_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "po_id"],
            ["pay_purchase_orders.org_id", "pay_purchase_orders.id"],
            ondelete="RESTRICT",
            name="fk_pay_invoices_po_org",
        ),
        CheckConstraint(
            "status NOT IN ('scheduled', 'paid') OR payment_run_id IS NOT NULL",
            name="scheduled_has_run",
        ),
        CheckConstraint(
            "status <> 'paid' OR (paid_at IS NOT NULL AND paid_by IS NOT NULL)",
            name="paid_has_evidence",
        ),
        CheckConstraint(
            "status NOT IN ('approved', 'scheduled', 'paid') OR (approved_at IS NOT NULL "
            "AND approved_account_id IS NOT NULL AND approved_bank_digest IS NOT NULL "
            "AND approved_checks_digest IS NOT NULL AND approved_amount IS NOT NULL)",
            name="approved_has_evidence",
        ),
        Index("ix_pay_invoices_status", "org_id", "status", "created_at"),
        Index("ix_pay_invoices_vendor", "org_id", "vendor_id", "created_at"),
        Index("ix_pay_invoices_number_key", "org_id", "vendor_id", "number_key"),
        Index("ix_pay_invoices_po", "org_id", "po_id", "created_at"),
        ForeignKeyConstraint(
            ["org_id", "credit_for_invoice_id"],
            ["pay_invoices.org_id", "pay_invoices.id"],
            ondelete="RESTRICT",
            name="fk_pay_invoices_credit_for_org",
        ),
        CheckConstraint(f"document_type IN ({_in(DocumentType)})", name="document_type_valid"),
        CheckConstraint(
            "document_type = 'credit_note' OR credit_for_invoice_id IS NULL",
            name="credit_link_only_on_credit_notes",
        ),
        CheckConstraint(
            "status <> 'applied' OR (document_type = 'credit_note' "
            "AND credit_for_invoice_id IS NOT NULL AND credit_applied_at IS NOT NULL)",
            name="applied_is_credit_note",
        ),
        CheckConstraint(
            "document_type = 'invoice' OR status NOT IN "
            "('awaiting_approval', 'approved', 'scheduled', 'paid')",
            name="credit_note_never_paid",
        ),
        CheckConstraint("credit_total >= 0", name="credit_total_not_negative"),
        Index("ix_pay_invoices_credit_for", "org_id", "credit_for_invoice_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    reference: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    intake_email_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # The receipt reference the sender got (``REF-...``), for email intake.
    intake_reference: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Domains the invoice came from: the sender's, and the forwarded original sender's.
    sender_domains: Mapped[list[str]] = _jsonb_list()
    submitted_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    submitted_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    submitted_by: Mapped[uuid.UUID | None] = _user_fk()
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=True
    )
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("extractions.id", ondelete="RESTRICT"), nullable=True
    )
    extraction_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    guardrail_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    sanitized_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    removed_spans: Mapped[list[Any]] = _jsonb_list()
    # Vendor link and how it was found: {method, confidence, candidates, conflict}.
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    vendor_match: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Current header values (corrections included), for lists and later checks.
    vendor_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    vendor_trn: Mapped[str | None] = mapped_column(String(15), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # The payment terms printed on the invoice, as read (``pay_0015``).
    payment_terms: Mapped[str | None] = mapped_column(String(120), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    po_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    vat_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    total: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    review_reasons: Mapped[list[str]] = _jsonb_list()
    decision_ids: Mapped[list[str]] = _jsonb_list()
    # Validation and fraud checks (WP4.6, ``pay_0003``, ``pairly_pay.checks``): what
    # later invoices are compared with (number without separators, line items digest,
    # template fingerprint), every check's result with its findings, and the risk score.
    number_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lines_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fingerprint: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    checks: Mapped[list[Any]] = _jsonb_list()
    risk: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    checked_at: Mapped[datetime | None] = _ts()
    # SHA-256 of the inputs the checks ran on (``pairly_pay.freshness``, ``pay_0004``):
    # when the invoice now hashes differently, the stored results are stale.
    checks_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Coding a person sets (``pay_0004``): what the approval rules route on. The
    # category defaults to the vendor's when empty.
    department: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = _user_fk()
    reviewed_at: Mapped[datetime | None] = _ts()
    rejected_by: Mapped[uuid.UUID | None] = _user_fk()
    rejected_at: Mapped[datetime | None] = _ts()
    reject_reason: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    # Approval (WP4.7, ``pay_0005``, ``pairly_pay.approvals``). Who sent it for
    # approval, the run and the engine approval, and what was approved: the people (or
    # the rule), the amount, the vendor's bank account and the keyed digest of its
    # details, and the check digest. Payment runs and the bank file compare with these.
    approval_submitted_by: Mapped[uuid.UUID | None] = _user_fk()
    approval_submitted_at: Mapped[datetime | None] = _ts()
    approval_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=True
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=True
    )
    approved_at: Mapped[datetime | None] = _ts()
    approved_by_rule: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    approver_ids: Mapped[list[str]] = _jsonb_list()
    approved_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    approved_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    approved_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    approved_bank_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_checks_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Payment (``pay_0006``, ``pairly_pay.payment_runs``): the run that pays it, and when
    # a person marked it paid (after the bank confirmed, outside Pairly).
    payment_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    paid_at: Mapped[datetime | None] = _ts()
    paid_by: Mapped[uuid.UUID | None] = _user_fk()
    payment_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Matching (``pay_0007``, ``pairly_pay.checks.matching``): the PO the invoice cites
    # when it is on file, and the allocation of the invoice lines to its lines
    # ({po_id, status, lines: [{invoice_line, po_line_no, quantity}]}).
    po_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    po_match: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()
    # Vendor portal and credit notes (``pay_0012``): what kind of document it is, the
    # invoice a credit note reduces (the vendor's choice, confirmed when a person applies
    # it), the credit notes applied to this invoice so far (what is paid is
    # ``approved_amount - credit_total``), and the portal contact who sent it.
    document_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DocumentType.INVOICE.value, server_default="invoice"
    )
    credit_for_invoice_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    credit_total: Mapped[Decimal] = mapped_column(
        Numeric(20, 4), nullable=False, default=Decimal(0), server_default="0"
    )
    credit_applied_at: Mapped[datetime | None] = _ts()
    credit_applied_by: Mapped[uuid.UUID | None] = _user_fk()
    portal_contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class PayIntakeEmail(Base, OrgScoped):
    """How Pay classified one inbound email, and the receipt it sent."""

    __tablename__ = INTAKE_EMAILS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_intake_emails_org_id_id"),
        UniqueConstraint(
            "org_id", "inbound_email_id", name="uq_pay_intake_emails_org_id_inbound_email_id"
        ),
        CheckConstraint(f"classification IN ({_in(EmailClass)})", name="classification_valid"),
        Index("ix_pay_intake_emails_created", "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    inbound_email_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("inbound_emails.id", ondelete="RESTRICT"), nullable=False
    )
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    reasons: Mapped[list[str]] = _jsonb_list()
    reference: Mapped[str] = mapped_column(String(32), nullable=False)
    sender_domain: Mapped[str | None] = mapped_column(String(253), nullable=True)
    forwarded_domain: Mapped[str | None] = mapped_column(String(253), nullable=True)
    invoice_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Attachments kept with the email but not read as invoices (terms, logos).
    supporting_document_ids: Mapped[list[str]] = _jsonb_list()
    receipt_sent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    receipt_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _created()


class PayInvoiceCorrection(Base, OrgScoped):
    """A person's correction of one extracted field, kept per vendor (brief 4.3.4).

    The engine's ``extraction_corrections`` row is the record of the correction; this
    row files it under the vendor so the next invoice from that vendor can learn from it
    (``pairly_pay.learning``). Insert only. Bank-detail corrections keep no values here.
    """

    __tablename__ = CORRECTIONS
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "invoice_id"],
            ["pay_invoices.org_id", "pay_invoices.id"],
            ondelete="RESTRICT",
            name="fk_pay_invoice_corrections_invoice_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "vendor_id"],
            ["pay_vendors.org_id", "pay_vendors.id"],
            ondelete="RESTRICT",
            name="fk_pay_invoice_corrections_vendor_org",
        ),
        Index("ix_pay_invoice_corrections_vendor", "org_id", "vendor_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("extractions.id", ondelete="RESTRICT"), nullable=False
    )
    correction_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("extraction_corrections.id", ondelete="RESTRICT"), nullable=False
    )
    field_path: Mapped[str] = mapped_column(String(200), nullable=False)
    field_pattern: Mapped[str] = mapped_column(String(200), nullable=False)
    old_value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    corrected_by: Mapped[uuid.UUID] = _user_fk(nullable=False)
    created_at: Mapped[datetime] = _created()


# ------------------------------------------------------------------ payment runs (pay_0006)

PAYMENT_RUNS: Final = "pay_payment_runs"
PAYMENT_RUN_ITEMS: Final = "pay_payment_run_items"


class PaymentRunStatus(StrEnum):
    # Proposed by a person; the ``pay_payment_run`` run waits for a finance controller.
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    # A bank file was downloaded at least once. Nothing was sent to a bank.
    EXPORTED = "exported"
    PAID = "paid"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    # The final action refused (something changed since the proposal): nothing is paid.
    FAILED = "failed"


OPEN_RUN_STATUSES: Final = frozenset(
    {PaymentRunStatus.AWAITING_APPROVAL, PaymentRunStatus.APPROVED, PaymentRunStatus.EXPORTED}
)


class RunItemStatus(StrEnum):
    INCLUDED = "included"
    PAID = "paid"
    # Back out of the run (rejected, cancelled, failed): the invoice is approved again.
    RELEASED = "released"


class PayPaymentRun(Base, OrgScoped):
    """A proposal to pay approved invoices, its approval, its bank files and its end."""

    __tablename__ = PAYMENT_RUNS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_payment_runs_org_id_id"),
        UniqueConstraint("org_id", "reference", name="uq_pay_payment_runs_org_id_reference"),
        CheckConstraint(f"status IN ({_in(PaymentRunStatus)})", name="status_valid"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint("cash_limit > 0", name="cash_limit_positive"),
        CheckConstraint("total >= 0 AND total <= cash_limit", name="total_within_limit"),
        CheckConstraint(
            "status NOT IN ('approved', 'exported', 'paid') OR approved_at IS NOT NULL",
            name="approved_has_time",
        ),
        Index("ix_pay_payment_runs_status", "org_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    reference: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    cash_limit: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_on_or_before: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Invoices the proposal left out, and why: [{invoice_id, reference, reason}].
    excluded: Mapped[list[Any]] = _jsonb_list()
    proposed_by: Mapped[uuid.UUID] = _user_fk(nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=True
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=True
    )
    approver_ids: Mapped[list[str]] = _jsonb_list()
    approved_at: Mapped[datetime | None] = _ts()
    export_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Every download: [{format, sha256, items, at, by}]. The files themselves are not
    # kept (they hold bank details); the digest proves which one was given out.
    exports: Mapped[list[Any]] = _jsonb_list()
    exported_at: Mapped[datetime | None] = _ts()
    paid_at: Mapped[datetime | None] = _ts()
    closed_at: Mapped[datetime | None] = _ts()
    close_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decision_ids: Mapped[list[str]] = _jsonb_list()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class PayPaymentRunItem(Base, OrgScoped):
    """One invoice of a payment run, with the bank account and digests it was proposed,
    approved and exported with."""

    __tablename__ = PAYMENT_RUN_ITEMS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_payment_run_items_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "payment_run_id"],
            ["pay_payment_runs.org_id", "pay_payment_runs.id"],
            ondelete="RESTRICT",
            name="fk_pay_payment_run_items_run_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "invoice_id"],
            ["pay_invoices.org_id", "pay_invoices.id"],
            ondelete="RESTRICT",
            name="fk_pay_payment_run_items_invoice_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "vendor_id"],
            ["pay_vendors.org_id", "pay_vendors.id"],
            ondelete="RESTRICT",
            name="fk_pay_payment_run_items_vendor_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "bank_account_id"],
            ["pay_vendor_bank_accounts.org_id", "pay_vendor_bank_accounts.id"],
            ondelete="RESTRICT",
            name="fk_pay_payment_run_items_account_org",
        ),
        CheckConstraint(f"status IN ({_in(RunItemStatus)})", name="status_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("discount IS NULL OR discount >= 0", name="discount_not_negative"),
        CheckConstraint(
            "status <> 'paid' OR (paid_at IS NOT NULL AND paid_by IS NOT NULL)",
            name="paid_has_evidence",
        ),
        Index("ix_pay_payment_run_items_run", "org_id", "payment_run_id", "position"),
        # An invoice is in at most one run at a time.
        Index(
            "uq_pay_payment_run_items_one_active",
            "org_id",
            "invoice_id",
            unique=True,
            postgresql_where=text("status <> 'released'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    payment_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # The run's execution date, kept per item (``pay_0012``) so a vendor's portal shows
    # when it will be paid without reading the run (other vendors' payments).
    execution_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # The early-payment discount taken (``pairly_pay.discounts``): ``amount`` is what is
    # due less this. Empty when the run does not capture discounts or none applies.
    discount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    bank_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    bank_details_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    checks_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    end_to_end_id: Mapped[str] = mapped_column(String(35), nullable=False)
    paid_at: Mapped[datetime | None] = _ts()
    paid_by: Mapped[uuid.UUID | None] = _user_fk()
    payment_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


# ------------------------------------------------------------------ purchase orders (pay_0007)

PURCHASE_ORDERS: Final = "pay_purchase_orders"
PURCHASE_ORDER_LINES: Final = "pay_purchase_order_lines"
GOODS_RECEIPTS: Final = "pay_goods_receipts"
GOODS_RECEIPT_LINES: Final = "pay_goods_receipt_lines"


class PoStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class MatchType(StrEnum):
    # Invoice against the PO only (services).
    TWO_WAY = "2-way"
    # Invoice against the PO and the goods received (goods).
    THREE_WAY = "3-way"


class RecordSource(StrEnum):
    API = "api"
    CSV = "csv"


class PayPurchaseOrder(Base, OrgScoped):
    """A purchase order (brief 4.5.1): what was ordered from a vendor, at what price."""

    __tablename__ = PURCHASE_ORDERS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_purchase_orders_org_id_id"),
        # One PO per number (compared without case, spaces or separators).
        UniqueConstraint("org_id", "number_key", name="uq_pay_purchase_orders_org_id_number_key"),
        ForeignKeyConstraint(
            ["org_id", "vendor_id"],
            ["pay_vendors.org_id", "pay_vendors.id"],
            ondelete="RESTRICT",
            name="fk_pay_purchase_orders_vendor_org",
        ),
        CheckConstraint(f"status IN ({_in(PoStatus)})", name="status_valid"),
        CheckConstraint(f"match_type IN ({_in(MatchType)})", name="match_type_valid"),
        CheckConstraint(f"source IN ({_in(RecordSource)})", name="source_valid"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint("subtotal >= 0", name="subtotal_not_negative"),
        CheckConstraint("line_count > 0", name="has_lines"),
        CheckConstraint(
            "status = 'open' OR (closed_at IS NOT NULL AND closed_by_id IS NOT NULL)",
            name="closed_has_evidence",
        ),
        Index("ix_pay_purchase_orders_vendor", "org_id", "vendor_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    po_number: Mapped[str] = mapped_column(String(64), nullable=False)
    number_key: Mapped[str] = mapped_column(String(64), nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    match_type: Mapped[str] = mapped_column(String(8), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    department: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requester: Mapped[str | None] = mapped_column(String(120), nullable=True)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    created_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()
    # Closed or cancelled by a person (``pay_0011``): when, by whom and why. Invoices
    # received before a PO was closed keep their match; later ones are not matched.
    closed_at: Mapped[datetime | None] = _ts()
    closed_by_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    closed_by_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class PayPurchaseOrderLine(Base, OrgScoped):
    """One line of a purchase order (in the PO's currency)."""

    __tablename__ = PURCHASE_ORDER_LINES
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_purchase_order_lines_org_id_id"),
        UniqueConstraint("org_id", "po_id", "line_no", name="uq_pay_purchase_order_lines_line"),
        ForeignKeyConstraint(
            ["org_id", "po_id"],
            ["pay_purchase_orders.org_id", "pay_purchase_orders.id"],
            ondelete="RESTRICT",
            name="fk_pay_purchase_order_lines_po_org",
        ),
        CheckConstraint("line_no > 0", name="line_no_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_not_negative"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    po_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    created_at: Mapped[datetime] = _created()


class PayGoodsReceipt(Base, OrgScoped):
    """A goods receipt (GRN): what arrived against a purchase order."""

    __tablename__ = GOODS_RECEIPTS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_goods_receipts_org_id_id"),
        UniqueConstraint("org_id", "number_key", name="uq_pay_goods_receipts_org_id_number_key"),
        ForeignKeyConstraint(
            ["org_id", "po_id"],
            ["pay_purchase_orders.org_id", "pay_purchase_orders.id"],
            ondelete="RESTRICT",
            name="fk_pay_goods_receipts_po_org",
        ),
        CheckConstraint(f"source IN ({_in(RecordSource)})", name="source_valid"),
        CheckConstraint("line_count > 0", name="has_lines"),
        Index("ix_pay_goods_receipts_po", "org_id", "po_id", "received_date"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    grn_number: Mapped[str] = mapped_column(String(64), nullable=False)
    number_key: Mapped[str] = mapped_column(String(64), nullable=False)
    po_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    received_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    created_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _created()


class PayGoodsReceiptLine(Base, OrgScoped):
    """The quantity received of one PO line."""

    __tablename__ = GOODS_RECEIPT_LINES
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_goods_receipt_lines_org_id_id"),
        UniqueConstraint(
            "org_id", "receipt_id", "po_line_no", name="uq_pay_goods_receipt_lines_line"
        ),
        ForeignKeyConstraint(
            ["org_id", "receipt_id"],
            ["pay_goods_receipts.org_id", "pay_goods_receipts.id"],
            ondelete="RESTRICT",
            name="fk_pay_goods_receipt_lines_receipt_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "po_id"],
            ["pay_purchase_orders.org_id", "pay_purchase_orders.id"],
            ondelete="RESTRICT",
            name="fk_pay_goods_receipt_lines_po_org",
        ),
        CheckConstraint("po_line_no > 0", name="po_line_no_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index("ix_pay_goods_receipt_lines_po", "org_id", "po_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    receipt_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    po_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    po_line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    created_at: Mapped[datetime] = _created()


# ------------------------------------------------------------------ GL coding (pay_0009)

CODING_LISTS: Final = "pay_coding_lists"
CODING_ENTRIES: Final = "pay_coding_entries"
LINE_CODINGS: Final = "pay_invoice_line_codings"
CODING_CORRECTIONS: Final = "pay_coding_corrections"


class CodingKind(StrEnum):
    GL_ACCOUNT = "gl_account"
    COST_CENTRE = "cost_centre"


class CodingSource(StrEnum):
    # A person chose it (a suggestion they confirmed, or their own choice).
    PERSON = "person"
    # Applied by the system: a high confidence suggestion, only when the organisation
    # turned auto-coding on (``auto_code_high_confidence``, off by default).
    AUTO = "auto"


class PayCodingList(Base, OrgScoped):
    """One version of the chart of accounts or of the cost centres (never edited: a
    change is a new version, the newest is in force)."""

    __tablename__ = CODING_LISTS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_coding_lists_org_id_id"),
        UniqueConstraint("org_id", "kind", "version", name="uq_pay_coding_lists_version"),
        CheckConstraint(f"kind IN ({_in(CodingKind)})", name="kind_valid"),
        CheckConstraint(f"source IN ({_in(RecordSource)})", name="source_valid"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("entry_count >= 0", name="entry_count_not_negative"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    created_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _created()


class PayCodingEntry(Base, OrgScoped):
    """One GL account or cost centre of a list version."""

    __tablename__ = CODING_ENTRIES
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_coding_entries_org_id_id"),
        UniqueConstraint("org_id", "list_id", "code", name="uq_pay_coding_entries_code"),
        ForeignKeyConstraint(
            ["org_id", "list_id"],
            ["pay_coding_lists.org_id", "pay_coding_lists.id"],
            ondelete="RESTRICT",
            name="fk_pay_coding_entries_list_org",
        ),
        CheckConstraint(f"kind IN ({_in(CodingKind)})", name="kind_valid"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    list_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    name_ar: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = _created()


class PayInvoiceLineCoding(Base, OrgScoped):
    """The GL account and cost centre of one invoice line, with the list versions they
    came from and what was suggested at the time. Later suggestions learn from these
    rows (``pairly_pay.coding``)."""

    __tablename__ = LINE_CODINGS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_invoice_line_codings_org_id_id"),
        UniqueConstraint(
            "org_id", "invoice_id", "line_index", name="uq_pay_invoice_line_codings_line"
        ),
        ForeignKeyConstraint(
            ["org_id", "invoice_id"],
            ["pay_invoices.org_id", "pay_invoices.id"],
            ondelete="RESTRICT",
            name="fk_pay_invoice_line_codings_invoice_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "gl_list_id"],
            ["pay_coding_lists.org_id", "pay_coding_lists.id"],
            ondelete="RESTRICT",
            name="fk_pay_invoice_line_codings_gl_list_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "cc_list_id"],
            ["pay_coding_lists.org_id", "pay_coding_lists.id"],
            ondelete="RESTRICT",
            name="fk_pay_invoice_line_codings_cc_list_org",
        ),
        CheckConstraint(f"source IN ({_in(CodingSource)})", name="source_valid"),
        CheckConstraint("line_index >= 0 AND line_index < 1000", name="line_index_range"),
        Index("ix_pay_invoice_line_codings_vendor", "org_id", "vendor_id", "coded_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    line_index: Mapped[int] = mapped_column(Integer, nullable=False)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # The line as it was coded (what later lines are compared with).
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    gl_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cost_centre: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gl_list_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    cc_list_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    # What the suggestion said when it was coded ({gl_code, cost_centre, confidence,
    # level, method}); empty when there was none.
    suggestion: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # A person chose something else than the suggestion: it weighs more later.
    corrected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    coded_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    coded_by_id: Mapped[str] = mapped_column(String(128), nullable=False)
    coded_at: Mapped[datetime] = _created()
    created_at: Mapped[datetime] = _created()


class PayCodingCorrection(Base, OrgScoped):
    """A person chose another GL account or cost centre than the suggestion (insert
    only): the trail of what the suggestions got wrong."""

    __tablename__ = CODING_CORRECTIONS
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_coding_corrections_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "invoice_id"],
            ["pay_invoices.org_id", "pay_invoices.id"],
            ondelete="RESTRICT",
            name="fk_pay_coding_corrections_invoice_org",
        ),
        Index("ix_pay_coding_corrections_invoice", "org_id", "invoice_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    line_index: Mapped[int] = mapped_column(Integer, nullable=False)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    suggested: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    chosen_gl: Mapped[str | None] = mapped_column(String(32), nullable=True)
    chosen_cost_centre: Mapped[str | None] = mapped_column(String(32), nullable=True)
    corrected_by: Mapped[uuid.UUID] = _user_fk(nullable=False)
    created_at: Mapped[datetime] = _created()


# ------------------------------------------------------------------ messages (pay_0010)

MESSAGES: Final = "pay_messages"


class MessageStatus(StrEnum):
    # Saved by a person; the ``pay_message`` run asks for the approval.
    SUBMITTED = "submitted"
    AWAITING_APPROVAL = "awaiting_approval"
    # Approved and handed to the engine's notifications (sent by the worker).
    QUEUED = "queued"
    DECLINED = "declined"
    CHANGES_REQUESTED = "changes_requested"
    # Not sent: the run could not start, nobody may approve, or the send was refused.
    FAILED = "failed"
    # Vendor portal (``pay_0012``): an AP reply shown in the portal at once (its
    # template in force needs no approval), and a message a vendor wrote in the portal.
    POSTED = "posted"
    RECEIVED = "received"


# What a vendor's portal shows of the messages AP sent: sent or posted, never drafts.
VISIBLE_TO_VENDOR: Final = frozenset({MessageStatus.QUEUED.value, MessageStatus.POSTED.value})


class MessageRecipient(StrEnum):
    VENDOR = "vendor"
    REQUESTER = "requester"
    # The organisation's accounts payable team (a vendor's message from the portal).
    AP = "ap"


class MessageChannel(StrEnum):
    EMAIL = "email"
    PORTAL = "portal"


class MessageDirection(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class PayMessage(Base, OrgScoped):
    """A drafted message about an invoice exception, to the vendor (a contact on file)
    or to the requester (a member of the organisation). Sent only after a person
    approved its exact text (the engine's sensitive template ``pay.vendor.message``)."""

    __tablename__ = MESSAGES
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_pay_messages_org_id_id"),
        UniqueConstraint("org_id", "reference", name="uq_pay_messages_org_id_reference"),
        ForeignKeyConstraint(
            ["org_id", "invoice_id"],
            ["pay_invoices.org_id", "pay_invoices.id"],
            ondelete="RESTRICT",
            name="fk_pay_messages_invoice_org",
        ),
        CheckConstraint(f"status IN ({_in(MessageStatus)})", name="status_valid"),
        CheckConstraint(f"recipient_kind IN ({_in(MessageRecipient)})", name="recipient_valid"),
        CheckConstraint("locale IN ('en', 'ar')", name="locale_valid"),
        CheckConstraint(
            "recipient_kind <> 'requester' OR recipient_user_id IS NOT NULL",
            name="requester_is_member",
        ),
        CheckConstraint(
            "recipient_kind <> 'vendor' OR recipient_email IS NOT NULL", name="vendor_has_email"
        ),
        CheckConstraint(
            "status <> 'queued' OR (approval_id IS NOT NULL AND notification_id IS NOT NULL)",
            name="queued_has_approval",
        ),
        ForeignKeyConstraint(
            ["org_id", "vendor_id"],
            ["pay_vendors.org_id", "pay_vendors.id"],
            ondelete="RESTRICT",
            name="fk_pay_messages_vendor_org",
        ),
        CheckConstraint(f"channel IN ({_in(MessageChannel)})", name="channel_valid"),
        CheckConstraint(f"direction IN ({_in(MessageDirection)})", name="direction_valid"),
        CheckConstraint(
            "direction = 'inbound' OR submitted_by IS NOT NULL", name="outbound_has_author"
        ),
        CheckConstraint(
            "direction = 'outbound' OR (portal_contact_id IS NOT NULL AND channel = 'portal' "
            "AND recipient_kind = 'ap' AND status = 'received')",
            name="inbound_from_portal",
        ),
        CheckConstraint(
            "status NOT IN ('posted', 'received') OR channel = 'portal'",
            name="portal_statuses",
        ),
        Index("ix_pay_messages_invoice", "org_id", "invoice_id", "created_at"),
        Index("ix_pay_messages_vendor", "org_id", "vendor_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    reference: Mapped[str] = mapped_column(String(16), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    template_id: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient_user_id: Mapped[uuid.UUID | None] = _user_fk()
    recipient_name: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    recipient_email: Mapped[Sealed | None] = mapped_column(EncryptedString(), nullable=True)
    locale: Mapped[str] = mapped_column(String(2), nullable=False)
    message: Mapped[Sealed] = mapped_column(EncryptedString(), nullable=False)
    # The personal paragraph came from the model (prompt ``pay_exception_note``).
    ai_note: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    model_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    status_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=True
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=True
    )
    notification_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The person who wrote it (outbound); NULL for a vendor's portal message.
    submitted_by: Mapped[uuid.UUID | None] = _user_fk()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()
    sent_at: Mapped[datetime | None] = _ts()
    # Vendor portal (``pay_0012``): by email (exception messages) or in the portal, sent
    # by AP or written by the vendor, the invoice's vendor (a portal session only sees
    # its own vendor's thread) and the portal contact who wrote or is written to.
    channel: Mapped[str] = mapped_column(
        String(8), nullable=False, default=MessageChannel.EMAIL.value, server_default="email"
    )
    direction: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default=MessageDirection.OUTBOUND.value,
        server_default="outbound",
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    portal_contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
