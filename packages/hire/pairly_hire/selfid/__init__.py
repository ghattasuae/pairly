# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Voluntary self-identification (brief 3.2.1): kept apart from everything that assesses.

* ``schema``: the categories an organisation may ask, with fixed options (always
  optional, always with "prefer not to say").
* ``models``: the ``hire_self_identification`` table, its own row per application,
  encrypted. Two readers only: bias reporting, only as counts per group
  (``reporting.stage_counts``, used by ``pairly_hire.analytics.bias``), and the
  candidate's own data export from the portal (``export.subject_answers``, their own
  answers back to them, never to staff).
* ``reporting``: that counts-only reader for the bias report.
* ``export``: that narrow subject-export reader; the portal service is its only importer.

The separation is enforced, not promised: ``pairly_hire.scoring`` may not import this
package; the pipeline, messaging and scheduling may not import its table or the subject
export; the portal reaches the table only through ``export`` (import-linter contracts in
the root ``pyproject.toml`` and ``tests/test_selfid_isolation.py``), the application
answers that scoring reads never hold these values (they go straight to this table),
and the routes that show an application never read it.
"""
