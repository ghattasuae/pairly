# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pay permissions, all ``pay.<resource>.<action>``, with default role grants.

Who does what (brief 4.1, 6.2):

* **Vendors** (``pay.vendor.read`` / ``write``): AP clerks and finance controllers keep
  the vendor master (create, edit contact data, invite a vendor to onboard, review an
  onboarding). Approvers and auditors read it. Admins keep it too (an admin sets up the
  organisation). The agent role only reads.
* **Bank-detail changes** (``pay.bank_change.*``), the signature control:

  - ``create``: open a change ticket (AP clerks, finance controllers, admins). It is
    also the run start permission of the ``pay_bank_change`` graph (the verifier starts
    the approval run, see ``pairly_pay.bank_changes``).
  - ``verify``: record the out-of-band call to the phone number on file (AP clerks and
    finance controllers). A ``verify`` action, so a second factor (ADR 0012).
  - ``approve``: the dual approval (approvers and finance controllers). An ``approve``
    action, so a second factor. Admins do not approve by default.

* **Payment holds** (``pay.hold.manage``): place a manual hold, or release a hold a
  ticket left in place on purpose (finance controllers). A ``manage`` action, so a
  second factor.
* **Settings** (``pay.settings.manage``): the cooling-off period (admins, finance
  controllers). A ``manage`` action.
* **Invoices** (``pay.invoice.*``):

  - ``read``: the invoice list and the side-by-side viewer.
  - ``upload``: submit invoices by upload or through the API (an API key may hold it:
    keys only read, propose and upload). It covers the invoice's document: Pay's own
    routes store it, so ``core.document.upload`` is not needed (and still guards
    ``/v1/documents`` for Pay, unchanged).
  - ``propose``: the agent's part. It starts the ``pay_invoice`` intake run (every
    invoice is read by the agent, whoever sent it) and later proposes coding and flags.
  - ``process``: the AP clerk's work on an invoice: correct fields, link the vendor,
    mark it reviewed, reject it (coding and matching later).
  - ``submit``: send a ``ready`` invoice for approval (AP clerks, finance
    controllers). The start permission of the ``pay_invoice_approval`` graph, so the
    sender is a preparer and never approves it.
  - ``approve``: approve an invoice for payment (approvers and finance controllers;
    the approval rules pick who, ``policy/invoice_approval_rules.yaml``).

* **Payment runs** (``pay.payment_run.*``, WP4.7):

  - ``read``: the payment run list and detail.
  - ``create``: propose a run from approved invoices, or cancel one before it is paid
    (AP clerks, finance controllers). It is the start permission of the
    ``pay_payment_run`` graph, so the proposer is a preparer and never approves it.
  - ``approve``: approve a run (finance controllers only). A second factor.
  - ``export``: download the bank payment file (AP clerks, finance controllers). An
    ``export`` action, so a second factor. Nothing is sent to a bank.
  - ``record`` (``pay.payment.record``): mark invoices of an exported run as paid,
    after the bank confirmed it outside Pairly.

* **Purchase orders and goods receipts** (``pay.purchase_order.*``, brief 4.5.1):
  ``read`` for the readers and the agent (matching reads them); ``write`` (create or
  import from CSV) for AP clerks, finance controllers and admins.

* **Chart of accounts and cost centres** (``pay.coding.manage``, brief 4.5.3): create a
  new version of a list or import one from CSV (admins, finance controllers). A
  ``manage`` action, so a second factor. Coding invoice lines is ``process``.
* **Documents** (``pay.document.read``): Pay documents (invoices, onboarding files) are
  read with Pay's own permission, so a member limited to Pay can open the invoice
  viewer. Uploads to ``/v1/documents`` stay on ``core.document.upload`` (no
  ``pay.document.upload`` is declared), so existing API keys keep working.
* **Analytics** (``pay.analytics.read``): the Pay analytics page (brief 4.9), for
  finance controllers and admins.
* **Extractions** (``pay.extraction.read`` / ``correct``): what was read from a Pay
  document and the corrections of it. ``correct`` is never an agent's.
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

