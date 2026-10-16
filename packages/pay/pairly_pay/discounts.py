# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Early-payment discounts (brief 4.6.4): where paying early saves money.

Pure and deterministic. ``parse_terms`` reads a discount from payment terms text, the
invoice's printed terms first, the vendor's agreed terms (``PayVendor.payment_terms``)
when the invoice prints none:

* ``2/10 net 30``, ``2/10, n/30``, ``1.5/15 n 45``: 2 percent off when paid within 10
  days of the invoice date, otherwise the full amount within 30;
* ``2% 10 days, net 30``, ``2% discount if paid within 10 days``;
* Arabic: ``خصم 2% خلال 10 أيام، صافي 30 يوماً`` (Arabic-Indic digits too).

A bare ``2/10`` (it could be a date) is not a discount unless a net period or a percent
sign goes with it. Percent between 0 and 50 exclusive, the discount period 1 to 365
days and shorter than the net period.

``early_payment`` turns terms into an offer on one amount: the saving (percent of what
is due, rounded half up to the currency's minor unit, ``Decimal`` only), the pay-by date
(the invoice date plus the discount period), the amount to pay early, and the annualised
rate the discount is worth (simple interest over the days paid early). An offer is
``available`` on a day up to and including its pay-by date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final, Literal

from pairly_pay.checks.settings import amount_text, to_minor_unit
from pairly_pay.models import DocumentType, PayInvoice

Source = Literal["invoice", "vendor"]

MAX_PERCENT: Final = Decimal(50)
MAX_DAYS: Final = 365
RATE_PLACES: Final = Decimal("0.0001")

# Arabic-Indic and Persian digits, and the Arabic decimal separator (U+066B).
_DIGITS: Final = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹\u066b", "01234567890123456789.")
_NUM: Final = r"\d{1,2}(?:[.,]\d{1,3})?"
_SLASH: Final = re.compile(
    rf"(?<![\d/.,])(?P<pct>{_NUM})\s*(?P<sign>%)?\s*/\s*(?P<days>\d{{1,3}})(?![\d/])"
    r"(?:\s*,?\s*(?:net|n)\s*/?\s*(?P<net>\d{1,3}))?",
    re.IGNORECASE,
)
_WORDS: Final = re.compile(
    rf"(?<![\d.,])(?P<pct>{_NUM})\s*%\s*(?:discount\s+)?(?:if\s+paid\s+)?"
    r"(?:within\s+|in\s+)?(?P<days>\d{1,3})\s*days?"
    r"(?:.{0,40}?\bnet\s*(?P<net>\d{1,3}))?",
    re.IGNORECASE,
)
_ARABIC: Final = re.compile(
    rf"خصم\s*(?P<pct>{_NUM})\s*[%٪]\s*(?:عند\s+الدفع\s+)?(?:خلال|في\s+غضون)\s*"
    r"(?P<days>\d{1,3})\s*(?:يوماً|يوما|يوم|أيام)"
    r"(?:.{0,40}?صافي\s*(?P<net>\d{1,3}))?"
)


@dataclass(frozen=True, slots=True)
class DiscountTerms:
    percent: Decimal
    days: int
    net_days: int | None = None

    @property
    def label(self) -> str:
        base = f"{self.percent.normalize():f}/{self.days}"
        return f"{base} net {self.net_days}" if self.net_days is not None else base


def _terms(pct: str, days: str, net: str | None) -> DiscountTerms | None:
    try:
        percent = Decimal(pct.replace(",", "."))
    except InvalidOperation:
        return None
    period = int(days)
    net_days = int(net) if net else None
    if not (Decimal(0) < percent < MAX_PERCENT) or not (1 <= period <= MAX_DAYS):
        return None
    if net_days is not None and not (period < net_days <= MAX_DAYS):
        return None
    return DiscountTerms(percent, period, net_days)


def parse_terms(text: str | None) -> DiscountTerms | None:
    """The early-payment discount in ``text``, or ``None`` when it offers none."""
    if not text:
        return None
    clean = " ".join(text.translate(_DIGITS).split())[:300]
    for match in _SLASH.finditer(clean):
        if match.group("net") is None and match.group("sign") is None:
            continue
        found = _terms(match.group("pct"), match.group("days"), match.group("net"))
        if found is not None:
            return found
    for pattern in (_WORDS, _ARABIC):
        for match in pattern.finditer(clean):
            found = _terms(match.group("pct"), match.group("days"), match.group("net"))
            if found is not None:
                return found
    return None


@dataclass(frozen=True, slots=True)
class EarlyPayment:
    terms: DiscountTerms
    source: Source
    currency: str
    # What is due without the discount (after applied credits).
    amount: Decimal
    saving: Decimal
    pay_by: date
    net_due: date | None
    # The discount as a yearly rate over the days paid early; None without a net period.
    annual_rate: Decimal | None

    @property
    def discounted(self) -> Decimal:
        """What is paid when paid by ``pay_by``."""
        return self.amount - self.saving

    def available(self, day: date) -> bool:
        return day <= self.pay_by

    def days_left(self, day: date) -> int:
        return (self.pay_by - day).days


def early_payment(
    *,
    invoice_terms: str | None,
    vendor_terms: str | None,
    invoice_date: date | None,
    amount: Decimal | None,
    currency: str | None,
) -> EarlyPayment | None:
    """The early-payment offer on ``amount``, from the invoice's terms first, else the
    vendor's. ``None`` without terms that offer a discount, an invoice date, a positive
    amount or a currency."""
    if invoice_date is None or amount is None or amount <= 0 or not currency:
        return None
    source: Source = "invoice"
    terms = parse_terms(invoice_terms)
    if terms is None:
        source = "vendor"
        terms = parse_terms(vendor_terms)
    if terms is None:
        return None
    due = to_minor_unit(amount, currency)
    saving = to_minor_unit(due * terms.percent / Decimal(100), currency)
    if saving <= 0 or saving >= due:
        return None
    rate: Decimal | None = None
    if terms.net_days is not None:
        early_days = Decimal(terms.net_days - terms.days)
        rate = (
            terms.percent / (Decimal(100) - terms.percent) * Decimal(365) / early_days
        ).quantize(RATE_PLACES, rounding=ROUND_HALF_UP)
    return EarlyPayment(
        terms=terms,
        source=source,
        currency=currency,
        amount=due,
        saving=saving,
        pay_by=invoice_date + timedelta(days=terms.days),
        net_due=invoice_date + timedelta(days=terms.net_days)
        if terms.net_days is not None
        else None,
        annual_rate=rate,
    )


def amount_due_of(invoice: PayInvoice) -> Decimal | None:
    """What is due on an invoice: the approved amount (the total before an approval)
    less the credit notes applied to it."""
    base = invoice.approved_amount if invoice.approved_amount is not None else invoice.total
    if base is None:
        return None
    return base - (invoice.credit_total or Decimal(0))


def invoice_offer(invoice: PayInvoice, vendor_terms: str | None) -> EarlyPayment | None:
    """The early-payment offer on an invoice (never on a credit note, which pays
    nothing): its printed terms first, else the vendor's."""
    if invoice.document_type != DocumentType.INVOICE.value:
        return None
    return early_payment(
        invoice_terms=invoice.payment_terms,
        vendor_terms=vendor_terms,
        invoice_date=invoice.invoice_date,
        amount=amount_due_of(invoice),
        currency=invoice.approved_currency or invoice.currency,
    )


def offer_json(offer: EarlyPayment, today: date) -> dict[str, Any]:
    """The offer as the API shows it (amounts at the currency's minor unit, as text)."""
    return {
        "terms": offer.terms.label,
        "source": offer.source,
        "percent": format(offer.terms.percent.normalize(), "f"),
        "currency": offer.currency,
        "amount": amount_text(offer.amount, offer.currency),
        "saving": amount_text(offer.saving, offer.currency),
        "pay_early": amount_text(offer.discounted, offer.currency),
        "pay_by": offer.pay_by.isoformat(),
        "net_due": offer.net_due.isoformat() if offer.net_due else None,
        "annual_rate": format(offer.annual_rate, "f") if offer.annual_rate is not None else None,
        "available": offer.available(today),
        "days_left": offer.days_left(today),
    }
