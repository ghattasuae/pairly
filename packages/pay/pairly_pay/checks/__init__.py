# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Invoice validation and fraud checks (brief 4.4, WP4.6).

Every check is a pure function of an ``InvoiceFacts`` (what was read from the invoice and
its document) and a ``History`` (what the organisation already has), and returns a
``CheckResult``: ``pass``, ``fail`` (with findings) or ``error`` (it could not run, which
sends the invoice to a person). The database work (reading facts, opening a bank-detail
change ticket, the policy checks, the risk assessment, decision records) is in
``pairly_pay.checks.service``, registered in the ``CHECKS`` hook of the ``pay_invoice``
graph.

* ``arithmetic``     line totals, subtotal, VAT per tax code (5% UAE default, zero-rated
                     and reverse charge), total. ``Decimal``, one minor unit tolerance.
* ``duplicates``     same number, fuzzy numbers, same amount within days, same lines,
                     same file. Each match names the suspected original.
* ``bank``           printed bank details against the vendor master.
* ``impersonation``  sender domain, lookalike domains, first-time sender, urgency.
* ``amounts``        unusual amount (engine ``amount_outlier``), round amounts, split
                     invoices just under the approval threshold.
* ``forgery``        template fingerprint, metadata, editing traces, TRN. Flags only.
* ``injection``      the guardrail findings, as Pay findings.
* ``policy``         the tenant's rule set and spend policy (``pairly_pay.checks.policy``).
* ``matching``       2-way and 3-way matching with the cited purchase order: price,
                     quantity and total tolerances, receipts, partial deliveries and
                     what earlier invoices of the PO took.

Every finding carries a one-sentence explanation in English and Arabic.
"""
