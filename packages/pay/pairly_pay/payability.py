# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Can this invoice be approved or paid right now? One answer for every step.

``blockers`` lists every reason an invoice cannot move towards payment, read from the
database in the caller's transaction:

* ``no_vendor`` / ``vendor_not_active``: no active vendor is linked;
* ``vendor_held:<reason>``: the vendor's payments are on hold (``pairly_pay.holds``);
* ``bank_change_open``: a bank-detail change ticket of the vendor is open;
* ``no_bank_details``: the vendor has no bank account on file at all;
* ``no_verified_account``: its only details are not verified yet (proposed, waiting for
  their change ticket; security review 2026-10, M-03);
* ``checks_stale`` / ``checks_failed`` / ``checks_missing`` (``pairly_pay.freshness``);
* ``amount_missing``, ``currency_missing``: nothing to pay.

Submitting for approval, the approval's final action, approval by a rule, proposing a
payment run, the run's final action and the bank file export all call it again, so a
change made in between (a hold, a new ticket, a correction) is always seen (fail
closed). ``bank_on_file`` returns the vendor's active account and the keyed digest of
its details: what an approval binds and what the export compares with.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_pay.bank_changes import details_digest, details_match, open_ticket_of
from pairly_pay.freshness import checks_problem
from pairly_pay.models import (
    AccountStatus,
    PayInvoice,
    PayVendor,
    PayVendorBankAccount,
    VendorStatus,
)
from pairly_pay.vendors import account_name_of, active_account, iban_of


@dataclass(frozen=True, slots=True)
class BankOnFile:
    account: PayVendorBankAccount
    digest: str


def account_digest(account: PayVendorBankAccount) -> str:
    """Keyed digest of an account's details (the same digest a change ticket binds)."""
    return details_digest(
        account.org_id, iban_of(account), account_name_of(account), account.bank_name, account.swift
    )


def same_details(digest: str | None, account: PayVendorBankAccount) -> bool:
    """Whether ``account`` still holds the details ``digest`` was made of."""
    return digest is not None and details_match(digest, account)


async def bank_on_file(session: AsyncSession, vendor_id: uuid.UUID) -> BankOnFile | None:
    account = await active_account(session, vendor_id)
    if account is None:
        return None
    return BankOnFile(account, account_digest(account))


async def vendor_blockers(session: AsyncSession, vendor: PayVendor | None) -> list[str]:
    if vendor is None:
        return ["no_vendor"]
    out: list[str] = []
    if vendor.status != VendorStatus.ACTIVE.value:
        out.append("vendor_not_active")
    if vendor.payment_hold:
        out.append(f"vendor_held:{vendor.hold_reason or 'hold'}")
    if await open_ticket_of(session, vendor.id) is not None:
        out.append("bank_change_open")
    if await active_account(session, vendor.id) is None:
        # Details that are not verified yet (a new vendor's, waiting for their ticket)
        # are never paid (M-03): a reason of their own.
        proposed = await session.scalar(
            select(PayVendorBankAccount.id).where(
                PayVendorBankAccount.vendor_id == vendor.id,
                PayVendorBankAccount.status == AccountStatus.PROPOSED.value,
            )
        )
        out.append("no_verified_account" if proposed is not None else "no_bank_details")
    return out


async def blockers(session: AsyncSession, invoice: PayInvoice) -> list[str]:
    """Every reason ``invoice`` cannot be approved or paid now (empty: none)."""
    vendor = (
        await session.scalar(
            select(PayVendor)
            .where(PayVendor.id == invoice.vendor_id)
            .execution_options(populate_existing=True)
        )
        if invoice.vendor_id is not None
        else None
    )
    out = await vendor_blockers(session, vendor)
    problem = await checks_problem(session, invoice)
    if problem is not None:
        out.append(problem)
    if invoice.total is None or invoice.total <= Decimal(0):
        out.append("amount_missing")
    if invoice.currency is None:
        out.append("currency_missing")
    return out
