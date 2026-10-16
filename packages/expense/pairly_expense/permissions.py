# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Expense permissions, all ``expense.<resource>.<action>``, with default role grants.

Everyone who works in the organisation (the ``employee`` role and every other built-in
human role but auditor and read only) may submit notes and contest decisions. The
employee role holds nothing else here (the engine only lets it hold read, submit,
create and upload permissions). Reviewers (approver, finance controller,
AP clerk, admin) and auditors see every note, its receipt and what was read from it; a
submitter sees their own notes through the expense routes. Only approvers and finance
controllers approve (an ``approve`` action, so the engine asks for the second factor).
The agent role only reads.

Declaring ``expense.document.*``, ``expense.extraction.*`` and ``expense.appeal.create``
makes the engine use them instead of the ``core.*`` defaults for this workflow's
documents, extractions and appeals (``pairly_core.documents.permissions``,
``pairly_core.extraction.permissions``, ``pairly_core.decisions.scope``).
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

NOTE_READ: Final = "expense.note.read"
NOTE_SUBMIT: Final = "expense.note.submit"
NOTE_APPROVE: Final = "expense.note.approve"
DOCUMENT_READ: Final = "expense.document.read"
DOCUMENT_UPLOAD: Final = "expense.document.upload"
EXTRACTION_READ: Final = "expense.extraction.read"
EXTRACTION_CORRECT: Final = "expense.extraction.correct"
APPEAL_CREATE: Final = "expense.appeal.create"

# Permissions that let a person see every note, not just their own. ``core.decision.read``
# is the core-wide grant of admins and auditors.
SEE_ALL: Final = frozenset({NOTE_APPROVE, EXTRACTION_CORRECT, "core.decision.read"})

_STAFF = frozenset(
    {
        R.ADMIN,
        R.RECRUITER,
        R.HIRING_MANAGER,
        R.INTERVIEWER,
        R.AP_CLERK,
        R.APPROVER,
        R.FINANCE_CONTROLLER,
        R.EMPLOYEE,
    }
)
_REVIEWERS = frozenset({R.ADMIN, R.AP_CLERK, R.APPROVER, R.FINANCE_CONTROLLER, R.AUDITOR})

PERMISSIONS: Final[tuple[PermissionSpec, ...]] = (
    PermissionSpec(
        NOTE_READ,
        "View expense notes (your own, or every note when you review them)",
        frozenset({*_STAFF, R.AUDITOR, R.READ_ONLY, R.AGENT}),
    ),
    PermissionSpec(NOTE_SUBMIT, "Submit an expense note with its receipt", _STAFF),
    PermissionSpec(
        NOTE_APPROVE,
        "Approve, reject or send back expense notes",
        frozenset({R.APPROVER, R.FINANCE_CONTROLLER}),
    ),
    PermissionSpec(DOCUMENT_READ, "View expense receipts", frozenset({*_REVIEWERS, R.AGENT})),
    PermissionSpec(DOCUMENT_UPLOAD, "Upload expense receipts", _STAFF),
    PermissionSpec(
        EXTRACTION_READ,
        "View what was read from receipts, with its evidence",
        frozenset({*_REVIEWERS, R.AGENT}),
    ),
    PermissionSpec(
        EXTRACTION_CORRECT,
        "Correct what was read from a receipt",
        frozenset({R.ADMIN, R.AP_CLERK}),
    ),
    PermissionSpec(APPEAL_CREATE, "Contest an expense decision and ask for a human review", _STAFF),
)