VENDOR_READ: Final = "pay.vendor.read"
VENDOR_WRITE: Final = "pay.vendor.write"
BANK_CHANGE_READ: Final = "pay.bank_change.read"
BANK_CHANGE_CREATE: Final = "pay.bank_change.create"
BANK_CHANGE_VERIFY: Final = "pay.bank_change.verify"
BANK_CHANGE_APPROVE: Final = "pay.bank_change.approve"
HOLD_MANAGE: Final = "pay.hold.manage"
SETTINGS_MANAGE: Final = "pay.settings.manage"
INVOICE_READ: Final = "pay.invoice.read"
INVOICE_PROPOSE: Final = "pay.invoice.propose"
INVOICE_PROCESS: Final = "pay.invoice.process"
INVOICE_SUBMIT: Final = "pay.invoice.submit"
INVOICE_APPROVE: Final = "pay.invoice.approve"
INVOICE_UPLOAD: Final = "pay.invoice.upload"
PAYMENT_RUN_READ: Final = "pay.payment_run.read"
PAYMENT_RUN_CREATE: Final = "pay.payment_run.create"
PAYMENT_RUN_APPROVE: Final = "pay.payment_run.approve"
PAYMENT_RUN_EXPORT: Final = "pay.payment_run.export"
PAYMENT_RECORD: Final = "pay.payment.record"
PURCHASE_ORDER_READ: Final = "pay.purchase_order.read"
PURCHASE_ORDER_WRITE: Final = "pay.purchase_order.write"
DOCUMENT_READ: Final = "pay.document.read"
EXTRACTION_READ: Final = "pay.extraction.read"
EXTRACTION_CORRECT: Final = "pay.extraction.correct"
ANALYTICS_READ: Final = "pay.analytics.read"
CODING_MANAGE: Final = "pay.coding.manage"

_READERS = frozenset({R.ADMIN, R.AP_CLERK, R.APPROVER, R.FINANCE_CONTROLLER, R.AUDITOR})

PERMISSIONS: Final[tuple[PermissionSpec, ...]] = (
    PermissionSpec(VENDOR_READ, "View vendors and their risk profile", _READERS | {R.AGENT}),
    PermissionSpec(
        VENDOR_WRITE,
        "Create and edit vendors, invite vendors and review onboardings",
        frozenset({R.ADMIN, R.AP_CLERK, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(BANK_CHANGE_READ, "View bank-detail change tickets", _READERS),
    PermissionSpec(
        BANK_CHANGE_CREATE,
        "Open a bank-detail change ticket (puts the vendor's payments on hold)",
        frozenset({R.ADMIN, R.AP_CLERK, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        BANK_CHANGE_VERIFY,
        "Record the call back to the vendor on the phone number on file",
        frozenset({R.AP_CLERK, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        BANK_CHANGE_APPROVE,
        "Approve or reject a verified bank-detail change",
        frozenset({R.APPROVER, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        HOLD_MANAGE,
        "Place or release a vendor's payment hold",
        frozenset({R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        SETTINGS_MANAGE,
        "Change Pay settings such as the cooling-off period",
        frozenset({R.ADMIN, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(INVOICE_READ, "View invoices", _READERS | {R.AGENT}),
    PermissionSpec(
        INVOICE_PROPOSE,
        "Propose coding, matching or a risk flag for an invoice",
        frozenset({R.AGENT}),
    ),
    PermissionSpec(
        INVOICE_PROCESS,
        "Process invoices: code, match and send for approval",
        frozenset({R.AP_CLERK, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        INVOICE_SUBMIT,
        "Send an invoice for approval",
        frozenset({R.AP_CLERK, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        INVOICE_APPROVE,
        "Approve an invoice for payment",
        frozenset({R.APPROVER, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        INVOICE_UPLOAD,
        "Submit invoices by upload or through the API",
        frozenset({R.ADMIN, R.AP_CLERK, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(PAYMENT_RUN_READ, "View payment runs", _READERS),
    PermissionSpec(
        PAYMENT_RUN_CREATE,
        "Propose or cancel a payment run",
        frozenset({R.AP_CLERK, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        PAYMENT_RUN_APPROVE,
        "Approve a payment run",
        frozenset({R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        PAYMENT_RUN_EXPORT,
        "Download the bank payment file of an approved run",
        frozenset({R.AP_CLERK, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        PAYMENT_RECORD,
        "Mark invoices of an exported payment run as paid",
        frozenset({R.AP_CLERK, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        PURCHASE_ORDER_READ, "View purchase orders and goods receipts", _READERS | {R.AGENT}
    ),
    PermissionSpec(
        PURCHASE_ORDER_WRITE,
        "Create or import purchase orders and goods receipts",
        frozenset({R.ADMIN, R.AP_CLERK, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        CODING_MANAGE,
        "Change the chart of accounts and the cost centres (new versions, CSV import)",
        frozenset({R.ADMIN, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(DOCUMENT_READ, "View Pay documents and their content", _READERS | {R.AGENT}),
    PermissionSpec(
        EXTRACTION_READ,
        "View what was read from Pay documents, with evidence and corrections",
        _READERS | {R.AGENT},
    ),
    PermissionSpec(
        EXTRACTION_CORRECT,
        "Correct what was read from an invoice",
        frozenset({R.ADMIN, R.AP_CLERK, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        ANALYTICS_READ,
        "View Pay analytics (touchless rate, cycle time, exceptions, fraud flags)",
        frozenset({R.ADMIN, R.FINANCE_CONTROLLER}),
    ),
)
