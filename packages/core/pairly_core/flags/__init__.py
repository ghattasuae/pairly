# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Feature flags per organisation (WP1.12).

* ``registry``     flags are declared in code (``FlagSpec``: key, description, default);
                   core declares ``CORE_FLAGS``, a workflow plugin may add its own
                   through an optional ``feature_flags`` attribute, under its namespace.
* ``models``       ``feature_flags``: an organisation's overrides (org scoped, RLS).
* ``service``      ``is_enabled``, ``effective_flags``, ``set_flag``, ``reset_flag``.
* ``permissions``  ``core.flags.read`` and ``core.flags.manage``.

A flag that is not declared does not exist: reading or setting it is an error, so a
typo can never silently fall back to a default.
"""
