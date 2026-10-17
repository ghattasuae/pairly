# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Retention permissions (registered through ``pairly_core.rbac.CORE_PERMISSION_MODULES``).

* ``core.retention.read``: see the retention rules, the latest report, the preview list
  of what is scheduled for deletion and the legal holds.
* ``core.retention.manage``: switch rules on or off, change their periods and the daily
  cap, place and release legal holds (second factor: ``manage`` is a step-up action).
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

RETENTION_READ: Final = "core.retention.read"
RETENTION_MANAGE: Final = "core.retention.manage"

PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        RETENTION_READ,
        "View retention rules, reports, the deletion preview and legal holds",
        frozenset({R.ADMIN, R.AUDITOR}),
    ),
    PermissionSpec(
        RETENTION_MANAGE,
        "Change retention settings and place or release legal holds",
        frozenset({R.ADMIN}),
    ),
)
