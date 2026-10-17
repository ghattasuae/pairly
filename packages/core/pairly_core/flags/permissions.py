# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Feature flag permissions (registered through ``pairly_core.rbac.CORE_PERMISSION_MODULES``).

* ``core.flags.read``: see the organisation's flags and whether each is overridden.
* ``core.flags.manage``: turn a flag on or off, or reset it to its default. A step-up
  action (TOTP sign-in). Admin only by default.
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

FLAGS_READ: Final = "core.flags.read"
FLAGS_MANAGE: Final = "core.flags.manage"

PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(FLAGS_READ, "View feature flags", frozenset({R.ADMIN, R.AUDITOR})),
    PermissionSpec(FLAGS_MANAGE, "Change feature flags", frozenset({R.ADMIN})),
)
