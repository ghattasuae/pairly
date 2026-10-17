# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""API key permissions (registered through ``pairly_core.rbac.CORE_PERMISSION_MODULES``).

``core.api_keys.manage``: create, list and revoke the organisation's API keys. ``manage``
is a step-up action, so it needs a TOTP sign-in. Admin only by default.
"""

from __future__ import annotations

from typing import Final

from pairly_core.rbac import BuiltinRole as R
from pairly_core.rbac import PermissionSpec

API_KEYS_MANAGE: Final = "core.api_keys.manage"

PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(API_KEYS_MANAGE, "Create, list and revoke API keys", frozenset({R.ADMIN})),
)
