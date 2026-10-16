# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pydantic models for the synthetic Pay data: vendors, invoice ground truth, purchase
orders, goods receipts, matching cases, attack labels and inbound emails.

Money is always a ``Decimal`` string with two places (``"1234.50"``), quantities a
``Decimal`` string, dates ISO ``YYYY-MM-DD``. Nothing here is real: see
``synth/pay/README.md``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Money = str
VatCategory = Literal["S", "Z", "O"]  # standard 5%, zero-rated, out of scope (reverse charge)
Format = Literal["pdf", "png", "jpg", "scan_pdf", "docx", "xml", "email_body"]
Layout = Literal["classic", "band", "grid", "compact", "rtl", "bilingual", "ubl", "plain"]
Language = Literal["en", "ar", "bilingual"]
TextLayer = Literal["logical", "visual_shaped", "image", "structured"]
Channel = Literal["email", "upload", "api"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _money(value: str) -> str:
    amount = Decimal(value)
    if amount.as_tuple().exponent != -2:
        raise ValueError(f"money must have exactly two decimal places: {value!r}")
    return value


# ------------------------------------------------------------------ vendors


class Address(_Model):
    line1: str
    line2: str
    city: str
    region: str
    country: str = Field(pattern=r"^[A-Z]{2}$")
    po_box: str | None = None


class Contact(_Model):
    name: str
    role: str
    email: str
    phone: str


class BankDetails(_Model):
    account_name: str
    bank_name: str
    iban: str
    bic: str
    country: str = Field(pattern=r"^[A-Z]{2}$")


class Template(_Model):
    """How a vendor's invoices usually look (a change is a fake-invoice signal)."""

    layout: Layout
    font: Literal["Helvetica", "Times-Roman", "Courier"]
    accent: str = Field(pattern=r"^#[0-9a-f]{6}$")
    numbering: str
    date_format: str
    payment_terms_days: int
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    language: Language
    formats: dict[str, int]
    prints_bank: bool
    logo: Literal["circle", "square", "bar", "none"]
    producer: str


class Vendor(_Model):
    id: str
    legal_name: str
    legal_name_ar: str | None
    trade_name: str
    country: str
    trade_licence: str
    trn: str | None
    tax_id: str | None
    address: Address
    contacts: tuple[Contact, ...]
    phone_on_file: str
    bank: BankDetails
    email_domains: tuple[str, ...]
    sender_addresses: tuple[str, ...]
    category: str
    template: Template
    po_based: bool
    since: str
    typical_subtotal: tuple[Money, Money]


# ------------------------------------------------------------------ invoices


class LineTruth(_Model):
    line_no: int
    description: str
    quantity: str
    unit: str
    unit_price: Money
    vat_category: VatCategory
    vat_rate: str
    vat_amount: Money
    line_total: Money

    @field_validator("unit_price", "vat_amount", "line_total")
    @classmethod
    def _two_places(cls, value: str) -> str:
        return _money(value)


class InvoiceTruth(_Model):
    """What a perfect extractor reads from the visible invoice (hidden text excluded)."""

    vendor_id: str | None
    vendor_name: str
    vendor_name_ar: str | None
    vendor_trn: str | None
    buyer_name: str
    buyer_trn: str
    invoice_number: str
    invoice_date: str
    due_date: str
    payment_terms: str
    payment_terms_days: int
    currency: str
    po_number: str | None
    subtotal: Money
    vat_rate: str
    vat_amount: Money
    total: Money
    bank: BankDetails | None
    lines: tuple[LineTruth, ...]

    @field_validator("subtotal", "vat_amount", "total")
    @classmethod
    def _two_places(cls, value: str) -> str:
        return _money(value)


class AttackInfo(_Model):
    types: tuple[str, ...]
    description: str
    expected_findings: tuple[str, ...]
    original_id: str | None = None
    related_ids: tuple[str, ...] = ()
    payloads: tuple[str, ...] = ()
    correct_values: dict[str, str] = Field(default_factory=dict)
    notes: dict[str, str] = Field(default_factory=dict)


class InvoiceRecord(_Model):
    id: str
    kind: Literal["invoice", "attack"]
    file: str
    format: Format
    layout: Layout
    language: Language
    text_layer: TextLayer
    channel: Channel
    email_id: str | None
    date_format: str
    usual_template: bool
    demo: str | None = None
    truth: InvoiceTruth
    attack: AttackInfo | None = None


# ------------------------------------------------------------------ matching


class PoLine(_Model):
    line_no: int
    description: str
    quantity: str
    unit: str
    unit_price: Money
    line_total: Money


class PurchaseOrder(_Model):
    po_number: str
    vendor_id: str
    date: str
    currency: str
    department: str
    requester: str
    kind: Literal["goods", "services"]
    match_type: Literal["2-way", "3-way"]
    lines: tuple[PoLine, ...]
    subtotal: Money


class ReceiptLine(_Model):
    po_line_no: int
    quantity_received: str


class GoodsReceipt(_Model):
    grn_number: str
    po_number: str
    date: str
    received_by: str
    lines: tuple[ReceiptLine, ...]


class MatchCase(_Model):
    id: str
    scenario: str
    po_number: str | None
    invoice_ids: tuple[str, ...]
    grn_numbers: tuple[str, ...]
    expected: Literal["match", "exception"]
    expected_exceptions: tuple[str, ...]
    note: str


# ------------------------------------------------------------------ emails


class Attachment(_Model):
    filename: str
    content_type: str
    invoice_id: str | None


class EmailRecord(_Model):
    id: str
    file: str
    kind: str
    sender: str
    original_sender: str | None
    to: str
    subject: str
    date: str
    attachments: tuple[Attachment, ...]
    body_invoice_id: str | None
    invoice_ids: tuple[str, ...]
    ignore: bool
    demo: str | None = None
    expected_findings: tuple[str, ...] = ()
