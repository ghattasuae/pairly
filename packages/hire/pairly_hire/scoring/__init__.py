# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""What scoring may see (the screening graph is built in WP2.4).

Scoring reads an application through ``inputs.scoring_input`` only: the locked rubric,
the CV document and the answers to the custom and knockout questions. It must never see
voluntary self-identification answers. That is enforced three ways:

* by schema: self-identification answers are never written to ``hire_applications``
  (``pairly_hire.applying`` sends them to ``hire_self_identification``), and
  ``ScoringInput`` has no field that could carry them;
* by imports: this package may not import ``pairly_hire.selfid`` or the form builder
  that knows its categories (import-linter contract in the root ``pyproject.toml``);
* by test: ``packages/hire/tests/test_selfid_isolation.py`` walks this package's imports.
"""
