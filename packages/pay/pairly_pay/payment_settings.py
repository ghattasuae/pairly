# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Approval and payment settings per organisation (``pay_settings.payment_settings``).

* ``auto_approve_low_risk`` (default **off**): when an organisation turns it on, an
  invoice may be approved by a rule instead of a person, only if every one of these
  holds: the risk level is ``low`` and does not ask for a person, the amount is under
  ``auto_approve_limit`` in ``auto_approve_currency``, the vendor's risk is low, and
  the invoice could be submitted at all (current checks, no hold, no open bank-detail
  change). High and critical risk invoices are never approved by a rule, whatever the
  amount. The rule's approval is recorded as a decision record of its own. A payment
  run still needs a finance controller (a person) before any bank file exists.
* ``payer_name``, ``payer_iban``, ``payer_bic``: the organisation's own account, the
  debtor of the bank file (synthetic in every fixture).
* ``default_cash_limit``: the cash limit a new payment run proposes when none is given.

Unknown keys are ignored; a malformed value keeps its default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_pay.iban import normalize_iban, validate_bic, validate_iban
from pairly_pay.models import PaySettings

MAX_AMOUNT: Final = Decimal("1000000000000")


@dataclass(frozen=True, slots=True)
class PaymentSettings:
    auto_approve_low_risk: bool = False
    auto_approve_limit: Decimal = Decimal(0)
    auto_approve_currency: str = "AED"
    payer_name: str | None = None
    payer_iban: str | None = None
    payer_bic: str | None = None
    default_cash_limit: Decimal | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "auto_approve_low_risk": self.auto_approve_low_risk,
            "auto_approve_limit": str(self.auto_approve_limit),
            "auto_approve_currency": self.auto_approve_currency,
            "payer_name": self.payer_name,
            "payer_iban": self.payer_iban,
            "payer_bic": self.payer_bic,
            "default_cash_limit": str(self.default_cash_limit)
            if self.default_cash_limit is not None
            else None,
        }


def _amount(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and Decimal(0) <= number <= MAX_AMOUNT else None


def parse_payment_settings(data: Mapping[str, Any] | None) -> PaymentSettings:
    if not isinstance(data, Mapping):
        return PaymentSettings()
    auto = data.get("auto_approve_low_risk") is True
    limit = _amount(data.get("auto_approve_limit")) or Decimal(0)
    currency = data.get("auto_approve_currency")
    currency = (
        currency.upper()
        if isinstance(currency, str) and len(currency) == 3 and currency.isalpha()
        else "AED"
    )
    name = data.get("payer_name")
    name = name.strip()[:140] if isinstance(name, str) and name.strip() else None
    iban = data.get("payer_iban")
    iban = normalize_iban(iban) if isinstance(iban, str) else None
    if iban is not None and not validate_iban(iban).valid:
        iban = None
    bic_text = data.get("payer_bic")
    bic_check = validate_bic(bic_text) if isinstance(bic_text, str) else None
    bic = bic_check.bic if bic_check is not None and bic_check.valid else None
    return PaymentSettings(
        auto_approve_low_risk=auto,
        auto_approve_limit=limit,
        auto_approve_currency=currency,
        payer_name=name,
        payer_iban=iban,
        payer_bic=bic,
        default_cash_limit=_amount(data.get("default_cash_limit")),
    )


async def load_payment_settings(session: AsyncSession) -> PaymentSettings:
    row = await session.scalar(select(PaySettings))
    return parse_payment_settings(row.payment_settings if row is not None else None)
