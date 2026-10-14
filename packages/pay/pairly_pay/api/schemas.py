# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Request and response models of the Pay routes. Every name starts with ``Pay``:
OpenAPI schema names are global, and a plugin's model must never rename a core one."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)]
Domain = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        max_length=253,
        pattern=r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    ),
]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PayAddressIn(_In):
    line1: Text | None = None
    line2: Text | None = None
    city: Text | None = None
    region: Text | None = None
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    po_box: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] | None = None


class PayContactIn(_In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    role: ShortText | None = None
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)] | None = None
    phone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None


class PayBankIn(_In):
    iban: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=64)]
    account_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=2, max_length=200)
    ]
    bank_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)] | None = (
        None
    )
    swift: Annotated[str, StringConstraints(strip_whitespace=True, max_length=16)] | None = None


class PayVendorFields(_In):
    legal_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=2, max_length=200)
    ]
    legal_name_ar: Text | None = None
    trade_name: Text | None = None
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    trade_licence: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] | None
    ) = None
    trn: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None
    address: PayAddressIn = Field(default_factory=PayAddressIn)
    contacts: Annotated[list[PayContactIn], Field(max_length=10)] = []
    phone_on_file: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None
    ) = None
    email_domains: Annotated[list[Domain], Field(max_length=10)] = []
    category: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] | None = None


class PayVendorIn(PayVendorFields):
    bank: PayBankIn | None = None
    # The person checked the similar names shown and this is a different company.
    acknowledge_similar: bool = False


class PayVendorPatchIn(_In):
    legal_name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=200)]
        | None
    ) = None
    legal_name_ar: Text | None = None
    trade_name: Text | None = None
    trade_licence: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] | None
    ) = None
    trn: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None
    address: PayAddressIn | None = None
    contacts: Annotated[list[PayContactIn], Field(max_length=10)] | None = None
    phone_on_file: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None
    ) = None
    email_domains: Annotated[list[Domain], Field(max_length=10)] | None = None
    category: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] | None = None


class PayVendorCheckIn(_In):
    legal_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    trade_name: Text | None = None
    trn: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None
    iban: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] | None = None
    swift: Annotated[str, StringConstraints(strip_whitespace=True, max_length=16)] | None = None


class PayReasonIn(_In):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class PayCancelIn(PayReasonIn):
    keep_hold: bool = False


class PayAcceptIn(_In):
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None


