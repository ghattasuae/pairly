# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Webhook permissions (registered through ``pairly_core.rbac.CORE_PERMISSION_MODULES``).

* ``core.webhooks.read``: see webhook endpoints (never their secrets) and deliveries.
* ``core.webhooks.manage``: create, change, disable endpoints and rotate their secrets.
  A step-up action (TOTP sign-in).
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

WEBHOOKS_READ: Final = "core.webhooks.read"
WEBHOOKS_MANAGE: Final = "core.webhooks.manage"

PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        WEBHOOKS_READ, "View webhook endpoints and deliveries", frozenset({R.ADMIN, R.AUDITOR})
    ),
    PermissionSpec(WEBHOOKS_MANAGE, "Manage webhook endpoints", frozenset({R.ADMIN})),
)
