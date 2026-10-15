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
* ``purchase_orders``: purchase orders and goods receipts, created or imported from CSV.
* ``analytics``: the Pay analytics (brief 4.9), every number with its records.
* ``forecast``: the cash forecast, by week and currency, every number with its invoices.
* ``reconciliation``: bank statements (CSV, camt.053) matched with payments marked
  paid; unmatched lines and paid invoices no statement explains.
* ``month_end``: the month-end report (open payables and aging, accrual suggestions,
  open exceptions) and its CSV; ``journal``: the generic journal export.
* ``exceptions``: the exceptions queue and drafted messages to vendors and requesters,
  sent only after approval (brief 4.5.4).
* ``coding``: the chart of accounts and cost centres, and GL coding of invoice lines
  with suggestions from history (brief 4.5.3).
* ``vendor_portal``: the vendor portal (brief 4.7): magic-link sign-in for a vendor's
  contacts, invoice statuses, submissions, messages, contact details and bank change
  requests, each limited to the contact's own vendor.
* ``portal_admin``: the AP team's side: portal access for contacts on file, the portal
  thread and replies, and applying credit notes.

Imported only by the API process (the worker never loads routes).
"""

from __future__ import annotations

from fastapi import APIRouter

from pairly_pay.api import (
    analytics,
    approval_rules,
    bank_changes,
    coding,
    exceptions,
    forecast,
    invoices,
    journal,
    month_end,
    onboarding,
    payment_runs,
    portal_admin,
    purchase_orders,
    reconciliation,
    vendor_portal,
    vendors,
)

router = APIRouter()
router.include_router(vendors.router)
router.include_router(bank_changes.router)
router.include_router(onboarding.router)
router.include_router(invoices.router)
router.include_router(approval_rules.router)
router.include_router(payment_runs.router)
router.include_router(purchase_orders.router)
router.include_router(analytics.router)
router.include_router(forecast.router)
router.include_router(reconciliation.router)
router.include_router(month_end.router)
router.include_router(journal.router)
router.include_router(coding.router)
router.include_router(exceptions.router)
router.include_router(vendor_portal.router)
router.include_router(portal_admin.router)
