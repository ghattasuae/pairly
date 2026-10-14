# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pay routes, mounted by the API under ``/v1/pay`` (``PayPlugin.api_router``).

* ``vendors``: the vendor master, onboarding review, payment holds and Pay settings
  (WP4.2).
* ``bank_changes``: the bank-detail change tickets, the call back and the cancel path
  (WP4.3). The dual approval itself happens in the approvals inbox.
* ``onboarding``: invite links for the AP team, and the vendor's self-service form
  (no sign-in, rate limited).
* ``invoices``: invoice intake by upload and API, the invoice list and the side-by-side
  review (corrections, vendor link, reviewed, rejected) (WP4.4, WP4.5), and sending an
  invoice for approval (WP4.7).
* ``approval_rules``: the approval rules in force and their versions, read only.
* ``payment_runs``: propose, approve (in the inbox), export the bank file, mark paid.

Imported only by the API process (the worker never loads routes).
"""

from __future__ import annotations

from fastapi import APIRouter

from pairly_pay.api import (
    approval_rules,
    bank_changes,
    invoices,
    onboarding,
    payment_runs,
    vendors,
)

router = APIRouter()
router.include_router(vendors.router)
router.include_router(bank_changes.router)
router.include_router(onboarding.router)
router.include_router(invoices.router)
router.include_router(approval_rules.router)
router.include_router(payment_runs.router)
