# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Retention permissions (registered through ``pairly_core.rbac.CORE_PERMISSION_MODULES``).

* ``core.retention.read``: see the retention rules and the latest dry-run report.
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

RETENTION_READ: Final = "core.retention.read"

PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        RETENTION_READ, "View retention rules and dry-run reports", frozenset({R.ADMIN, R.AUDITOR})
    ),
)
