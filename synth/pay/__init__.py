# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Synthetic Pay data (WP4.1): vendors, invoices, purchase orders, goods receipts,
matching cases, attack invoices and inbound emails.

* ``models``     Pydantic models: vendor master, invoice ground truth, POs, receipts,
                 matching cases, attack labels, emails.
* ``banking``    synthetic banks, IBANs with valid ISO 13616 check digits, random TRNs.
* ``catalog``    spend categories and line items (en, ar), name syllables, places.
* ``vendors``    the vendor master and each vendor's invoice template.
* ``invoices``   line items, Decimal totals, numbering, terms, the ``InvoiceDoc``.
* ``layout``     an invoice as positioned items on an A4 page.
* ``render``     PDF (reportlab), scans (Pillow), DOCX, UBL / PINT AE XML, email body.
* ``dataset``    regular invoices with POs, goods receipts and matching cases.
* ``attacks``    attack invoices with what a detector should flag; the demo trio.
* ``emails``     inbound ``.eml`` messages wrapping the invoices.

Run ``uv run python -m synth.pay --help``. See ``synth/pay/README.md``.
"""
