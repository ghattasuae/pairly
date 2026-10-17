# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Notification permissions (registered through ``pairly_core.rbac.CORE_PERMISSION_MODULES``).

* ``core.template.read``: see notification templates and preview them (nothing is sent).
* ``core.template.manage``: save template versions. ``manage`` is a step-up action.
* ``core.integration.manage``: configure Slack (workspace, bot token, channel mappings).
  Step-up as well.

A person's own notifications, preferences and Slack link need no permission beyond
membership of the organisation: every route scopes them to the signed-in user.
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

TEMPLATE_READ: Final = "core.template.read"
TEMPLATE_MANAGE: Final = "core.template.manage"
INTEGRATION_MANAGE: Final = "core.integration.manage"

PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        TEMPLATE_READ,
        "View and preview notification templates",
        frozenset({R.ADMIN, R.RECRUITER, R.HIRING_MANAGER, R.AP_CLERK, R.AUDITOR}),
    ),
    PermissionSpec(TEMPLATE_MANAGE, "Create notification template versions", frozenset({R.ADMIN})),
    PermissionSpec(
        INTEGRATION_MANAGE, "Configure integrations such as Slack", frozenset({R.ADMIN})
    ),
)