class PayRequestContactIn(_In):
    """Contact details the change request itself gave (an email signature, a letter).
    Stored for investigation only: never shown, never called."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    phone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)] | None = None


class PayBankChangeIn(_In):
    bank: PayBankIn
    source: Literal["staff", "email", "invoice", "vendor_portal"] = "staff"
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None
    request_contact: PayRequestContactIn | None = None


class PayCallIn(_In):
    result: Literal["confirmed", "not_confirmed", "no_answer"]
    notes: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None
    # "I called the number on file shown on this page, not a number from the request."
    called_number_on_file: bool


class PayCheckSettingsIn(_In):
    """Invoice check settings (``pairly_pay.checks.settings``); omitted keys keep their
    current value."""

    standard_vat_rate: Annotated[Decimal, Field(ge=0, le=100)] | None = None
    extra_vat_rates: (
        Annotated[list[Annotated[Decimal, Field(ge=0, le=100)]], Field(max_length=10)] | None
    ) = None
    rounding_units: Annotated[int, Field(ge=0, le=100)] | None = None
    duplicate_window_days: Annotated[int, Field(ge=0, le=366)] | None = None
    split_window_days: Annotated[int, Field(ge=0, le=366)] | None = None
    split_margin_pct: Annotated[Decimal, Field(ge=0, le=100)] | None = None
    approval_threshold: Annotated[Decimal, Field(ge=0, le=1_000_000_000)] | None = None
    approval_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    round_unit: Annotated[Decimal, Field(ge=Decimal("0.01"), le=1_000_000_000)] | None = None
    round_minimum: Annotated[Decimal, Field(ge=0, le=1_000_000_000)] | None = None
    outlier_threshold: Annotated[Decimal, Field(ge=Decimal("0.5"), le=100)] | None = None
    outlier_min_history: Annotated[int, Field(ge=1, le=1000)] | None = None
    outlier_min_ratio: Annotated[Decimal, Field(ge=1, le=1000)] | None = None
    metadata_late_days: Annotated[int, Field(ge=1, le=3650)] | None = None


class PayPaymentSettingsIn(_In):
    """Approval and payment settings (``pairly_pay.payment_settings``); omitted keys keep
    their current value. ``auto_approve_low_risk`` is off by default."""

    auto_approve_low_risk: bool | None = None
    auto_approve_limit: Annotated[Decimal, Field(ge=0, le=1_000_000_000)] | None = None
    auto_approve_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    payer_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=140)] | None = (
        None
    )
    payer_iban: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = (
        None
    )
    payer_bic: Annotated[str, StringConstraints(strip_whitespace=True, max_length=11)] | None = None
    default_cash_limit: Annotated[Decimal, Field(gt=0, le=1_000_000_000_000)] | None = None


class PaySettingsIn(_In):
    cooling_off_hours: Annotated[int, Field(ge=0, le=720)]
    phone_change_window_days: Annotated[int, Field(ge=0, le=365)] = 30
    checks: PayCheckSettingsIn | None = None
    payments: PayPaymentSettingsIn | None = None


class PayInviteIn(_In):
    company_hint: Text | None = None
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)] | None = None
    locale: Literal["en", "ar"] = "en"


class PayTokenIn(_In):
    token: Annotated[str, StringConstraints(min_length=10, max_length=200)]


class PayOnboardingIn(PayVendorFields):
    phone_on_file: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=5, max_length=40)
    ]
    contacts: Annotated[list[PayContactIn], Field(min_length=1, max_length=10)]
    bank: PayBankIn
    confirm: Literal[True]


# ------------------------------------------------------------------ out


class PayDuplicateOut(BaseModel):
    vendor_id: uuid.UUID
    reference: str
    legal_name: str
    status: str
    reasons: list[str]
    score: str


class PayFieldCheckOut(BaseModel):
    valid: bool
    error: str | None = None
    country: str | None = None
    bank_code: str | None = None
    formatted: str | None = None


class PayVendorCheckOut(BaseModel):
    duplicates: list[PayDuplicateOut]
    trn: PayFieldCheckOut | None
    iban: PayFieldCheckOut | None
    swift: PayFieldCheckOut | None
    warnings: list[str]


class PayBankAccountOut(BaseModel):
    id: uuid.UUID
    status: str
    iban_masked: str
    country: str
    bank_code: str | None
    bank_name: str | None
    swift: str | None
    account_name: str
    created_at: datetime
    activated_at: datetime | None
    retired_at: datetime | None


class PayTicketSummary(BaseModel):
    id: uuid.UUID
    reference: str
    vendor_id: uuid.UUID
    vendor_name: str
    vendor_reference: str
    status: str
    source: str
    new_iban_masked: str
    created_at: datetime
    updated_at: datetime
    activates_at: datetime | None
    outcome: str | None


class PayTicketList(BaseModel):
    items: list[PayTicketSummary]


class PayVendorSummary(BaseModel):
    id: uuid.UUID
    reference: str
    legal_name: str
    trade_name: str | None
    country: str
    trn: str | None
    status: str
    source: str
    payment_hold: bool
    hold_reason: str | None
    iban_masked: str | None
    open_ticket_id: uuid.UUID | None
    review_reasons: list[str]
    created_at: datetime


class PayVendorList(BaseModel):
    items: list[PayVendorSummary]


class PayContactOut(BaseModel):
    name: str
    role: str | None = None
    email: str | None = None
    phone: str | None = None


class PayVendorDetail(PayVendorSummary):
    legal_name_ar: str | None
    trade_licence: str | None
    trn_status: str
    address: dict[str, Any]
    contacts: list[PayContactOut]
    phone_on_file: str | None
    phone_changed_at: datetime | None
    email_domains: list[str]
    category: str | None
    document_ids: list[str]
    hold_set_at: datetime | None
    hold_sticky: bool
    accounts: list[PayBankAccountOut]
    risk: dict[str, Any]
    tickets: list[PayTicketSummary]
    duplicates: list[PayDuplicateOut]
    activated_at: datetime | None
    can_write: bool
    can_request_change: bool
    can_manage_hold: bool


class PayCallOut(BaseModel):
    id: uuid.UUID
    result: str
    called_by: uuid.UUID
    number_last4: str
    notes: str | None
    created_at: datetime


class PayApprovalStepOut(BaseModel):
    name: str
    status: str
    decided_by: list[uuid.UUID]


class PayVerificationOut(BaseModel):
    # The number on file at opening, the only one to call. Only for people who verify.
    phone_on_file: str | None
    phone_last4: str
    phone_recently_changed: bool
    verified_by: uuid.UUID | None
    verified_at: datetime | None
    calls: list[PayCallOut]


class PayTicketDetail(PayTicketSummary):
    requested_by_type: str
    requested_by_id: str
    requester_user_id: uuid.UUID | None
    note: str | None
    request_had_contact_details: bool
    previous_account: PayBankAccountOut | None
    proposed_account: PayBankAccountOut
    # The full new IBAN, to read back to the vendor on the call. Verifiers and approvers.
    proposed_iban: str | None
    verification: PayVerificationOut
    approval_id: uuid.UUID | None
    approval_status: str | None
    approval_steps: list[PayApprovalStepOut]
    approver_ids: list[uuid.UUID]
    approved_at: datetime | None
    cooling_off_hours: int
    completed_at: datetime | None
    closed_at: datetime | None
    hold_kept: bool | None
    vendor_payment_hold: bool
    vendor_hold_reason: str | None
    decision_ids: list[str]
    can_verify: bool
    can_cancel: bool
    can_start_approval: bool


class PaySettingsOut(BaseModel):
    cooling_off_hours: int
    phone_change_window_days: int
    # The invoice check settings in force (defaults filled in).
    checks: dict[str, Any] = Field(default_factory=dict)
    # The approval and payment settings in force (defaults filled in).
    payments: dict[str, Any] = Field(default_factory=dict)
    can_manage: bool


class PayInviteOut(BaseModel):
    id: uuid.UUID
    company_hint: str | None
    email: str | None
    locale: str
    state: str
    expires_at: datetime
    used_at: datetime | None
    vendor_id: uuid.UUID | None
    created_at: datetime


class PayInviteCreatedOut(PayInviteOut):
    # Shown once: the token goes in the URL fragment, never in a server log.
    token: str
    path: str


class PayInviteList(BaseModel):
    items: list[PayInviteOut]


class PayVendorSessionOut(BaseModel):
    session_token: str
    expires_at: datetime


class PayOnboardingDocumentOut(BaseModel):
    id: uuid.UUID
    filename: str | None


class PayOnboardingPageOut(BaseModel):
    company: str
    company_hint: str | None
    locale: str
    documents: list[PayOnboardingDocumentOut]
    max_documents: int
    expires_at: datetime


class PayOnboardingDoneOut(BaseModel):
    reference: str
    status: str
