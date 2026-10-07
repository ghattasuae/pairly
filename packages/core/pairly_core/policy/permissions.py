# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Policy permissions (registered through ``pairly_core.rbac.CORE_PERMISSION_MODULES``).

* ``core.policy.read``: see rule sets, their versions, policy documents, rule results,
  and run dry-run evaluations and policy searches (nothing is stored). Agents hold it,
  because agents check items against policies.
* ``core.policy.manage``: publish rule set versions, upload and archive policy
  documents. ``manage`` is a step-up action (brief 6.1), so it needs a TOTP sign-in.
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

POLICY_READ: Final = "core.policy.read"
POLICY_MANAGE: Final = "core.policy.manage"

PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        POLICY_READ,
        "View policies, rule sets and policy results",
        frozenset(
            {
                R.ADMIN,
                R.AUDITOR,
                R.AP_CLERK,
                R.APPROVER,
                R.FINANCE_CONTROLLER,
                R.RECRUITER,
                R.HIRING_MANAGER,
                R.READ_ONLY,
                R.AGENT,
            }
        ),
    ),
    PermissionSpec(
        POLICY_MANAGE,
        "Publish rule sets and manage policy documents",
        frozenset({R.ADMIN}),
    ),
)
