# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Risk and anomaly scoring (WP1.6).

* ``types``      ``RiskSignal``, ``RiskDetector`` protocol, ``RiskContext``.
* ``registry``   detector registry and the fail-closed runner.
* ``detectors``  generic detectors (guardrail findings, extraction confidence,
  duplicate file hash, amount outlier).
* ``stats``      robust statistics (median, MAD) shared with Pay.
* ``combiner``   risk policy (versioned YAML) and the weighted combination with hard rules.
* ``models``     ``risk_assessments`` and ``risk_signals``.
* ``service``    ``assess``, ``requires_human``, ``find_hash_matches``.

This package imports nothing on purpose, so ``pairly_core.models`` can import the table
definitions without import cycles.
"""
