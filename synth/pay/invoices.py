# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Invoice content: line items, Decimal totals, numbering, terms and the ``InvoiceDoc``
the renderers draw.

All money is ``Decimal`` rounded half up to two places per line: ``line_total =
quantity x unit_price``, ``vat_amount = line_total x rate`` (per line), ``subtotal =
sum(line_total)``, ``vat_amount = sum(line vat)``, ``total = subtotal + vat``. Attack
invoices that break the arithmetic say so in their manifest record.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from synth.pay.catalog import (
    BUYER_NAME,
    BUYER_NAME_AR,
    BUYER_TRN,
    CATEGORY_BY_KEY,
    UNIT_AR,
    Item,
)
from synth.pay.models import BankDetails, InvoiceTruth, LineTruth, VatCategory, Vendor

CENT: Final = Decimal("0.01")
RATES: Final[dict[str, Decimal]] = {
    "S": Decimal("5.00"),
    "Z": Decimal("0.00"),
    "O": Decimal("0.00"),
}


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def money(value: Decimal) -> str:
    return str(q2(value))


def qty_str(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def printed_money(value: str | Decimal) -> str:
    """``12345.5`` -> ``12,345.50`` (the printed form; ground truth keeps ``12345.50``)."""
    return f"{q2(Decimal(value)):,.2f}"


@dataclass
class LineSpec:
    item: Item
    quantity: Decimal
    unit_price: Decimal
    vat: VatCategory


@dataclass
class Hidden:
    """Text a person never sees: ``white``, ``tiny``, ``offpage``, ``behind`` (under a
    drawn box), or one of the metadata channels (``meta_subject``, ``meta_keywords``,
    ``docx_hidden``, ``docx_comment``, ``xml_note``)."""

    text: str
    mode: str


@dataclass
class Edit:
    """An editing trace: the original value covered by a white box, a new one on top."""

    field: str
    old: str
    new: str


@dataclass
class InvoiceDoc:
    id: str
    vendor: Vendor
    truth: InvoiceTruth
    format: str
    layout: str
    language: str
    font: str
    accent: str
    logo: str
    date_format: str
    producer: str
    seed: int
    notes: list[str] = field(default_factory=list)
    hidden: list[Hidden] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    pdf_dates: tuple[str, str] | None = None  # (creation, modification), PDF date strings
    edit: Edit | None = None
    filename: str | None = None
    seq: int = 0


def describe(item: Item, language: str) -> str:
    return item.ar if language == "ar" else item.en


def unit_label(unit: str, language: str) -> str:
    return UNIT_AR.get(unit, unit) if language == "ar" else unit


def pick_lines(rng: random.Random, vendor: Vendor, target: Decimal) -> list[LineSpec]:
    """Items from the vendor's category with quantities that land near ``target``."""
    items = CATEGORY_BY_KEY[vendor.category].items
    n = min(len(items), rng.choice((1, 2, 2, 3, 3, 4, 5, 6)))
    chosen = rng.sample(items, n)
    share = target / n
    lines: list[LineSpec] = []
    for item in chosen:
        price = Decimal(str(round(rng.uniform(item.low, item.high), 2)))
        if item.qty == "one":
            qty = Decimal(1)
            if share > price * 3:
                price = q2(share * Decimal(str(round(rng.uniform(0.7, 1.0), 2))))
        elif item.qty == "dec":
            qty = max(Decimal("0.5"), (share / price * 2).quantize(Decimal(1)) / 2)
        else:
            qty = max(Decimal(1), (share / price).quantize(Decimal(1)))
            if item.qty == "int":
                qty = min(qty, Decimal(60))
        lines.append(LineSpec(item, qty, price, _vat(item, vendor)))
    return lines


def _vat(item: Item, vendor: Vendor) -> VatCategory:
    if vendor.country != "AE":
        return "O"
    return item.vat


def line_truths(lines: list[LineSpec], language: str) -> tuple[LineTruth, ...]:
    out = []
    for n, line in enumerate(lines, start=1):
        total = q2(line.quantity * line.unit_price)
        rate = RATES[line.vat]
        out.append(
            LineTruth(
                line_no=n,
                description=describe(line.item, language),
                quantity=qty_str(line.quantity),
                unit=unit_label(line.item.unit, language),
                unit_price=money(line.unit_price),
                vat_category=line.vat,
                vat_rate=str(rate),
                vat_amount=money(total * rate / 100),
                line_total=money(total),
            )
        )
    return tuple(out)


def totals(lines: tuple[LineTruth, ...]) -> tuple[str, str, str, str]:
    """(subtotal, vat_rate, vat_amount, total)."""
    subtotal = sum((Decimal(x.line_total) for x in lines), Decimal(0))
    vat = sum((Decimal(x.vat_amount) for x in lines), Decimal(0))
    rate = "5.00" if any(x.vat_category == "S" for x in lines) else "0.00"
    return money(subtotal), rate, money(vat), money(subtotal + vat)


def invoice_number(pattern: str, prefix: str, seq: int, date: dt.date) -> str:
    return (
        pattern.replace("{YYYY}", f"{date.year}")
        .replace("{YY}", f"{date.year % 100:02d}")
        .replace("{MM}", f"{date.month:02d}")
        .replace("{P}", prefix)
        .replace("{N4}", f"{seq:04d}")
        .replace("{N5}", f"{seq:05d}")
        .replace("{N6}", f"{seq:06d}")
    )


def vendor_prefix(vendor: Vendor) -> str:
    return vendor.trade_name[:3].upper()


def terms_text(days: int, language: str) -> str:
    if language == "ar":
        return "الدفع عند الاستلام" if days == 0 else f"صافي {days} يوماً"
    en = "Due on receipt" if days == 0 else f"Net {days}"
    if language == "bilingual":
        ar = "الدفع عند الاستلام" if days == 0 else f"صافي {days} يوماً"
        return f"{en} / {ar}"
    return en


def print_date(value: str, fmt: str) -> str:
    return dt.date.fromisoformat(value).strftime(fmt)


def make_truth(
    vendor: Vendor,
    *,
    number: str,
    date: dt.date,
    lines: tuple[LineTruth, ...],
    language: str,
    po_number: str | None,
    bank: BankDetails | None,
    currency: str | None = None,
    terms_days: int | None = None,
) -> InvoiceTruth:
    days = vendor.template.payment_terms_days if terms_days is None else terms_days
    subtotal, rate, vat, total = totals(lines)
    return InvoiceTruth(
        vendor_id=vendor.id,
        vendor_name=vendor.legal_name_ar
        if language == "ar" and vendor.legal_name_ar
        else vendor.legal_name,
        vendor_name_ar=vendor.legal_name_ar if language != "en" else None,
        vendor_trn=vendor.trn,
        buyer_name=BUYER_NAME_AR if language == "ar" else BUYER_NAME,
        buyer_trn=BUYER_TRN,
        invoice_number=number,
        invoice_date=date.isoformat(),
        due_date=(date + dt.timedelta(days=days)).isoformat(),
        payment_terms=terms_text(days, language),
        payment_terms_days=days,
        currency=currency or vendor.template.currency,
        po_number=po_number,
        subtotal=subtotal,
        vat_rate=rate,
        vat_amount=vat,
        total=total,
        bank=bank,
        lines=lines,
    )
