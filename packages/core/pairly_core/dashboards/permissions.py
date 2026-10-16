# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Dashboard permissions (registered through ``pairly_core.rbac.CORE_PERMISSION_MODULES``).

* ``core.admin_dashboard.read``: the organisation admin dashboard (brief 5.1 item 13).
* ``core.ai_governance.read``: the AI governance dashboard (item 14), admin and auditor.
* ``core.platform.read``: the platform operations dashboard and job history (item 15).
  Held by admins, but only effective in the deployment's system organisation
  (``PAIRLY_SYSTEM_ORG_ID``): an admin of any other organisation is refused.

The auditor dashboard (item 12) needs ``core.audit.read``.
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

ADMIN_DASHBOARD_READ: Final = "core.admin_dashboard.read"
AI_GOVERNANCE_READ: Final = "core.ai_governance.read"
PLATFORM_READ: Final = "core.platform.read"
AUDITOR_DASHBOARD_READ: Final = "core.audit.read"

PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        ADMIN_DASHBOARD_READ, "View the organisation admin dashboard", frozenset({R.ADMIN})
    ),
    PermissionSpec(
        AI_GOVERNANCE_READ, "View the AI governance dashboard", frozenset({R.ADMIN, R.AUDITOR})
    ),
    PermissionSpec(
        PLATFORM_READ,
        "View platform operations (system organisation only)",
        frozenset({R.ADMIN}),
    ),
)
