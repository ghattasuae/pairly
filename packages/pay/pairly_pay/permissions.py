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
  - ``approve``: approve an invoice for payment (later steps).

* **Documents** (``pay.document.read``): Pay documents (invoices, onboarding files) are
  read with Pay's own permission, so a member limited to Pay can open the invoice
  viewer. Uploads to ``/v1/documents`` stay on ``core.document.upload`` (no
  ``pay.document.upload`` is declared), so existing API keys keep working.
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
INVOICE_APPROVE: Final = "pay.invoice.approve"
INVOICE_UPLOAD: Final = "pay.invoice.upload"
DOCUMENT_READ: Final = "pay.document.read"
EXTRACTION_READ: Final = "pay.extraction.read"
EXTRACTION_CORRECT: Final = "pay.extraction.correct"

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
        INVOICE_APPROVE,
        "Approve an invoice for payment",
        frozenset({R.APPROVER, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(
        INVOICE_UPLOAD,
        "Submit invoices by upload or through the API",
        frozenset({R.ADMIN, R.AP_CLERK, R.FINANCE_CONTROLLER}),
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
)
