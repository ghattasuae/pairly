# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Check settings per organisation (``pay_settings.check_settings``), with defaults.

* Tax codes: ``S`` standard rate (5% in the UAE by default), ``Z`` zero-rated, ``E``
  exempt, ``RC`` reverse charge (the recipient accounts for the VAT, 0% on the invoice),
  ``O`` out of scope. A line's code is inferred from its rate and description: a 0% line
  is ``RC`` when the vendor is outside the UAE or the invoice says "reverse charge", ``Z``
  or ``E`` when its description names a zero-rated or exempt supply, else unexplained.
* Rounding: amounts are rounded half up to the currency's minor unit (2 places; 3 for
  BHD, KWD and OMR). Each comparison tolerates ``rounding_units`` minor units per
  rounded figure it adds up (one line: one unit; a sum of n lines: n units), so per-line
  and per-invoice rounding both pass and nothing larger does.
* Duplicates: the same total from the same vendor within ``duplicate_window_days``.
* Split invoices: invoices of one vendor within ``split_window_days``, each at least
  ``split_margin_pct`` percent under the approval threshold, that add up to it or more.
* Round amounts: a multiple of ``round_unit`` of at least ``round_minimum``.
* Matching (``pairly_pay.checks.matching``): an invoice unit price may be at most
  ``match_price_pct`` percent above the PO's, an invoiced quantity at most
  ``match_quantity_pct`` percent above what is left to invoice (received for 3-way,
  ordered for 2-way), and the subtotal at most the larger of ``match_total_pct``
  percent and ``match_total_amount`` above the PO value of the invoiced lines.
  ``require_po``: every invoice must cite a PO (otherwise only the invoices of vendors
  with POs on file must).
* Coding (``pairly_pay.coding``): ``auto_code_high_confidence`` lets the system code an
  invoice line by itself when the suggestion from history is of high confidence. Off by
  default: a person confirms every code.

Unknown keys in the stored JSON are ignored; a malformed value falls back to its default
(and the setting is validated when it is saved).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final

DEFAULT_STANDARD_RATE: Final = Decimal("5.00")
THREE_PLACE_CURRENCIES: Final = frozenset({"BHD", "KWD", "OMR", "JOD", "TND", "LYD", "IQD"})
# Descriptions of supplies the UAE zero-rates or exempts (Federal Decree-Law 8 of 2017,
# articles 45 and 46), as they appear on invoices. Lower case, matched as words.
ZERO_RATED_WORDS: Final = (
    "international freight",
    "international air freight",
    "international sea freight",
    "international transport",
    "international shipping",
    "export",
    "exported",
    "zero rated",
    "zero-rated",
    "شحن دولي",
    "شحن جوي دولي",
    "شحن بحري دولي",
    "نقل دولي",
    "تصدير",
    "نسبة الصفر",
)
EXEMPT_WORDS: Final = (
    "exempt",
    "residential rent",
    "life insurance",
    "bare land",
    "local passenger transport",
    "معفى",
    "معفاة",
)
REVERSE_CHARGE_WORDS: Final = (
    "reverse charge",
    "reverse-charge",
    "vat to be accounted for by the recipient",
    "احتساب عكسي",
    "الاحتساب العكسي",
)


@dataclass(frozen=True, slots=True)
class CheckSettings:
    standard_vat_rate: Decimal = DEFAULT_STANDARD_RATE
    # Other rates a line may carry besides the standard one and 0%.
    extra_vat_rates: tuple[Decimal, ...] = ()
    rounding_units: int = 1
    duplicate_window_days: int = 14
    split_window_days: int = 7
    split_margin_pct: Decimal = Decimal("10")
    approval_threshold: Decimal = Decimal("50000.00")
    approval_currency: str = "AED"
    round_unit: Decimal = Decimal("1000")
    round_minimum: Decimal = Decimal("5000")
    outlier_threshold: Decimal = Decimal("3.5")
    outlier_min_history: int = 5
    outlier_min_ratio: Decimal = Decimal("2")
    # Created more than this many days after the invoice date: a metadata flag.
    metadata_late_days: int = 30
    match_price_pct: Decimal = Decimal("2")
    match_quantity_pct: Decimal = Decimal("5")
    match_total_pct: Decimal = Decimal("2")
    match_total_amount: Decimal = Decimal("1.00")
    require_po: bool = False
    auto_code_high_confidence: bool = False
    home_country: str = "AE"
    zero_rated_words: tuple[str, ...] = field(default=ZERO_RATED_WORDS)
    exempt_words: tuple[str, ...] = field(default=EXEMPT_WORDS)

    @property
    def allowed_rates(self) -> frozenset[Decimal]:
        return frozenset({self.standard_vat_rate, Decimal(0), *self.extra_vat_rates})

    def as_json(self) -> dict[str, Any]:
        return {
            "standard_vat_rate": str(self.standard_vat_rate),
            "extra_vat_rates": [str(r) for r in self.extra_vat_rates],
            "rounding_units": self.rounding_units,
            "duplicate_window_days": self.duplicate_window_days,
            "split_window_days": self.split_window_days,
            "split_margin_pct": str(self.split_margin_pct),
            "approval_threshold": str(self.approval_threshold),
            "approval_currency": self.approval_currency,
            "round_unit": str(self.round_unit),
            "round_minimum": str(self.round_minimum),
            "outlier_threshold": str(self.outlier_threshold),
            "outlier_min_history": self.outlier_min_history,
            "outlier_min_ratio": str(self.outlier_min_ratio),
            "metadata_late_days": self.metadata_late_days,
            "match_price_pct": str(self.match_price_pct),
            "match_quantity_pct": str(self.match_quantity_pct),
            "match_total_pct": str(self.match_total_pct),
            "match_total_amount": str(self.match_total_amount),
            "require_po": self.require_po,
            "auto_code_high_confidence": self.auto_code_high_confidence,
        }


