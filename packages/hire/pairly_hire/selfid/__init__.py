# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Voluntary self-identification (brief 3.2.1): kept apart from everything that assesses.

* ``schema``: the categories an organisation may ask, with fixed options (always
  optional, always with "prefer not to say").
* ``models``: the ``hire_self_identification`` table, its own row per application,
  encrypted, readable only with ``hire.selfid.read`` and only as aggregates
  (``pairly_hire.api.reports``).

The separation is enforced, not promised: ``pairly_hire.scoring`` may not import this
package (import-linter contract in the root ``pyproject.toml`` and
``tests/test_selfid_isolation.py``), the application answers that scoring reads never
hold these values (they go straight to this table), and the routes that show an
application never read it.
"""
