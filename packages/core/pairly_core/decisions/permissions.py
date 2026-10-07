# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Decision record permissions (registered via ``pairly_core.rbac.CORE_PERMISSION_MODULES``).

``core.decision.read`` (receipts, history) is defined in ``pairly_core.rbac``. Added here:

* ``core.decision.replay``: re-run a decision and store the result (auditor, admin).
* ``core.decision_personal.read``: see decrypted personal data in records and receipts.
  Admin only by default: an auditor sees everything else, and gets this permission only
  through a role that grants it explicitly.
* ``core.appeal.read`` / ``core.appeal.create``: the appeals queue, and filing an appeal
  on behalf of a candidate or vendor (staff, until the portals exist).
* ``core.appeal.decide``: resolve an appeal with a written outcome (step-up: TOTP).
* ``core.compliance.export``: build and download a compliance pack (step-up: TOTP).
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

DECISION_READ: Final = "core.decision.read"
DECISION_REPLAY: Final = "core.decision.replay"
DECISION_PERSONAL_READ: Final = "core.decision_personal.read"
APPEAL_READ: Final = "core.appeal.read"
APPEAL_CREATE: Final = "core.appeal.create"
APPEAL_DECIDE: Final = "core.appeal.decide"
COMPLIANCE_EXPORT: Final = "core.compliance.export"

_STAFF = (R.RECRUITER, R.HIRING_MANAGER, R.INTERVIEWER, R.AP_CLERK)
_DECIDERS = (R.APPROVER, R.FINANCE_CONTROLLER)

PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        DECISION_REPLAY, "Replay a decision and store the result", frozenset({R.ADMIN, R.AUDITOR})
    ),
    PermissionSpec(
        DECISION_PERSONAL_READ,
        "See decrypted personal data in decision records and receipts",
        frozenset({R.ADMIN}),
    ),
    PermissionSpec(
        APPEAL_READ,
        "See appeals and their outcomes",
        frozenset({R.ADMIN, R.AUDITOR, *_STAFF, *_DECIDERS}),
    ),
    PermissionSpec(
        APPEAL_CREATE,
        "File an appeal on behalf of a candidate or vendor",
        frozenset({R.ADMIN, *_STAFF, *_DECIDERS}),
    ),
    PermissionSpec(
        APPEAL_DECIDE,
        "Resolve an appeal with a written outcome",
        frozenset({R.ADMIN, R.HIRING_MANAGER, *_DECIDERS}),
    ),
    PermissionSpec(
        COMPLIANCE_EXPORT,
        "Build and download compliance packs",
        frozenset({R.ADMIN, R.AUDITOR}),
    ),
)