_DECIMALS: Final = {
    "standard_vat_rate": (Decimal(0), Decimal(100)),
    "split_margin_pct": (Decimal(0), Decimal(100)),
    "approval_threshold": (Decimal(0), Decimal("1000000000")),
    "round_unit": (Decimal("0.01"), Decimal("1000000000")),
    "round_minimum": (Decimal(0), Decimal("1000000000")),
    "outlier_threshold": (Decimal("0.5"), Decimal(100)),
    "outlier_min_ratio": (Decimal("1"), Decimal(1000)),
    "match_price_pct": (Decimal(0), Decimal(100)),
    "match_quantity_pct": (Decimal(0), Decimal(100)),
    "match_total_pct": (Decimal(0), Decimal(100)),
    "match_total_amount": (Decimal(0), Decimal("1000000")),
}
_INTS: Final = {
    "rounding_units": (0, 100),
    "duplicate_window_days": (0, 366),
    "split_window_days": (0, 366),
    "outlier_min_history": (1, 1000),
    "metadata_late_days": (1, 3650),
}


def _decimal(value: Any, low: Decimal, high: Decimal) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and low <= number <= high else None


def parse_settings(data: Mapping[str, Any] | None) -> CheckSettings:
    """Settings from stored JSON; anything missing or out of range keeps its default."""
    settings = CheckSettings()
    if not isinstance(data, Mapping):
        return settings
    changes: dict[str, Any] = {}
    for name, (low, high) in _DECIMALS.items():
        if name in data and (number := _decimal(data[name], low, high)) is not None:
            changes[name] = number
    for name, (ilow, ihigh) in _INTS.items():
        value = data.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and ilow <= value <= ihigh:
            changes[name] = value
    rates = data.get("extra_vat_rates")
    if isinstance(rates, list):
        parsed = [_decimal(r, Decimal(0), Decimal(100)) for r in rates[:10]]
        changes["extra_vat_rates"] = tuple(r for r in parsed if r is not None)
    for flag in ("require_po", "auto_code_high_confidence"):
        if isinstance(data.get(flag), bool):
            changes[flag] = data[flag]
    currency = data.get("approval_currency")
    if isinstance(currency, str) and len(currency) == 3 and currency.isalpha():
        changes["approval_currency"] = currency.upper()
    known = {f.name for f in fields(CheckSettings)}
    return replace(settings, **{k: v for k, v in changes.items() if k in known})


def minor_unit(currency: str | None) -> Decimal:
    return Decimal("0.001") if (currency or "") in THREE_PLACE_CURRENCIES else Decimal("0.01")


def to_minor_unit(value: Decimal, currency: str | None) -> Decimal:
    """``value`` rounded half up to the currency's minor unit (3 places for BHD, KWD,
    OMR and the other three-place currencies, else 2)."""
    return value.quantize(minor_unit(currency), rounding=ROUND_HALF_UP)


def amount_text(value: Decimal | None, currency: str | None) -> str | None:
    """The amount as a plain string at the currency's precision: what approval payloads,
    digests, audit entries and decision records carry (never fewer places than the
    currency has, so a three-place amount keeps its last digit)."""
    return None if value is None else format(to_minor_unit(value, currency), "f")


def amount_display(value: Decimal, currency: str | None) -> str:
    """The amount for a message: grouped thousands, at the currency's precision."""
    places = 3 if (currency or "") in THREE_PLACE_CURRENCIES else 2
    return f"{to_minor_unit(value, currency):,.{places}f}"
